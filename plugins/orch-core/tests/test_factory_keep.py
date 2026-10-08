"""The owner's live run (2026-10-08): a Dark start, the planner made two children and auto-approved them, then the
epic page offered "Re-approve the epic"; the plain re-approve signed a charter without the delegation, the run
vanished from Factories and the children worked on with nothing to release or close them. Reproduced from the
planner flow (tests/factory_e2e.py), then each fix: why a re-approval was offered, re-signing the same charter,
ending a run only on purpose, and ended runs staying visible."""
import shutil

import pytest

from orch.core import epics, factory_sessions as fs, store
from factory_e2e import STOP, Script
from test_factory_e2e import make_world, world  # noqa: F401  (the fixtures)

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


@pytest.fixture
def planned(make_world):
    """The live run up to 04:57: the planner split the epic into two children and auto-approved them; their workers
    started and wait at their prompt."""
    w = make_world({"release": "merge"})
    w.scripts["worker"] = lambda a: Script(a, [("stop", lambda: STOP)])
    w.settle(release=False)
    kids = [c.id for c in epics.children(w.ws, w.epic)]
    assert len(kids) == 2
    return w


def _epic(w):
    return store.load(w.ws, w.epic)[1]


def test_planner_children_are_covered_by_the_running_charter_and_no_reapproval_is_offered(planned):
    w = planned
    from orch.core.query import needs_you
    from orch.dashboard.data import epic as epic_data
    from orch.dashboard.data.cards import Cards
    e = _epic(w)
    s = epics.summary(w.ws, e)
    assert {c["state"] for c in s["children"]} == {"delegated"}
    assert s["delegation"]["active"] and not s["delegation"]["epic_changed"]  # the planner's log line changed nothing
    entries = store.scan(w.ws)
    needs = needs_you(w.ws, entries=entries)
    assert not [n for n in needs if n["kind"] == "approve-epic"]  # Today asks nothing
    from orch.core.events import read_events
    page = epic_data.page_data(w.ws, e, entries=entries, needs=needs, events=read_events(w.ws),
                               builder=Cards(w.ws, entries=entries, needs=needs))
    assert page["reapprove"] is False  # the epic page offered "Re-approve the epic" here: the live trap


# -- 1. re-approving a factory epic keeps its charter; ending the run is its own, explicit action -----------------

def _client(w):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(w.ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _text(r):
    return " ".join(r.text.split())


def _seen(w):
    return epics.charter(w.ws, _epic(w))["content_hash"]


def _charters(w):
    from orch.core import ledger
    return [e for e in ledger.entries(w.ws) if e.get("kind") == "charter" and e.get("ticket") == w.epic]


def _edit_epic(w):
    """The human edits the epic's own text: the one change that needs the charter signed again."""
    from orch.core.ops import Ops
    Ops(w.ws, w.human).set_section(w.epic, "Requirements", _epic(w).section("Requirements") + " Use grey.")


def test_a_plain_approval_of_a_running_factory_epic_is_refused_and_signs_nothing(planned):
    w = planned
    from conftest import human_ops
    from orch.errors import ValidationError
    before = _charters(w)
    with pytest.raises(ValidationError, match="would end the run"):
        human_ops(w.ws, w.human).approve(w.epic, "requirements")  # the old Re-approve: no delegation
    c = _client(w)
    r = c.post(f"/t/{w.epic}/approve", data={"gate": "requirements", "seen": _seen(w), "start": ""},
               follow_redirects=False)  # the old form, as the live run posted it
    assert "err=" in r.headers["location"]
    assert _charters(w) == before and epics.delegation(w.ws, _epic(w))["active"]
    # an addon's approve intent never sends a delegation: it fails closed the same way
    from orch.addons.api import Intent
    from orch.addons.intents import execute
    with pytest.raises(ValidationError):
        execute(w.ws, Intent("approve", ref=w.epic, gate="requirements", expected_hash=_seen(w)),
                allowed_ref=w.epic, tickets=False, actor=w.human, source="resolve")
    assert _charters(w) == before


def test_the_epic_page_offers_resign_with_its_checklist_only_when_the_text_changed(planned):
    w = planned
    c = _client(w)
    page = _text(c.get(f"/t/{w.epic}"))
    assert "Re-approve the epic" not in page and "Re-sign the" not in page  # nothing is missing: nothing offered
    assert "End the factory run" in page  # the one explicit way to sign it without the factory
    _edit_epic(w)
    page = _text(c.get(f"/t/{w.epic}"))
    assert "Re-sign the Dark charter" in page and "Re-approve the epic" not in page
    assert "The epic&#39;s text changed: re-sign the charter to continue" in page
    assert "Mode: Dark AI Factory" in page and "Release up to: merge" in page and "You give the verdict" in page
    run = _text(c.get(f"/factory/{w.epic}"))
    assert "The epic&#39;s text changed: re-sign the charter to continue" in run and "Re-sign the charter" in run
    from orch.core.query import needs_you
    from orch.dashboard.data.steps import why_waiting
    (item,) = [n for n in needs_you(w.ws) if n["kind"] == "approve-epic"]
    assert item["factory"] and why_waiting(item) == "The epic's text changed: re-sign the charter to continue"
    assert "Re-sign the charter" in _text(c.get("/"))


def test_resign_signs_the_same_charter_over_the_new_text_and_the_run_goes_on(planned):
    w = planned
    _edit_epic(w)
    old = epics.delegation(w.ws, _epic(w))
    assert old["epic_changed"] and not old["active"]
    c = _client(w)
    stale = c.post(f"/t/{w.epic}/resign", data={"seen": _seen(w), "charter": "sha256:other"}, follow_redirects=False)
    assert "err=" in stale.headers["location"]  # a charter other than the one shown: nothing signed
    r = c.post(f"/t/{w.epic}/resign", data={"seen": _seen(w), "charter": old["id"]}, follow_redirects=False)
    assert "err=" not in r.headers["location"], r.headers["location"]
    first, last = _charters(w)[0], _charters(w)[-1]
    assert last["delegate"] == first["delegate"] and last["delegation"] != old["id"]
    assert {k["id"] for k in last["children"]} == {k.id for k in epics.children(w.ws, w.epic)}
    d = epics.delegation(w.ws, _epic(w))
    assert d["active"] and fs.armed(w.ws, d["id"])
    from orch.dashboard.data import factory as data
    assert data.run_view(w.ws, _epic(w))["state"] != "changed"
    assert "re-signed the factory charter" in _epic(w).section("Log")


def test_end_the_factory_run_is_explicit_and_recorded(planned):
    w = planned
    c = _client(w)
    did = epics.delegation(w.ws, _epic(w))["id"]
    r = c.post(f"/t/{w.epic}/approve", data={"gate": "requirements", "seen": _seen(w), "end_factory": "1",
                                              "start": "factory"}, follow_redirects=False)
    assert "err=" in r.headers["location"]  # ending signs no delegation: a factory choice with it is refused
    r = c.post(f"/t/{w.epic}/approve", data={"gate": "requirements", "seen": _seen(w), "end_factory": "1"},
               follow_redirects=False)
    assert "err=" not in r.headers["location"], r.headers["location"]
    last = _charters(w)[-1]
    assert last["ends_factory"] == did and not last.get("delegation")
    assert epics.factory_charter(w.ws, w.epic) is None
    from conftest import human_ops
    from orch.errors import ValidationError
    with pytest.raises(ValidationError, match="no factory run to end"):
        human_ops(w.ws, w.human).approve(w.epic, "requirements", end_factory=True)


def test_the_cli_resigns_by_default_prints_the_charter_and_ends_only_with_end_factory(planned, monkeypatch, capsys):
    w = planned
    from orch import actor
    from orch.cli import run
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": w.epic)
    _edit_epic(w)
    capsys.readouterr()
    assert run(["approve", w.epic, "requirements"]) == 0
    out = capsys.readouterr()
    text = out.out + out.err
    assert "Re-sign the Dark charter" in text and "Mode: Dark AI Factory" in text and "--end-factory" in text
    assert _charters(w)[-1]["delegate"] == _charters(w)[0]["delegate"]
    assert epics.delegation(w.ws, _epic(w))["active"]
    monkeypatch.setattr("builtins.input", lambda prompt="": "nope")  # the typed id is still required
    assert run(["approve", w.epic, "requirements", "--end-factory"]) != 0
    assert epics.factory_charter(w.ws, w.epic) is not None
    monkeypatch.setattr("builtins.input", lambda prompt="": w.epic)
    assert run(["approve", w.epic, "requirements", "--end-factory"]) == 0
    out = capsys.readouterr()
    assert "End the factory run" in out.out + out.err
    assert epics.factory_charter(w.ws, w.epic) is None and _charters(w)[-1].get("ends_factory")


# -- 3. an ended run stays on Factories and its run view, said as Ended with why -------------------------------------

def _ended(w):
    from orch.dashboard.data import factory as data
    rows = [r for r in data.factory_list(w.ws) if r["epic"] == w.epic]
    assert len(rows) == 1, "the run vanished from Factories"
    view = data.run_view(w.ws, _epic(w))
    assert view is not None, "the run view vanished"
    return rows[0], view


def _assert_listed_as_ended(w, kind, why):
    row, view = _ended(w)
    assert row["ended"]["kind"] == kind and why in row["ended"]["why"], row["ended"]
    assert view["ended"] == row["ended"] and not view["live"] and not view["active"]
    s = view["summary"]
    assert s["children"] == 2 and "done" in s and "stages" in s and s["took"]
    c = _client(w)
    page = _text(c.get("/factory"))
    assert "1 ended" in page and kind in page
    run = _text(c.get(f"/factory/{w.epic}"))
    assert kind in run and f'href="/t/{w.epic}"' in run and "Summary" in run
    return view


def test_ended_by_a_plain_reapprove_as_in_the_live_run(planned, monkeypatch):
    """The live run's ledger: a later charter without the delegation and without `ends_factory` (signed before this
    fix), shown from the ledger alone."""
    w = planned
    from conftest import human_ops
    monkeypatch.setattr(epics, "factory_charter", lambda *a, **k: None)  # the old code: no refusal, no record
    human_ops(w.ws, w.human).approve(w.epic, "requirements")
    monkeypatch.setattr(epics, "factory_charter", _REAL_FACTORY_CHARTER)  # this fix again, for the reading
    view = _assert_listed_as_ended(w, "Ended by you", "without the factory")
    assert view["ended"]["restart"]  # one action: start a new run from the epic page
    assert "Start a new run" in _text(_client(w).get(f"/factory/{w.epic}"))
    page = _text(_client(w).get(f"/t/{w.epic}"))
    assert f'href="/factory/{w.epic}"' in page and "Start Dark AI Factory" in page  # linked, and a new start offered
    human_ops(w.ws, w.human).approve(w.epic, "requirements", delegate={"factory": True, "dark": True})
    assert _ended(w)[0]["ended"] is None  # the new run is the one shown


_REAL_FACTORY_CHARTER = epics.factory_charter


def test_ended_by_end_the_factory_run(planned):
    w = planned
    from conftest import human_ops
    human_ops(w.ws, w.human).approve(w.epic, "requirements", end_factory=True)
    _assert_listed_as_ended(w, "Ended by you", "You ended the factory run")


def test_ended_by_stop_the_run(planned):
    w = planned
    from orch.core.ops import Ops
    Ops(w.ws, w.human).epic_pause(w.epic)
    _assert_listed_as_ended(w, "Paused", "You stopped the run")


def test_ended_by_close(planned):
    w = planned
    from orch.core.ops import Ops
    Ops(w.ws, w.human).close(w.epic, "not needed after all", skip_release="not needed after all")
    _assert_listed_as_ended(w, "Finished", "You closed it")


def test_ended_by_budget(planned):
    w = planned
    w.at(73 * 3600)  # past the 72 hours the charter signs
    _assert_listed_as_ended(w, "Stopped", "time budget")


def test_ended_by_closing_by_itself(world):
    w = world
    w.settle()
    assert _epic(w).status == "done"
    row, view = _ended(w)
    assert row["ended"]["kind"] == "Finished" and "Closed by itself" in row["ended"]["why"]
    assert view["summary"]["done"] == 2 and view["summary"]["stages"] == ["merge", "dev", "production"]


def test_counts_say_working_and_ended_apart(planned):
    w = planned
    from conftest import human_ops
    from orch.core.ops import Ops
    e2 = Ops(w.ws, w.human).new("Second", type="epic", sections={"Requirements": "x", "Acceptance criteria": "- [ ] y"})
    human_ops(w.ws, w.human).approve(e2.id, "requirements", delegate={"factory": True, "dark": True})
    human_ops(w.ws, w.human).approve(w.epic, "requirements", end_factory=True)
    from orch.dashboard.data import factory as data
    rows = data.factory_list(w.ws)
    assert {r["epic"] for r in rows} == {w.epic, e2.id}
    assert [r["epic"] for r in rows][-1] == w.epic  # ended runs last
    assert "1 ended" in _text(_client(w).get("/factory"))


# -- 4. a shell redirect into a file is denied with the way that works -------------------------------------------

def test_a_redirect_into_a_file_is_denied_with_the_write_tool_hint(make_world):
    from orch.core import permits
    from test_factory_e2e import edit_worker
    w = make_world({"release": "merge"})
    live = "printf 'AC1: elephants.json exists\\nAC2: committed\\n' > /tmp/verification.md"  # as the live agent wrote it
    edit_worker(w, "elephants.json", lambda s, a: s.before("evidence", ("redirect", lambda: a.bash(live))))
    w.settle(release=False)
    kid = next(c.id for c in epics.children(w.ws, w.epic) if "elephants.json" in _epic_ac(w, c.id))
    (bad,) = w.session(kid).agent.denied()
    assert bad.text == live and bad.by == "hook", (bad.by, bad.message)
    assert permits.REDIRECT_HINT in bad.message and "was not run" in bad.message
    (card,) = [c for c in w.cards() if c["command"] == live]
    assert permits.REDIRECT_HINT in card["reason"]
    for cmd in ("echo ok >> notes.md", "cat > notes.md"):
        assert permits._writes_file(cmd)
    assert not permits._writes_file("orch show T-1 2>/dev/null") and not permits._writes_file("grep '>' f")


def _epic_ac(w, tid):
    return store.load(w.ws, tid)[1].section("Acceptance criteria")

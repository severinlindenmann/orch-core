"""Dark AI Factory: the opt-in auto-close. A Dark charter signed with `close` lets the runner give the epic's done
verdict by itself once everything is proven, through the same Ops path as the human's Accept, marked as the
charter's. Every condition is tested alone; the release runs with real git and a fake runner, as elsewhere."""
import re

import pytest

from orch.cli import run as cli_run
from orch.core import epics, factory_close as fc, factory_release as fr, factory_report, ledger, permits, store
from orch.core.events import read_events
from orch.errors import HumanOnlyError, UsageError
from test_factory_production import ProdFake, _prod_recipe
from test_factory_release import Fake, _refine, _states  # noqa: F401
from test_factory_release import (_not_stopping, bin_dir, fa, fh, fws, ready, recipe, remote,  # noqa: F401
                                  switch)

pytestmark = pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")


@pytest.fixture
def closing(ready):
    """A Ready Dark epic whose charter signs `close` (and no release unless asked)."""
    def _make(release="none", **kw):
        charter = {"close": True, **kw.pop("charter", {})}
        return ready(release=release, charter=charter, **kw)
    return _make


def _epic(ws, eid):
    return store.load(ws, eid)[1]


def _view(ws, eid):
    e = _epic(ws, eid)
    return fc.view(ws, e, permits.factory_delegation(ws, e))


def _codes(ws, eid):
    return [b["code"] for b in _view(ws, eid)["blockers"]]


# -- the charter ----------------------------------------------------------------------------------------------------

def test_close_is_hashed_only_when_set_and_only_in_a_dark_charter(ws, aops):
    base = {"factory": True, "dark": True}
    assert "close" not in epics.normalize_delegate({**base, "close": False})
    assert epics.normalize_delegate({**base, "close": True})["close"] is True
    with pytest.raises(UsageError, match="--close goes with --dark"):
        epics.normalize_delegate({"factory": True, "close": True})
    e = aops.new("E", type="epic")
    epic = _epic(ws, e.id)
    for d in ({"factory": True}, base, {**base, "release": "dev"}):
        assert epics.charter(ws, epic, d)["hash"] == epics.charter(ws, epic, {**d, "close": False})["hash"]
    assert epics.charter(ws, epic, base)["hash"] != epics.charter(ws, epic, {**base, "close": True})["hash"]


def test_cli_close_needs_dark_and_says_it_replaces_the_verdict(fws, fa, capsys, switch):
    e = fa.new("E", type="epic")
    _refine(fa, e.id, plan=None)
    switch.human(e.id)
    assert cli_run(["approve", e.id, "requirements", "--factory", "--close"]) != 0
    assert "--close goes with --dark" in capsys.readouterr().err
    assert cli_run(["approve", e.id, "requirements", "--dark", "--close"]) == 0
    out = capsys.readouterr().out
    assert "closes the epic by itself when everything is proven, on what the agents wrote under the close rules" in out
    assert "nothing is executed or verified by the factory" in out and "nothing is deployed or run" in out
    assert "this replaces your verdict for this run" in out
    assert "Reopen stays yours" in out and "verdict stays yours" not in out
    assert epics.delegation(fws, _epic(fws, e.id))["close"] is True


# -- closing --------------------------------------------------------------------------------------------------------

def test_it_closes_once_through_the_human_verdict_path_marked_as_the_charter(fws, closing, human):
    eid, (c,), d = closing()
    rep = factory_report.ready(fws, _epic(fws, eid))
    assert _view(fws, eid) == {"closed": None, "by_charter": False, "blockers": [], "pending": True}
    assert fc.tick(fws, human) == [f"{eid}: closed by itself under its charter with {c}"]
    assert _epic(fws, eid).status == "done" and _epic(fws, c).status == "done"
    signed = ledger.entries(fws)
    for tid in (eid, c):
        last = ledger.status_chain(fws, tid, signed)[-1]
        assert last["kind"] == "verdict" and last["verdict"] == "done" and last["via"] == fc.CHARTER_VIA
        assert factory_report.finished(fws, _epic(fws, tid), signed, read_events(fws)) == "done"
    auto = [e for e in read_events(fws) if e.kind == "verdict.auto"]
    assert len(auto) == 1 and auto[0].data == {"children": [c], "seen": rep["seen"], "delegation": d["id"]} and auto[0].via == "dark-charter"
    assert fc.closed_by_charter(fws, _epic(fws, eid)) and fc.record(fws, eid, d["id"])["closed"] is True
    assert fc.tick(fws, human) == [] and len([e for e in read_events(fws) if e.kind == "verdict.auto"]) == 1


def test_it_closes_after_merge_dev_and_production_are_proven(fws, closing, human, remote):
    from orch.dashboard.factory_runner import release_once
    eid, _, _ = closing(release="prod", recipe=_prod_recipe(remote))
    fake = ProdFake()
    lines = release_once(fws, run=fake)
    assert lines[-1].startswith(f"{eid}: closed by itself") and lines[-2].endswith("production of " + eid + " proven")
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven", "production": "proven"}
    assert _epic(fws, eid).status == "done"


def test_it_waits_for_every_signed_release_stage(fws, closing, human, remote):
    eid, _, _ = closing(release="prod", recipe=_prod_recipe(remote))
    assert fc.tick(fws, human) == [] and _epic(fws, eid).status == "open"
    v = _view(fws, eid)
    assert v["pending"] and {b["code"] for b in v["blockers"]} == {"release"}
    fr._atomic(fr._window_path(fws), '{"at": "' + __import__("orch").clock.stamp_s() + '"}')  # window shut
    fr.tick(fws, human, ProdFake())
    assert _states(fws, eid)["production"] == "waiting" and fc.tick(fws, human) == []
    assert _epic(fws, eid).status == "open"


@pytest.mark.parametrize("what,code", [
    ("charter", "charter"), ("dark-off", "off"), ("factory-off", None), ("ledger", "ledger"), ("unarmed", "unarmed"),
    ("paused", "charter"), ("edited", "charter"), ("not-ready", "ready"), ("request", "request"),
    ("stopped", "stopped"), ("coverage", "coverage"), ("coverage-none", "coverage"), ("coverage-error", "coverage"),
    ("once", "once"), ("not-open", "status"), ("evidence", "evidence")])
def test_each_condition_alone_keeps_it_open(fws, closing, ready, fa, fh, human, monkeypatch, what, code):
    from orch.core.ops import Ops
    if what == "charter":
        eid, _, _ = ready(release="none")
    elif what == "unarmed":
        eid, _, _ = closing(arm=False)
    elif what == "stopped":
        eid, _, _ = closing(charter={"max_children": 1})  # one child used the whole child budget: Stopped
        assert [r["code"] for r in factory_report.stopped(fws, _epic(fws, eid))] == ["budget"]
    else:
        eid, (c,), d = closing()
    if what == "dark-off":
        Ops(fws, human).set_factory_dark(False)
    elif what == "factory-off":
        monkeypatch.setattr(permits, "enabled", lambda ws: False)
    elif what == "ledger":
        monkeypatch.setattr(ledger, "head_ok", lambda: False)
    elif what == "paused":
        fh.epic_pause(eid)
    elif what == "edited":
        fa.set_section(eid, "Requirements", "r, and something new")
    elif what == "not-ready":
        late = fa.new("late child", epic=eid)
        _refine(fa, late.id)
    elif what == "request":
        permits.request(fws, fa.actor, _epic(fws, c), "make other", source="agent")
    elif what == "coverage":
        monkeypatch.setattr(factory_report, "coverage_ok", lambda ws, e: False, raising=False)
    elif what == "coverage-none":  # an unknown (the epic names no file) is not ok
        monkeypatch.setattr(factory_report, "coverage_ok", lambda ws, e: None, raising=False)
    elif what == "coverage-error":
        monkeypatch.setattr(factory_report, "coverage_ok", lambda ws, e: 1 / 0, raising=False)
    elif what == "once":
        fr.fs._create(fc._marker(fws, eid, d["id"], "intent"), {"at": "x"})
    elif what == "not-open":  # an epic that is not open (here: done, by your own verdict) is never closed again
        rep = factory_report.ready(fws, _epic(fws, eid))
        fh.verdict(eid, "done", expected_hash=rep["seen"])
    elif what == "evidence":
        fa.set_section(c, "Verification", "- AC1: looks right to me")
    assert fc.tick(fws, human) == [] and not [e for e in read_events(fws) if e.kind == "verdict.auto"]
    assert _epic(fws, eid).status == ("done" if what == "not-open" else "open")
    if code is not None:
        e = _epic(fws, eid)
        assert [b["code"] for b in fc.blockers(fws, e, permits.factory_delegation(fws, e))] == [code], what


@pytest.mark.parametrize("what", ["stale", "sensitive", "cleared", "blocked"])
def test_each_release_condition_alone_keeps_it_open(fws, closing, human, recipe, bin_dir, what):
    from test_factory_release import _branch
    eid, (c,), _ = closing(release="dev", recipe=recipe,
                           **({"files": {".github/x.yml": "x\n"}} if what == "sensitive" else {}))
    fake = Fake()
    if what == "stale":
        fr.tick(fws, human, fake)
        _branch(fws.root, f"feat/{c.lower()}-work", {"src/more.py": "x\n"}, start=f"feat/{c.lower()}-work")
        fr.tick(fws, human, fake)
    elif what == "sensitive":
        fr.tick(fws, human, fake)
    elif what == "cleared":
        fr.clear_recipe(fws, human)
    elif what == "blocked":
        (bin_dir / "gh").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
        fr.tick(fws, human, fake)
    assert fc.tick(fws, human) == [] and _epic(fws, eid).status == "open"
    bl = _view(fws, eid)["blockers"]
    assert bl and not any(b["pending"] for b in bl if b["code"] != "release") and not _view(fws, eid)["pending"], bl
    want = {"stale": "Release out of date", "sensitive": "Sensitive path touched", "cleared": "recipe cannot be read",
            "blocked": "Release could not start"}[what]
    assert any(want in b["text"] for b in bl), bl


def test_the_coverage_shim_counts_once_coverage_exists(fws, closing, human, monkeypatch):
    eid, _, _ = closing()
    monkeypatch.setattr(factory_report, "coverage_ok", lambda ws, e: True, raising=False)
    assert fc.epic_coverage_ok(fws, _epic(fws, eid)) is True
    monkeypatch.delattr(factory_report, "coverage_ok")
    assert fc.epic_coverage_ok(fws, _epic(fws, eid)) is True  # absent: no coverage condition to hold
    assert fc.tick(fws, human) and _epic(fws, eid).status == "done"


def test_a_failed_release_stage_keeps_it_open(fws, closing, human, recipe):
    eid, _, _ = closing(release="dev", recipe=recipe)
    fake = Fake()
    fake.results["deploy-dev"] = {"code": 2}
    fr.tick(fws, human, fake)
    assert fc.tick(fws, human) == [] and _epic(fws, eid).status == "open"
    assert not _view(fws, eid)["pending"] and "stopped" in _codes(fws, eid)


def test_a_crash_after_the_intent_never_closes_twice(fws, closing, human, monkeypatch):
    from orch.core.ops import Ops
    eid, _, _ = closing()

    def boom(*a, **k):
        raise KeyboardInterrupt  # the dashboard dies between the intent marker and the verdict
    with monkeypatch.context() as m:
        m.setattr(Ops, "verdict", boom)
        with pytest.raises(KeyboardInterrupt):
            fc.tick(fws, human)
    assert fc.tick(fws, human) == [] and _epic(fws, eid).status == "open"
    assert "once" in _codes(fws, eid)


def test_a_crash_after_the_children_closed_leaves_the_epic_to_the_human(fws, closing, human, monkeypatch):
    """The real verdict path: the children are closed (all of them, under their locks), then the dashboard dies
    before the epic's own verdict is written."""
    from orch.core.ops import Ops
    eid, (c0, c1), _ = closing(kids=2)
    real = Ops._close_children

    def then_die(self, *a, **k):
        real(self, *a, **k)
        raise KeyboardInterrupt
    with monkeypatch.context() as m:
        m.setattr(Ops, "_close_children", then_die)
        with pytest.raises(KeyboardInterrupt):
            fc.tick(fws, human)
    fr.release_lock(fws)
    assert _epic(fws, c0).status == _epic(fws, c1).status == "done" and _epic(fws, eid).status == "open"
    assert fc.tick(fws, human) == [] and _epic(fws, eid).status == "open"  # never a second try
    assert fc.record(fws, eid, permits.factory_delegation(fws, _epic(fws, eid))["id"]) is None
    pytest.importorskip("fastapi")
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Every child is done, so there is no Ready report" in html and "Close the epic" in html
    r = _client(fws).post(f"/factory/{eid}/close", data={"reason": "checked by hand"}, follow_redirects=False)
    assert "err=" not in r.headers["location"] and _epic(fws, eid).status == "done"


def test_a_change_between_ready_and_the_verdict_refuses_it_and_records_why(fws, closing, human, monkeypatch, fa):
    eid, (c,), _ = closing()
    real = factory_report.ready

    def stale_ready(ws, epic, **kw):  # the report shows evidence that is no longer what the children hold
        rep = real(ws, epic, **kw)
        return {**rep, "seen": "sha256:" + "0" * 64} if rep else rep
    monkeypatch.setattr(factory_report, "ready", stale_ready)
    lines = fc.tick(fws, human)
    assert "not closed by itself" in lines[0] and _epic(fws, eid).status == "open"


def test_never_for_an_agent_or_under_an_agent_harness(fws, closing, human, agent, monkeypatch):
    eid, _, _ = closing()
    with pytest.raises(HumanOnlyError):
        fc.tick(fws, agent)
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    with pytest.raises(HumanOnlyError):
        fc.tick(fws, human)
    with pytest.raises(HumanOnlyError):
        fc.close_once(fws, eid, permits.factory_delegation(fws, _epic(fws, eid))["id"])
    monkeypatch.delenv("ORCH_HARNESS")
    assert _epic(fws, eid).status == "open"


def test_orch_check_shows_the_close_as_a_delegated_decision(fws, closing, human):
    from orch.core.check import run_checks
    eid, (c,), _ = closing()
    fc.tick(fws, human)
    found = run_checks(fws, emit_events=False)
    mine = sorted((f.level, f.code, f.ticket) for f in found if f.ticket in (eid, c))
    assert mine == sorted([("info", "charter-verdict", eid), ("info", "charter-verdict", c),
                           ("info", "delegated-approval", c), ("info", "delegated-approval", c)])
    assert "delegated in that charter" in next(f.message for f in found if f.code == "charter-verdict")
    signed = ledger.entries(fws)
    did = permits.factory_delegation(fws, _epic(fws, eid))["id"]
    assert all(ledger.status_chain(fws, t, signed)[-1]["delegation"] == did for t in (eid, c))
    assert [e.data["delegation"] for e in read_events(fws) if e.kind == "verdict.auto"] == [did]


def test_orch_check_judges_the_close_against_the_charter_that_gave_it(fws, closing, human, fh):
    """Reopened and approved again without close: the earlier close is still the earlier charter's, info; a verdict
    entry naming a charter that does not sign close is a warning."""
    from orch.core.check import run_checks
    from orch.core.ops import Ops
    eid, (c,), d = closing()
    fc.tick(fws, human)
    Ops(fws, human).epic_pause(eid)
    Ops(fws, human).reopen(eid, "not done")
    fh.approve(eid, "requirements", delegate={"factory": True, "dark": True},
               expected_hash=epics.charter(fws, _epic(fws, eid))["content_hash"])
    assert "close" not in permits.factory_delegation(fws, _epic(fws, eid))
    codes = {(f.code, f.ticket) for f in run_checks(fws, emit_events=False)}
    assert ("charter-verdict", c) in codes and ("charter-verdict-unbacked", c) not in codes
    # the close record of that charter is gone: the child's charter verdict is no longer backed
    fc._marker(fws, eid, d["id"], "outcome").unlink()
    codes = {(f.code, f.ticket) for f in run_checks(fws, emit_events=False)}
    assert ("charter-verdict-unbacked", c) in codes


def test_the_close_records_are_guarded(ws):
    from orch.hooks.guard import evaluate
    base = ledger.base_dir()
    cmd = f"rm {base}/permits/release-records/x/close.sha256_x.intent"
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)}).allow
    assert permits.never_grantable(ws, cmd) is not None
    assert str(fc._marker(ws, "L-1", "sha256:x", "intent")).startswith(str(base / "permits" / "release-records"))


# -- the dashboard --------------------------------------------------------------------------------------------------

def _client(ws):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def test_ready_card_says_it_closes_by_itself_instead_of_accept(fws, closing, human, recipe):
    pytest.importorskip("fastapi")
    eid, _, _ = closing(release="dev", recipe=recipe)
    html = _client(fws).get(f"/factory/{eid}").text
    card = html[html.index(f'data-ready="{eid}"'):]
    card = card[:card.index("</article>")]
    assert "data-auto-ready" in card and "Your charter closes it by itself, in place of your verdict, when:" in card
    assert "the merge stage is proven by its check" in card and "Accept the epic" in card  # Accept stays yours
    assert "nothing is executed or verified by the factory" in card
    assert "Closing by itself" in html and "It closes by itself when everything is proven" in html


def test_a_condition_that_needs_you_offers_accept_and_says_why(fws, closing, human, fa):
    pytest.importorskip("fastapi")
    eid, (c,), _ = closing()
    permits.request(fws, fa.actor, _epic(fws, c), "make other", source="agent")
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Not closed by itself: a permission card of this epic is open" in html and "Accept the epic" in html


def test_run_view_after_the_close_and_the_humans_reopen(fws, closing, human):
    pytest.importorskip("fastapi")
    eid, (c,), _ = closing()
    fc.tick(fws, human)
    cl = _client(fws)
    html = cl.get(f"/factory/{eid}").text
    assert "Closed by itself under your charter" in html and "data-auto-closed" in html and "Reopen" in html
    assert "data-auto-summary" in html and "1 of 1 criteria" in html
    assert "The runner gave the done verdict by itself under your charter" in html
    r = cl.post(f"/factory/{eid}/reopen", data={"reason": "not done"}, headers={"origin": "http://evil.example"},
                follow_redirects=False)
    assert r.status_code == 403 and _epic(fws, eid).status == "done"
    r = cl.post(f"/factory/{eid}/reopen", data={"reason": ""}, follow_redirects=False)
    assert "err=" in r.headers["location"] and _epic(fws, eid).status == "done"
    assert "stop its run (the delegation is paused)" in html
    r = cl.post(f"/factory/{eid}/reopen", data={"reason": "not done yet"}, follow_redirects=False)
    assert "err=" not in r.headers["location"] and _epic(fws, eid).status == "open"
    assert epics.delegation(fws, _epic(fws, eid))["paused"]  # the runner does nothing more under that charter
    assert fc.tick(fws, human) == [] and _codes(fws, eid) == ["charter"]  # paused: never again by itself
    html = cl.get(f"/factory/{eid}").text
    assert "Not closed by itself: the charter is paused" in html
    d = epics.delegation(fws, _epic(fws, eid))  # and the once-marker of that charter holds as well
    assert __import__("os").path.lexists(fc._marker(fws, eid, d["id"], "intent"))


def test_reopen_route_refuses_an_epic_the_charter_did_not_close(fws, ready, human, monkeypatch):
    pytest.importorskip("fastapi")
    eid, _, _ = ready(release="none")
    r = _client(fws).post(f"/factory/{eid}/reopen", data={"reason": "x"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and _epic(fws, eid).status == "open"


def _new(c, **over):
    once = re.search(r'name="once" value="([^"]+)"', c.get("/new").text).group(1)
    data = {"title": "Export", "mode": "dark", "ask": "Build the export.", "done_when": "It exports.", "size": "m",
            "priority": "normal", "once": once, "confirm_dark": "dark", **over}
    return c.post("/new", data=data, follow_redirects=False)


def test_start_forms_sign_close_only_in_dark_mode_and_say_what_it_means(fws, fa, human):
    pytest.importorskip("fastapi")
    c = _client(fws)
    html = c.get("/new").text
    box = html[html.index("data-close-choice"):]
    assert re.search(r'name="close" value="1">', box) and "checked" not in box[:box.index("</label>")]
    assert "This replaces your verdict for this run" in box and "Reopen stays yours" in box
    assert html.rindex("only-dark", 0, html.index("data-close-choice")) > html.index('id="confirm_dark"')
    r = _new(c, close="1", confirm_dark="")
    assert r.status_code == 422 and "Type dark" in r.text
    r = _new(c, close="1")
    eid = r.headers["location"].split("/factory/")[1].split("?")[0]
    assert epics.delegation(fws, _epic(fws, eid))["close"] is True
    n = len(list(store.scan(fws)))
    for over in ({"mode": "factory", "close": "1"}, {"mode": "ticket", "close": "1"},
                 {"mode": "factory", "rollback": "1"}):
        r = _new(c, **over)
        assert r.status_code == 422 and "Only a Dark AI Factory" in r.text and len(list(store.scan(fws))) == n
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    assert "data-close-choice" in c.get(f"/t/{e.id}").text
    seen = epics.charter(fws, _epic(fws, e.id))["content_hash"]
    r = c.post(f"/t/{e.id}/approve", data={"gate": "requirements", "seen": seen, "start": "factory", "close": "1"},
               follow_redirects=False)
    assert "only a Dark AI Factory" in r.headers["location"].replace("+", " ") and epics.delegation(
        fws, _epic(fws, e.id)) is None
    r = c.post(f"/t/{e.id}/approve", data={"gate": "requirements", "seen": seen, "start": "dark",
                                           "confirm_dark": "dark", "close": "1"}, follow_redirects=False)
    assert "err=" not in r.headers["location"] and epics.delegation(fws, _epic(fws, e.id))["close"] is True


def test_a_release_that_cannot_start_is_never_shown_as_pending_and_accept_stays(fws, closing, human, bin_dir):
    """The review's probe P5: a pinned program changed after signing. The merge never starts; that is a Stopped reason
    the human fixes, never a close promised for later."""
    eid, _, _ = closing(release="merge")
    (bin_dir / "gh").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    for _ in range(3):
        fr.tick(fws, human, Fake())
    v = _view(fws, eid)
    assert not v["pending"] and "stopped" in [b["code"] for b in v["blockers"]]
    assert [r["code"] for r in factory_report.stopped(fws, _epic(fws, eid))] == ["release-blocked"]
    pytest.importorskip("fastapi")
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Not closed by itself: it is Stopped: Release could not start" in html and "Accept the epic" in html


# -- orch check after a close: what may turn an unsigned-decision warning into info, and what may not -----------------

def _findings(fws, tid):
    from orch.core.check import run_checks
    return {(f.level, f.code) for f in run_checks(fws, emit_events=False) if f.ticket == tid}


def _rewrite_events(fws, change):
    import json
    p = fws.state_dir / "events.jsonl"
    lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    p.write_text("".join(json.dumps(e) + "\n" for e in change(lines)), encoding="utf-8")


@pytest.mark.parametrize("break_it", ["none", "event-missing", "event-other-epic", "charter-unsigned",
                                      "child-outside-epic"])
def test_delegated_approvals_of_a_closed_child_stay_info_only_when_everything_backs_them(fws, closing, human, fa,
                                                                                       monkeypatch, break_it):
    eid, (c,), _ = closing()
    fc.tick(fws, human)
    assert _epic(fws, c).status == "done"
    if break_it == "event-missing":
        _rewrite_events(fws, lambda ev: [e for e in ev if not (e["kind"] == "gate.delegated" and e["ticket"] == c)])
    elif break_it == "event-other-epic":
        other = fa.new("Another epic", type="epic")

        def move(ev):
            for e in ev:
                if e["kind"] == "gate.delegated" and e["ticket"] == c:
                    e["data"]["epic"] = other.id
            return ev
        _rewrite_events(fws, move)
    elif break_it == "charter-unsigned":
        real = ledger.entries
        monkeypatch.setattr(ledger, "entries", lambda ws, *a, **k: [e for e in real(ws, *a, **k)
                                                                     if e.get("kind") != "charter"])
    elif break_it == "child-outside-epic":
        other = fa.new("Another epic", type="epic")
        path, t = store.load(fws, c)
        t.meta["parent"] = other.id
        store.save(fws, t, path)
    found = _findings(fws, c)
    if break_it == "none":
        assert ("info", "delegated-approval") in found and ("warning", "unsigned-decision") not in found
    else:
        assert ("warning", "unsigned-decision") in found, (break_it, found)


def test_an_open_child_with_an_ended_delegation_keeps_the_warning(fws, closing, human, fh):
    """A paused delegation (not a verdict) on a child that is not done: the warning stays, as before."""
    eid, (c,), _ = closing()
    fh.set_section(eid, "Requirements", "r, changed by you")  # suspends the delegation
    assert ("warning", "unsigned-decision") in _findings(fws, c)


def test_a_charter_verdict_without_its_close_record_is_a_warning(fws, closing, human):
    eid, (c,), d = closing()
    fc.tick(fws, human)
    fc._marker(fws, eid, d["id"], "outcome").unlink()
    assert ("warning", "charter-verdict-unbacked") in _findings(fws, eid)
    assert ("info", "charter-verdict") not in _findings(fws, eid)

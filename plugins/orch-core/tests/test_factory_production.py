"""Dark AI Factory: the production stage of the release recipe, its release window and the signed rollback.

As in test_factory_release.py, the runner's own git runs for real against temporary repositories (no network) and a
fake runner stands in for the recipe's commands: no test runs a real deploy, rollback or live check."""
import json
import os
import re
from datetime import timedelta

import pytest

from orch import clock
from orch.cli import run as cli_run
from orch.core import epics, factory_release as fr, ledger, permits, store
from orch.core.events import read_events
from orch.errors import HumanOnlyError, UsageError, ValidationError
from test_factory_release import (DEV, MERGE, PROD, ROLLBACK, Fake, _branch, _g, _recipe, _refine, _stage, _work,  # noqa: F401,E501
                                  _states, _stopped)
from test_factory_release import (_not_stopping, bin_dir, fa, fh, fws, ready, recipe, remote,  # noqa: F401
                                  switch)

pytestmark = pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")


class ProdFake(Fake):
    """The live check prints "live <sha>" while `live`; the rollback's check prints "ok" while `healthy`; `codes`
    makes a command (by its second word) exit with that code."""
    def __init__(self):
        super().__init__()
        self.live, self.healthy, self.codes = True, True, {}

    def __call__(self, argv, cwd, env, timeout, started=None):
        word, key = (argv[1] if len(argv) > 1 else ""), " ".join(argv[1:3])
        if word == "prod-live":
            self.results[key] = {"out": f"live {argv[2]}\n" if self.live else "dead <script>x</script>\n"}
        elif word == "prod-health":
            self.results[key] = {"out": "ok\n" if self.healthy else "still broken\n"}
        elif word in self.codes:
            self.results[key] = {"code": self.codes[word]}
        return super().__call__(argv, cwd, env, timeout, started)


def _prod_recipe(remote, rollback=True, hours=None):
    prod = {**PROD, **({"rollback": ROLLBACK} if rollback else {}),
            **({"window": {"min_hours_since_last": hours}} if hours else {})}
    return _recipe(remote, stages=[MERGE, DEV, prod])


@pytest.fixture
def prod(ready, remote):
    def _make(rollback=True, signed_rollback=False, hours=None, **kw):
        return ready(release="prod", recipe=_prod_recipe(remote, rollback, hours),
                     charter={"rollback": True} if signed_rollback else None, **kw)
    return _make


def _at(fws, hours_ago: float) -> None:
    """The runner's record of the last proven production, as if it was written `hours_ago`."""
    fr._atomic(fr._window_path(fws), json.dumps({"at": clock.stamp_s(clock.now() - timedelta(hours=hours_ago)),
                                                 "epic": "X-1"}))


def _later(monkeypatch, hours: float) -> None:
    """The fake clock: `hours` from now (for the release window)."""
    real = clock.now
    monkeypatch.setattr(clock, "now", lambda: real() + timedelta(hours=hours))


def _prod(fws, eid):
    st = fr.status(fws, store.load(fws, eid)[1], permits.factory_delegation(fws, store.load(fws, eid)[1]))
    return next(s for s in st["stages"] if s["name"] == "production")


# -- the recipe and the charter -----------------------------------------------------------------------------------

def test_a_recipe_with_production_validates_and_pins_its_rollback_programs(ws, remote, bin_dir):
    (bin_dir / "rollbacker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "rollbacker").chmod(0o755)
    rb = {"commands": [["rollbacker", "{epic}"]], "check": {"argv": ["rollbacker", "check"], "expect": "ok"}}
    rec = fr.check_recipe(_recipe(remote, stages=[MERGE, DEV, {**PROD, "rollback": rb}]), ws)
    p = rec["stages"][2]
    assert p["name"] == "production" and p["per"] == "epic" and p["window_hours"] == fr.DEFAULT_WINDOW == 20
    assert p["rollback"]["commands"] == [["rollbacker", "{epic}"]] and "rollbacker" in fr.programs_of(rec)
    assert "rollbacker" in fr.pin_programs(rec)
    rec = fr.check_recipe(_recipe(remote, stages=[MERGE, DEV, {**PROD, "window": {"min_hours_since_last": 3}}]), ws)
    assert rec["stages"][2]["window_hours"] == 3 and rec["stages"][2]["rollback"] is None
    assert [s["name"] for s in fr.up_to(rec, "prod")] == ["merge", "dev", "production"]
    assert fr.up_to(fr.check_recipe(_recipe(remote), ws), "prod") is None


def test_rollback_and_prod_are_hashed_only_when_set(ws, aops):
    base = {"factory": True, "dark": True}
    assert epics.normalize_delegate({**base, "release": "prod"}) == {**epics.normalize_delegate(base), "release": "prod"}
    assert "rollback" not in epics.normalize_delegate({**base, "release": "prod", "rollback": False})
    assert epics.normalize_delegate({**base, "release": "prod", "rollback": True})["rollback"] is True
    with pytest.raises(UsageError, match="--release prod"):
        epics.normalize_delegate({**base, "release": "dev", "rollback": True})
    with pytest.raises(UsageError, match="--release prod"):
        epics.normalize_delegate({**base, "rollback": True})
    e = aops.new("E", type="epic")
    epic = store.load(ws, e.id)[1]
    dev = {**base, "release": "dev"}
    assert epics.charter(ws, epic, dev)["hash"] == epics.charter(ws, epic, {**dev, "rollback": False})["hash"]
    assert epics.charter(ws, epic, {**base, "release": "prod"})["hash"] != epics.charter(
        ws, epic, {**base, "release": "prod", "rollback": True})["hash"]


def test_approve_prod_needs_all_three_stages_and_rollback_needs_one_in_the_recipe(fws, fa, fh, human, remote):
    e = fa.new("E", type="epic")
    _refine(fa, e.id, plan=None)
    fr.set_recipe(fws, human, _recipe(remote))
    with pytest.raises(ValidationError, match="up to production"):
        fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "prod"})
    fr.set_recipe(fws, human, _prod_recipe(remote, rollback=False))
    with pytest.raises(ValidationError, match="has no rollback"):
        fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "prod", "rollback": True})
    fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "prod"})
    d = epics.delegation(fws, store.load(fws, e.id)[1])
    assert d["release"] == "prod" and "rollback" not in d


def test_cli_approve_prod_says_what_runs(fws, fa, capsys, switch, human, remote):
    e = fa.new("E", type="epic")
    _refine(fa, e.id, plan=None)
    fr.set_recipe(fws, human, _prod_recipe(remote))
    switch.human(e.id)
    assert cli_run(["approve", e.id, "requirements", "--dark", "--release", "dev", "--rollback"]) != 0
    assert "--rollback goes with --release prod" in capsys.readouterr().err
    assert cli_run(["approve", e.id, "requirements", "--dark", "--release", "prod", "--rollback"]) == 0
    out = capsys.readouterr().out
    assert "releases to production by itself using the recipe on this machine" in out
    assert "waits for the release window" in out and "runs the recipe's rollback when the production check fails" in out
    assert epics.delegation(fws, store.load(fws, e.id)[1])["rollback"] is True


# -- the executor -------------------------------------------------------------------------------------------------

def test_happy_path_merge_dev_then_production_on_the_commit_dev_was_proven_on(fws, prod, human, remote):
    eid, (c,), _ = prod()
    fake = ProdFake()
    lines = fr.tick(fws, human, fake)
    assert lines == [f"{eid}: merge of {c} proven", f"{eid}: dev of {eid} proven", f"{eid}: production of {eid} proven"]
    dev = fr.unit_state(fws, eid, "dev", eid)
    assert fake.ran()[-2:] == [f"make deploy-prod {dev['base_sha']}", f"make prod-live {dev['base_sha']}"]
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven", "production": "proven"}
    assert _stopped(fws, eid) == [] and not fr.window(fws, 20)["open"]  # the window's clock started now
    ev = [e.data for e in read_events(fws) if e.kind == "release.stage"]
    assert ev[-1] == {"stage": "production", "child": eid, "proven": True, "exit": 0}
    assert fr.tick(fws, human, fake) == [] and len(fake.ran()) == 7  # nothing runs twice


def test_a_closed_window_is_waiting_not_stopped_and_opens_with_the_clock(fws, prod, human, monkeypatch):
    _at(fws, 5)  # another epic went to production 5 hours ago
    eid, _, _ = prod()
    fake = ProdFake()
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven", "production": "waiting"}
    p = _prod(fws, eid)
    assert p["window"]["open"] is False and p["window"]["hours"] == 20
    assert clock.parse_stamp(p["window"]["opens"]) - clock.parse_stamp(p["window"]["last"]) == timedelta(hours=20)
    assert _stopped(fws, eid) == [] and not any("prod" in x for x in fake.ran())
    assert fr.tick(fws, human, fake) == []  # re-checked each round; nothing runs, nothing is said
    real = clock.now
    monkeypatch.setattr(clock, "now", lambda: real() + timedelta(hours=16))  # the fake clock: past the window
    assert fr.window(fws, 20)["open"] is True
    assert fr.tick(fws, human, fake) == [f"{eid}: production of {eid} proven"]


def test_the_recipe_sets_the_window_length(fws, prod, human):
    _at(fws, 5)
    eid, _, _ = prod(hours=4)
    fr.tick(fws, human, ProdFake())
    assert _states(fws, eid)["production"] == "proven"


def test_a_damaged_window_record_keeps_the_window_shut(fws, prod, human):
    fr._atomic(fr._window_path(fws), "{nope")
    eid, _, _ = prod()
    fr.tick(fws, human, ProdFake())
    p = _prod(fws, eid)
    assert p["state"] == "waiting" and p["window"]["open"] is False and "cannot be read" in p["window"]["why"]


def test_a_failed_live_check_without_a_signed_rollback_stops_and_rolls_nothing_back(fws, prod, human):
    eid, _, _ = prod(signed_rollback=False)
    fake = ProdFake()
    fake.live = False
    lines = fr.tick(fws, human, fake)
    assert "production of" in lines[-1] and "failed" in lines[-1] and "rollback" not in " ".join(fake.ran())
    assert _stopped(fws, eid) == ["production-failed"]
    assert not fr.window(fws, 20)["open"]  # the clock started when production's commands began
    assert fr.tick(fws, human, fake) == []  # no automatic retry of production


def test_a_failed_live_check_with_a_signed_rollback_runs_it_once(fws, prod, human):
    eid, _, _ = prod(signed_rollback=True)
    fake = ProdFake()
    fake.live = False
    lines = fr.tick(fws, human, fake)
    assert lines[-1].endswith("; rolled back")
    assert fake.ran()[-2:] == [f"make rollback-prod {eid}", "make prod-health"]
    assert _stopped(fws, eid) == ["rolled-back"]
    assert fr.unit_state(fws, eid, "production", eid)["rollback"]["state"] == "rolled back"
    ev = [e.data for e in read_events(fws) if e.kind == "release.stage"]
    assert ev[-1] == {"stage": "rollback", "child": eid, "proven": True, "exit": 0}
    assert fr.tick(fws, human, fake) == [] and fake.ran().count(f"make rollback-prod {eid}") == 1


def test_a_failed_rollback_stops_with_its_own_reason(fws, prod, human):
    eid, _, _ = prod(signed_rollback=True)
    fake = ProdFake()
    fake.live, fake.healthy = False, False
    assert "rollback failed" in fr.tick(fws, human, fake)[-1]
    assert _stopped(fws, eid) == ["rollback-failed"]


def test_a_failed_production_command_never_rolls_back(fws, prod, human):
    eid, _, _ = prod(signed_rollback=True)
    fake = ProdFake()
    fake.codes["deploy-prod"] = 3
    fr.tick(fws, human, fake)
    assert "rollback" not in " ".join(fake.ran()) and _stopped(fws, eid) == ["production-failed"]


def test_a_signed_rollback_without_a_recipe_rollback_cannot_start_production(fws, prod, human, remote):
    eid, _, _ = prod(signed_rollback=True)
    fr.set_recipe(fws, human, _prod_recipe(remote, rollback=False))  # the human removed it after signing
    fake = ProdFake()
    fake.live = False
    lines = fr.tick(fws, human, fake)
    assert _stopped(fws, eid) == ["rollback-missing"] and "not started" in lines[-1]
    assert not any("prod" in x for x in fake.ran())  # production's commands never ran
    assert fr.window(fws, 20)["open"] and _states(fws, eid)["production"] == "waiting"
    reason = fr.status(fws, store.load(fws, eid)[1], permits.factory_delegation(fws, store.load(fws, eid)[1]))
    assert "signs a rollback, but the recipe's production stage has none" in reason["reasons"][0]["text"]
    assert fr.tick(fws, human, fake) == []  # a Stopped reason: nothing is tried again until you retry
    fr.set_recipe(fws, human, _prod_recipe(remote))
    fr.retry(fws, human, eid, "production", eid)
    assert _stopped(fws, eid) == [] and fr.tick(fws, human, fake)[-1].endswith("; rolled back")


def test_an_unknown_production_or_rollback_outcome_is_never_run_again(fws, prod, human, monkeypatch):
    eid, _, _ = prod(signed_rollback=True)
    fake = ProdFake()

    def crash(argv):
        if "deploy-prod" in argv:
            raise KeyboardInterrupt
    fake.on_call = crash
    with pytest.raises(KeyboardInterrupt):
        fr.tick(fws, human, fake)
    fake.on_call = None
    assert _states(fws, eid)["production"] == "unknown" and _stopped(fws, eid) == ["release-unknown"]
    assert fr.tick(fws, human, fake) == [] and fake.ran().count(next(x for x in fake.ran() if "deploy-prod" in x)) == 1
    # a rollback that started and has no outcome is a reason of its own, never run again
    fr.retry(fws, human, eid, "production", eid)
    fake.live = False
    _later(monkeypatch, 21)  # past the window the first attempt started

    def crash_rollback(argv):
        if "rollback-prod" in argv:
            raise KeyboardInterrupt
    fake.on_call = crash_rollback
    with pytest.raises(KeyboardInterrupt):
        fr.tick(fws, human, fake)
    fake.on_call = None
    fr.release_lock(fws)
    assert _stopped(fws, eid) == ["rollback-failed"] and fr.tick(fws, human, fake) == []


def test_retry_of_production_is_human_only_and_allows_one_attempt(fws, prod, human, agent, monkeypatch):
    eid, _, _ = prod()
    fake = ProdFake()
    fake.live = False
    fr.tick(fws, human, fake)
    with pytest.raises(HumanOnlyError):
        fr.retry(fws, agent, eid, "production", eid)
    fr.retry(fws, human, eid, "production", eid)
    with pytest.raises(ValidationError):
        fr.retry(fws, human, eid, "production", eid)
    fake.live = True
    assert fr.tick(fws, human, fake) == [] and _states(fws, eid)["production"] == "waiting"  # the window
    _later(monkeypatch, 21)
    assert fr.tick(fws, human, fake) == [f"{eid}: production of {eid} proven"]
    assert fake.ran().count(next(x for x in fake.ran() if "deploy-prod" in x)) == 2


def test_a_changed_merge_makes_dev_and_production_stale_and_production_waits(fws, prod, human):
    eid, (c,), _ = prod()
    fake = ProdFake()
    fr.tick(fws, human, fake)
    _branch(fws.root, f"feat/{c.lower()}-work", {"src/more.py": "more\n"}, start=f"feat/{c.lower()}-work")
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "stale", "dev": "stale", "production": "stale"}
    fr.retry(fws, human, eid, "merge", c)
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "stale", "production": "stale"}  # nothing ran for it
    n = len(fake.ran())
    fr.retry(fws, human, eid, "dev", eid)
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["dev"] == "proven" and _states(fws, eid)["production"] == "stale"
    assert "changed after production was proven" in _prod(fws, eid)["units"][0]["why"]
    assert not any("deploy-prod" in x for x in fake.ran()[n:])


def test_a_stale_dev_never_lets_production_run(fws, prod, fa, human, close_tasks, monkeypatch):
    _at(fws, 1)
    eid, _, _ = prod()
    fake = ProdFake()
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["production"] == "waiting"
    c = fa.new("late child", epic=eid)
    _refine(fa, c.id)
    fa.epic_auto_approve(c.id)
    _work(fa, fws.root, c.id, f"feat/{c.id.lower()}-work", {"src/late.py": "x\n"})
    fa.claim(c.id)
    close_tasks(fa, c.id)
    fa.set_section(c.id, "Verification", "- AC1: ran `pytest -q` on the branch, 3 passed")
    fa.move(c.id, "testing")
    real = clock.now
    monkeypatch.setattr(clock, "now", lambda: real() + timedelta(hours=30))
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["dev"] == "stale" and _states(fws, eid)["production"] == "waiting"
    assert not any("deploy-prod" in x for x in fake.ran())


def test_production_waits_while_another_release_holds_the_lock(fws, prod, human):
    eid, _, _ = prod()
    fake = ProdFake()
    assert fr.acquire(fws, "X-9", 60)  # another epic's release holds the workspace's lock
    try:
        assert fr.tick(fws, human, fake) == [f"{eid}: the release waits: another release holds the lock"]
        assert fake.calls == []
    finally:
        fr.release_lock(fws)
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["production"] == "proven"


def test_the_window_and_rollback_records_are_guarded(ws):
    from orch.hooks.guard import evaluate
    base = ledger.base_dir()
    for cmd in (f"cat {base}/permits/release-records/production-last-0.json",
                f"rm {base}/permits/release-records/x/production.L-1.1.rollback",
                f"echo x > {base}/permits/release-records/x/production.L-1.1.rollback-intent"):
        assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)}).allow
        assert permits.never_grantable(ws, cmd) is not None
    assert str(fr._window_path(ws)).startswith(str(base / "permits" / "release-records"))
    for tool in ("Write", "Edit"):
        assert not evaluate(ws, {"tool_name": tool, "tool_input": {"file_path": str(fr._window_path(ws)),
                                                                    "content": "{}", "old_string": "a",
                                                                    "new_string": "b"}, "cwd": str(ws.root)}).allow


# -- the dashboard ------------------------------------------------------------------------------------------------

def _client(ws):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _run(ws, eid):
    from orch.dashboard.data import factory as factory_data
    return factory_data.run_view(ws, store.load(ws, eid)[1])


def test_ring_gets_production_lit_only_from_its_proven_record(fws, prod, human):
    pytest.importorskip("fastapi")
    _at(fws, 1)
    eid, _, _ = prod()
    fr.tick(fws, human, ProdFake())
    run = _run(fws, eid)
    marks = dict(zip(run["names"], run["marks"]))
    assert run["names"] == ["Understand", "Plan", "Build", "Evidence", "Merge", "Dev", "Production", "Done"]
    assert marks["Dev"] == "done" and marks["Production"] != "done" and run["state"] == "window"
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Production waits for its release window" in html and "data-window" in html and "it opens at" in html
    assert "Stopped" not in re.sub(r"<[^>]+>", "", html.split('id="run-status"')[1].split("</h2>")[0])


def test_run_view_shows_the_rollback_and_escapes_the_output(fws, prod, human):
    pytest.importorskip("fastapi")
    eid, _, _ = prod(signed_rollback=True)
    fake = ProdFake()
    fake.live = False
    fr.tick(fws, human, fake)
    html = _client(fws).get(f"/factory/{eid}").text
    assert "Production rolled back" in html and 'data-rollback="rolled back"' in html and "Retry release" in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html and "<script>x</script>" not in html


def _new(c, **over):
    once = re.search(r'name="once" value="([^"]+)"', c.get("/new").text).group(1)
    data = {"title": "Export", "mode": "dark", "ask": "Build the export.", "done_when": "It exports.", "size": "m",
            "priority": "normal", "once": once, "confirm_dark": "dark", **over}
    return c.post("/new", data=data, follow_redirects=False)


def test_new_ticket_offers_production_only_with_all_three_stages(fws, human, remote):
    pytest.importorskip("fastapi")
    fr.set_recipe(fws, human, _recipe(remote))
    html = _client(fws).get("/new").text
    box = html[html.index("data-release-choice"):html.index("</fieldset>", html.index("data-release-choice"))]
    assert re.search(r'value="prod"[^>]*disabled', box) and "data-prod-off" in box
    fr.set_recipe(fws, human, _prod_recipe(remote, rollback=False))
    box = _client(fws).get("/new").text
    assert not re.search(r'value="prod"[^>]*disabled', box) and re.search(r'name="rollback"[^>]*disabled', box)
    assert "data-rollback-off" in box and "Roll back production by itself if its check fails" in box
    fr.set_recipe(fws, human, _prod_recipe(remote))
    box = _client(fws).get("/new").text
    assert not re.search(r'name="rollback"[^>]*disabled', box) and 'name="confirm_production"' in box


def test_new_ticket_production_needs_its_typed_word_and_signs_what_was_chosen(fws, human, remote):
    pytest.importorskip("fastapi")
    fr.set_recipe(fws, human, _prod_recipe(remote))
    c = _client(fws)
    n = len(list(store.scan(fws)))
    for over, why in (({"release": "prod"}, "Type production"), ({"release": "prod", "confirm_production": "prod"},
                                                                 "Type production"),
                      ({"release": "dev", "rollback": "1"}, "only with Release up to Production"),
                      ({"release": "prod", "confirm_production": "production", "confirm_dark": ""}, "Type dark")):
        r = _new(c, **over)
        assert r.status_code == 422 and why in r.text and len(list(store.scan(fws))) == n, over
    r = _new(c, release="prod", confirm_production="production", rollback="1")
    eid = r.headers["location"].split("/factory/")[1].split("?")[0]
    d = epics.delegation(fws, store.load(fws, eid)[1])
    assert d["release"] == "prod" and d["rollback"] is True
    r = _new(c, release="prod", confirm_production="production")
    eid = r.headers["location"].split("/factory/")[1].split("?")[0]
    assert "rollback" not in epics.delegation(fws, store.load(fws, eid)[1])


def test_epic_page_production_start_needs_the_typed_word(fws, fa, human, remote):
    pytest.importorskip("fastapi")
    fr.set_recipe(fws, human, _prod_recipe(remote))
    e = fa.new("Epic", type="epic")
    _refine(fa, e.id, plan=None)
    c = _client(fws)
    seen = epics.charter(fws, store.load(fws, e.id)[1])["content_hash"]
    data = {"gate": "requirements", "seen": seen, "start": "dark", "confirm_dark": "dark", "release": "prod"}
    r = c.post(f"/t/{e.id}/approve", data=data, follow_redirects=False)
    assert "err=" in r.headers["location"] and epics.delegation(fws, store.load(fws, e.id)[1]) is None
    r = c.post(f"/t/{e.id}/approve", data={**data, "confirm_production": "production"}, follow_redirects=False)
    assert "err=" not in r.headers["location"]
    assert epics.delegation(fws, store.load(fws, e.id)[1])["release"] == "prod"


# -- review fixes: blocked starts, the workspace-wide production hold, the window's sources, done children -----------

def test_a_production_that_cannot_resolve_holds_every_other_epics_production(fws, prod, human, monkeypatch):
    a, _, _ = prod(signed_rollback=True)
    fake = ProdFake()
    fake.live, fake.healthy = False, False
    fr.tick(fws, human, fake)
    assert _stopped(fws, a) == ["rollback-failed"]
    b, _, _ = prod()
    fake.live, fake.healthy = True, True
    _later(monkeypatch, 21)  # the window is open again: only the hold keeps B's production back
    fr.tick(fws, human, fake)
    assert _states(fws, b) == {"merge": "proven", "dev": "proven", "production": "waiting"}
    assert _prod(fws, b)["held"] == [a] and fake.ran().count(f"make rollback-prod {a}") == 1
    assert sum("deploy-prod" in x for x in fake.ran()) == 1  # only A's attempt ever ran
    pytest.importorskip("fastapi")
    html = _client(fws).get(f"/factory/{b}").text
    assert "Production is held: another epic&#39;s production is unresolved" in html and "data-held" in html
    fr.retry(fws, human, a, "production", a)  # the human resolves A: B is no longer held
    assert _prod(fws, b)["held"] == []


def test_the_window_counts_every_attempt_and_survives_a_deleted_record(fws, prod, human, monkeypatch):
    eid, _, _ = prod()
    fr.tick(fws, human, ProdFake())
    assert _states(fws, eid)["production"] == "proven"
    fr._window_path(fws).unlink()  # the runner's record is gone: the outcome records still say when
    w = fr.window(fws, 20)
    assert not w["open"] and w["last"] is not None
    _later(monkeypatch, 21)
    assert fr.window(fws, 20)["open"]


def test_a_window_record_in_the_future_keeps_the_window_shut(fws):
    fr._atomic(fr._window_path(fws), json.dumps({"at": "2999-01-01T00:00:00Z"}))
    w = fr.window(fws, 20)
    assert w["open"] is False and "lies in the future" in w["why"] and "cannot be read" not in w["why"]


@pytest.mark.parametrize("at", ["nonsense", None, 12])
def test_an_unreadable_window_record_keeps_the_window_shut(fws, at):
    fr._atomic(fr._window_path(fws), json.dumps({"at": at}))
    w = fr.window(fws, 20)
    assert w["open"] is False and "cannot be read" in w["why"] and "future" not in w["why"]


def test_a_stage_that_cannot_start_is_a_stopped_reason_until_the_human_retries(fws, ready, human, bin_dir, recipe):
    eid, (c,), _ = ready()
    (bin_dir / "gh").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    fake = Fake()
    assert "not the one you pinned" in fr.tick(fws, human, fake)[0]
    assert _stopped(fws, eid) == ["release-blocked"] and fake.calls == []
    assert fr.tick(fws, human, fake) == []  # said once, not every round
    reason = fr.reasons(fws, store.load(fws, eid)[1], permits.factory_delegation(fws, store.load(fws, eid)[1]))[0]
    assert "not the one you pinned" in reason["text"] and "nothing ran" in reason["text"]
    fr.set_recipe(fws, human, recipe)  # the human pins the program again
    with pytest.raises(ValidationError, match=f"blocked on the merge stage of {c}"):
        fr.retry(fws, human, eid, "dev", eid)  # a retry clears only the block it names
    fr.retry(fws, human, eid, "merge", c)
    assert _stopped(fws, eid) == [] and fr.tick(fws, human, fake)[-1] == f"{eid}: dev of {eid} proven"


def test_a_base_that_cannot_be_fetched_for_dev_is_a_stopped_reason(fws, ready, human, remote):
    eid, _, _ = ready()
    fake = Fake()

    def remote_goes_away(argv):  # after the merge's check, the recipe's remote is gone: dev cannot fetch the base
        if "view" in argv and remote.exists():
            remote.rename(remote.with_name("gone.git"))
    fake.on_call = remote_goes_away
    fr.tick(fws, human, fake)
    assert _states(fws, eid)["merge"] == "proven" and _stopped(fws, eid) == ["release-blocked"]
    assert "could not be fetched" in fr.reasons(fws, store.load(fws, eid)[1],
                                                permits.factory_delegation(fws, store.load(fws, eid)[1]))[0]["text"]


def test_a_recipe_cleared_after_signing_is_a_stopped_reason(fws, prod, human):
    eid, _, _ = prod()
    fr.clear_recipe(fws, human)
    assert "not started" in fr.tick(fws, human, ProdFake())[0]
    assert _stopped(fws, eid) == ["release-blocked"]


def test_a_child_closed_after_its_merge_does_not_make_dev_stale(fws, ready, human):
    from orch.core.ops import Ops
    eid, (c0, c1), _ = ready(kids=2)
    fake = Fake()
    fake.results["deploy-dev"] = {"code": 2}
    fr.tick(fws, human, fake)
    assert _states(fws, eid) == {"merge": "proven", "dev": "failed"}
    t1 = store.load(fws, c1)[1]
    Ops(fws, human).verdict(c1, "done", "ok", expected_hash=epics.verdict_hash([t1], fws),
                            skip_release="closed after its merge")  # dev is not proven: a reason is needed
    fr.retry(fws, human, eid, "dev", eid)
    fake.results.pop("deploy-dev")
    assert fr.tick(fws, human, fake) == [f"{eid}: dev of {eid} proven"]
    assert _states(fws, eid) == {"merge": "proven", "dev": "proven"} and _stopped(fws, eid) == []


def test_production_goes_stale_when_dev_is_proven_again_on_another_commit(fws, prod, human):
    eid, _, _ = prod()
    fr.tick(fws, human, ProdFake())
    dev = fr.unit_state(fws, eid, "dev", eid)
    d = fr._dir(fws, eid)
    n = dev["attempt"]
    body = {"stage": "dev", "unit": eid, "attempt": n + 1, "base_sha": "0" * 40, "children": dev["children"]}
    for kind, extra in (("intent", {}), ("outcome", {"proven": True, "codes": [0], "check": 0, "ended": "x"})):
        fr._write(d / fr._name("dev", eid, n + 1, kind), {**body, **extra})
    fr._write(d / fr._name("dev", eid, n, "retry"), {"at": "x"})
    p = _prod(fws, eid)
    assert p["state"] == "stale" and "dev changed after production was proven" in p["units"][0]["why"]


def test_a_window_longer_than_the_budget_is_refused_at_approval_and_warned_at_set(fws, fa, fh, human, remote,
                                                                                capsys, switch, tmp_path):
    e = fa.new("E", type="epic")
    _refine(fa, e.id, plan=None)
    fr.set_recipe(fws, human, _prod_recipe(remote, hours=72))
    with pytest.raises(ValidationError, match="time budget"):
        fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "prod"})
    fh.approve(e.id, "requirements", delegate={"factory": True, "dark": True, "release": "dev"})  # dev is fine
    f = tmp_path / "r.json"
    f.write_text(json.dumps(_prod_recipe(remote, hours=100)), encoding="utf-8")
    switch.human("RELEASE")
    assert cli_run(["factory", "release", "set", "--file", str(f)]) == 0
    assert "cannot sign prod" in capsys.readouterr().err
    pytest.importorskip("fastapi")
    html = _client(fws).get("/new").text
    assert re.search(r'value="prod"[^>]*disabled', html) and "data-prod-off" in html



# -- round 2: the hold reads the runner's index, Resolve, retries never run out, the window reset ------------------

def _broken_production(fws, prod):
    a, _, _ = prod(signed_rollback=True)
    fake = ProdFake()
    fake.live, fake.healthy = False, False
    fr.tick(fws, human_actor(), fake)
    assert _stopped(fws, a) == ["rollback-failed"]
    return a, fake


def human_actor():
    from orch.core.events import Actor
    return Actor("human", "you", "tty")


@pytest.mark.parametrize("hide", ["delete", "retype", "break"])
def test_hiding_the_unresolved_epics_ticket_does_not_lift_the_hold(fws, prod, human, monkeypatch, hide):
    a, fake = _broken_production(fws, prod)
    b, _, _ = prod()
    path = store.resolve(fws, a).path
    if hide == "delete":
        path.unlink()
    elif hide == "retype":
        path.write_text(path.read_text(encoding="utf-8").replace("type: epic", "type: feature"), encoding="utf-8")
    else:
        path.write_text("---\nthis: [is not\n---\n", encoding="utf-8")
    _later(monkeypatch, 21)
    fake.live, fake.healthy = True, True
    fr.tick(fws, human, fake)
    assert _states(fws, b)["production"] == "waiting" and _prod(fws, b)["held"] == [a]
    assert sum("deploy-prod" in x for x in fake.ran()) == 1  # only A's own attempt ever ran
    pytest.importorskip("fastapi")
    assert "Production is held" in _client(fws).get(f"/factory/{b}").text


def test_resolve_lifts_the_hold_without_running_the_production_again(fws, prod, human, agent, monkeypatch):
    a, fake = _broken_production(fws, prod)
    b, _, _ = prod()
    with pytest.raises(HumanOnlyError):
        fr.resolve(fws, agent, a, "checked by hand")
    with pytest.raises(UsageError):
        fr.resolve(fws, human, a, "  ")
    with pytest.raises(ValidationError, match="holds nothing"):
        fr.resolve(fws, human, b, "nothing to resolve")
    assert "does not run again" in fr.resolve(fws, human, a, "checked production by hand")
    with pytest.raises(ValidationError, match="resolved already"):
        fr.resolve(fws, human, a, "again")
    assert fr.unresolved_production(fws, b) == [] and _stopped(fws, a) == ["rollback-failed"]  # A still says it
    _later(monkeypatch, 21)
    fake.live, fake.healthy = True, True
    fr.tick(fws, human, fake)
    assert _states(fws, b)["production"] == "proven"
    assert _states(fws, a)["production"] == "failed" and fake.ran().count(f"make rollback-prod {a}") == 1
    rec = fr._read(fr._resolved_path(fws, a, 1))
    assert rec["by"] == "human:you" and rec["why"] == "checked production by hand"


def test_resolve_from_the_run_view_is_the_humans(fws, prod, human, monkeypatch):
    pytest.importorskip("fastapi")
    a, _ = _broken_production(fws, prod)
    c = _client(fws)
    assert "data-release-resolve" in c.get(f"/factory/{a}").text
    r = c.post(f"/factory/{a}/release/resolve", data={"reason": "x"}, headers={"origin": "http://evil.example"},
               follow_redirects=False)
    assert r.status_code == 403
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    r = c.post(f"/factory/{a}/release/resolve", data={"reason": "x"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and not os.path.lexists(fr._resolved_path(fws, a, 1))
    monkeypatch.delenv("ORCH_HARNESS")
    r = c.post(f"/factory/{a}/release/resolve", data={"reason": "checked"}, follow_redirects=False)
    assert "err=" not in r.headers["location"] and os.path.lexists(fr._resolved_path(fws, a, 1))


def test_a_block_can_be_retried_again_and_again(fws, ready, human, bin_dir, recipe):
    eid, (c,), _ = ready()
    for _ in range(fr.MAX_ATTEMPTS + 2):  # more than the old slots: never runs out
        (bin_dir / "gh").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
        fr.tick(fws, human, Fake())
        assert _stopped(fws, eid) == ["release-blocked"]
        fr.retry(fws, human, eid, "merge", c)


def test_a_future_dated_record_is_cleared_by_the_human_only(fws, human, agent):
    _at(fws, -500)  # 500 hours ahead
    assert "clear-window" in fr.window(fws, 20)["why"]
    with pytest.raises(HumanOnlyError):
        fr.clear_window(fws, agent)
    fr.clear_window(fws, human)
    assert fr.window(fws, 20)["open"]


def test_release_set_names_live_prod_charters_whose_budget_the_window_uses_up(fws, prod, human, remote, capsys, switch,
                                                                             tmp_path):
    eid, _, _ = prod()
    f = tmp_path / "r.json"
    f.write_text(json.dumps(_prod_recipe(remote, hours=72)), encoding="utf-8")
    switch.human("RELEASE")
    assert cli_run(["factory", "release", "set", "--file", str(f)]) == 0
    err = capsys.readouterr().err
    assert f"{eid}'s live charter signs prod with a 72-hour budget" in err


def test_the_new_human_verbs_and_records_are_guarded(ws):
    from orch.hooks.guard import evaluate
    base = ledger.base_dir()
    for cmd in ("orch factory release resolve L-0001 --reason x", "orch factory release clear-window",
                f"cat {base}/permits/release-records/production-index-x.jsonl",
                f"rm {base}/permits/release-records/window-reset-x.json"):
        assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)}).allow, cmd
        assert permits.never_grantable(ws, cmd) is not None, cmd


# -- fail closed on drifting state: every place a release decision reads state that could go missing ---------------

def test_a_deleted_journal_blocks_every_release_until_the_human_acknowledges_it(fws, prod, human, monkeypatch):
    a, fake = _broken_production(fws, prod)
    b, _, _ = prod()
    fr._index_path(fws).unlink()  # the runner's journal is gone; the records are still there
    _later(monkeypatch, 21)
    fake.live, fake.healthy = True, True
    assert fr.tick(fws, human, fake) == [] and _stopped(fws, b) == ["release-blocked"]
    assert "journal is missing or damaged" in fr.reasons(fws, store.load(fws, b)[1],
                                                         permits.factory_delegation(fws, store.load(fws, b)[1]))[0]["text"]
    assert "acknowledged" in fr.retry(fws, human, b, fr.JOURNAL_STAGE, b)
    fr.tick(fws, human, fake)
    assert _prod(fws, b)["held"] == [a]  # A's production records still hold B, found from the records themselves


def test_a_journal_with_a_line_that_cannot_be_read_holds_and_shuts(fws, prod, human):
    a, _ = _broken_production(fws, prod)
    with fr._index_path(fws).open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert fr.window(fws, 1)["open"] is False and "journal" in fr.window(fws, 1)["why"]
    assert "the runner's release journal (missing or damaged)" in fr.unresolved_production(fws, "L-9999")


def test_removing_a_blocks_file_does_not_lift_the_block(fws, ready, human, bin_dir):
    eid, (c,), _ = ready()
    (bin_dir / "gh").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    fr.tick(fws, human, Fake())
    (fr._dir(fws, eid) / "blocked.json").unlink()
    assert _stopped(fws, eid) == ["release-blocked"] and fr.tick(fws, human, Fake()) == []
    assert "the runner's journal still holds it" in fr._blocked_record(fws, eid)["why"]
    fr.retry(fws, human, eid, "merge", c)
    assert fr._blocked_record(fws, eid) is None


def test_a_gap_in_the_attempt_records_is_an_unknown_outcome(fws, prod, human):
    eid, _, _ = prod()
    d = fr._dir(fws, eid)
    fr._write(d / fr._name("production", eid, 2, "intent"), {"stage": "production", "unit": eid})
    us = fr.unit_state(fws, eid, "production", eid)
    assert us["state"] == "unknown" and "gap" in us["why"]
    assert "release-unknown" in _stopped(fws, eid)


def test_a_deleted_window_record_and_a_removed_epic_ticket_still_shut_the_window(fws, prod, human):
    eid, _, _ = prod()
    fr.tick(fws, human, ProdFake())
    fr._window_path(fws).unlink()
    store.resolve(fws, eid).path.unlink()
    assert fr.window(fws, 20)["open"] is False  # the journal and the records still say when production ran

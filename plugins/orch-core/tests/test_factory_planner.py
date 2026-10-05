"""AI Factory: the planner session that splits a childless epic, and the Dark baseline (docs/factory.md, "The planner"
and "The baseline"). A fake launcher stands in for tmux: no test starts a real agent or touches a real tmux server."""
import json
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest
import typer

from orch.cli import app
from orch.core import dark_profile, epics, factory_runner, factory_sessions as fs, ledger, permits, store
from orch.errors import HumanOnlyError
from orch.hooks.guard import evaluate
from test_dark_profile import _ok, _run, switch  # noqa: F401  (switch is a fixture)
from test_factory_runner import (Fake, _behavior, _payload, _refine, _tick, _trusted_programs, fa, fake,  # noqa: F401
                                 fh, fws)

UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _epic(ws, fa, fh, arm=True, title="Billing revamp", **limits):
    e = fa.new(title, type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True, **limits})
    d = epics.delegation(ws, store.load(ws, e.id)[1])
    if arm:
        fs.arm(ws, fh.actor, d["id"])
    return e.id, d


def _child(fa, eid, approve=True):
    c = fa.new("child", epic=eid)
    _refine(fa, c.id)
    if approve:
        fa.epic_auto_approve(c.id)
    return c.id


def _root(ws):
    return str(Path(ws.root).resolve())


# -- launching the planner ----------------------------------------------------------------------------------------

def test_one_planner_for_an_armed_epic_without_children(fws, fa, fh, human, fake):
    eid, d = _epic(fws, fa, fh)
    lines = _tick(fws, human, fake)
    ((name, cwd, argv),) = fake.started
    (b,) = fs.bindings(fws)
    assert lines == [f"started {name}"] and b["name"] == name and fs.is_planner(b)
    assert (b["epic"], b["child"], b["delegation"]) == (eid, eid, d["id"])
    assert b["checkout"] == ledger.checkout_id(fws) and b["start"] == cwd == _root(fws)  # the workspace root
    assert argv[argv.index("--session-id") + 1] == b["session"]
    prompt = argv[-1]
    assert prompt == factory_runner.planner_prompt(eid) and eid in prompt and not prompt.startswith("-")
    assert "\n" not in prompt and "orch epic auto-approve" in prompt
    assert fs.planner_runs(fws, d["id"]) == 1 and fs.runs(fws, d["id"]) == 0  # not a child of the charter's count
    assert _tick(fws, human, fake) == [] and len(fake.started) == 1  # one planner, not one per round


def test_the_planner_prompt_is_built_in_and_checks_its_key(fws):
    assert factory_runner.planner_prompt("L-0001").startswith("You are the planner of the AI Factory epic L-0001.")
    for bad in ("-x", "L-1\n", "l-1", "", None, "L-1 --model x"):
        assert factory_runner.planner_prompt(bad) is None


def test_no_planner_once_the_epic_has_any_child(fws, fa, fh, human, fake):
    eid, d = _epic(fws, fa, fh)
    _child(fa, eid, approve=False)  # a child no session may start yet: still not the planner's
    assert _tick(fws, human, fake) == [] and not fake.started and fs.planner_runs(fws, d["id"]) == 0


def _pause(fws, fa, fh, eid, monkeypatch, configure):
    fh.epic_pause(eid)


def _edit(fws, fa, fh, eid, monkeypatch, configure):
    fa.set_section(eid, "Requirements", "edited after the start")


def _expire(fws, fa, fh, eid, monkeypatch, configure):
    from orch import clock
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))


def _blocked(fws, fa, fh, eid, monkeypatch, configure):
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: "no orch hooks at user scope")


def _no_programs(fws, fa, fh, eid, monkeypatch, configure):
    monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: None)


def _cut(fws, fa, fh, eid, monkeypatch, configure):
    monkeypatch.setattr(ledger, "head_ok", lambda: False)


@pytest.mark.parametrize("hold", [_pause, _edit, _expire, _blocked, _no_programs, _cut])
def test_no_planner_while_the_runner_must_not_start_anything(fws, fa, fh, human, fake, monkeypatch, configure, hold):
    eid, d = _epic(fws, fa, fh)
    hold(fws, fa, fh, eid, monkeypatch, configure)
    _tick(fws, human, fake)
    assert not fake.started and fs.bindings(fws) == []
    assert fs.planner_runs(fws, d["id"]) == 0  # nothing used up its two launches


def test_no_planner_unarmed_or_with_the_factory_off(fws, fa, fh, human, fake, configure):
    _epic(fws, fa, fh, arm=False)
    assert _tick(fws, human, fake) == [] and not fake.started
    _epic(fws, fa, fh, title="Second")
    assert _tick(configure(factory={"enabled": False}), human, fake) == [] and not fake.started


def test_the_planner_takes_a_concurrency_slot(fws, fa, fh, human, fake, configure):
    e1, _ = _epic(fws, fa, fh)
    e2, _ = _epic(fws, fa, fh, title="Second")
    kids = [_child(fa, e2) for _ in range(3)]
    _tick(fws, human, fake)
    assert sorted(b["child"] for b in fs.bindings(fws)) == sorted([e1, *kids[:2]])  # the planner and two children


def test_a_full_cap_holds_the_planner_back(fws, fa, fh, human, fake, configure):
    e1, _ = _epic(fws, fa, fh)
    cid = _child(fa, e1)
    e2, d2 = _epic(fws, fa, fh, title="Second")
    one = configure(factory={"enabled": True, "max_concurrency": 1})
    _tick(one, human, fake)
    assert [b["child"] for b in fs.bindings(one)] == [cid] and fs.planner_runs(one, d2["id"]) == 0


def test_a_planner_starts_at_most_twice_and_only_when_what_it_waits_for_changed(fws, fa, fh, human, fake):
    eid, d = _epic(fws, fa, fh)
    for i in range(fs.PLANNER_LAUNCHES + 2):
        _tick(fws, human, fake)
        if not fs.bindings(fws):
            break
        (b,) = fs.bindings(fws)
        assert fs.is_planner(b)
        out = permits.hook_decision(fws, _payload(b["session"], f"make t{i}"))  # parks on a card
        assert _behavior(out) == "deny"
        fake.names.clear()  # the planner ended its session
        assert "ended" in _tick(fws, human, fake)[0]
        assert _tick(fws, human, fake) == []  # nothing it waits for changed: no relaunch, no loop
        (r,) = permits.open_requests(fws)
        assert r["ticket"] == eid and r["epic"] == eid
        permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])  # a change that wakes it
    _tick(fws, human, fake)
    assert len(fake.started) == fs.PLANNER_LAUNCHES == fs.planner_runs(fws, d["id"])
    assert fs.runs(fws, d["id"]) == 0


def test_the_planner_goes_on_while_a_child_waits_and_stops_once_every_child_is_approved(fws, fa, fh, human, fake):
    eid, d = _epic(fws, fa, fh)
    _tick(fws, human, fake)
    (p,) = fs.bindings(fws)
    c1 = _child(fa, eid)  # the planner's work: one child approved,
    c2 = _child(fa, eid, approve=False)  # one still being written
    _tick(fws, human, fake)
    assert sorted(b["child"] for b in fs.bindings(fws)) == sorted([eid, c1])  # it goes on; the approved child starts
    fa.epic_auto_approve(c2)
    lines = _tick(fws, human, fake)
    assert any(p["name"] in x and "planner is done" in x for x in lines) and p["name"] in fake.stopped
    assert sorted(b["child"] for b in fs.bindings(fws)) == sorted([c1, c2])
    _tick(fws, human, fake)
    assert not any(fs.is_planner(b) for b in fs.bindings(fws)) and fs.planner_runs(fws, d["id"]) == 1


def test_a_planner_holding_the_only_slot_hands_it_to_the_child_after_planning(fws, fa, fh, human, fake, configure):
    one = configure(factory={"enabled": True, "max_concurrency": 1})
    eid, d = _epic(one, fa, fh)
    _tick(one, human, fake)
    (p,) = fs.bindings(one)
    cid = _child(fa, eid)
    _tick(one, human, fake)  # the planner stops (its child is approved) and frees the slot
    _tick(one, human, fake)
    assert [b["child"] for b in fs.bindings(one)] == [cid] and p["name"] in fake.stopped


def _later(monkeypatch, minutes):
    from orch import clock
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(minutes=minutes))


def test_the_planner_time_limit_with_a_child_waiting(fws, fa, fh, human, fake, monkeypatch):
    eid, d = _epic(fws, fa, fh)
    _tick(fws, human, fake)
    (p,) = fs.bindings(fws)
    _child(fa, eid, approve=False)
    _later(monkeypatch, factory_runner.PLANNER_MINUTES - 1)
    _tick(fws, human, fake)
    assert fs.bindings(fws) == [p]
    _later(monkeypatch, factory_runner.PLANNER_MINUTES + 1)
    assert "time limit" in _tick(fws, human, fake)[0] and fs.bindings(fws) == []
    _tick(fws, human, fake)
    assert fs.bindings(fws) == [] and len(fake.started) == 1  # a child exists: no planner again


def test_the_planner_time_limit_without_a_child_counts_as_a_launch(fws, fa, fh, human, fake, monkeypatch):
    eid, d = _epic(fws, fa, fh)
    _tick(fws, human, fake)
    _later(monkeypatch, factory_runner.PLANNER_MINUTES + 1)
    assert "time limit" in _tick(fws, human, fake)[0] and fs.bindings(fws) == []
    assert _tick(fws, human, fake) == [] and fs.planner_runs(fws, d["id"]) == 1  # parked, not looping


def test_three_childless_epics_on_one_slot_take_turns(fws, fa, fh, human, fake, monkeypatch, configure):
    one = configure(factory={"enabled": True, "max_concurrency": 1})
    ids = [_epic(one, fa, fh, title=f"Epic {i}")[0] for i in range(3)]
    seen = []
    for i in range(3):
        _later(monkeypatch, i * (factory_runner.PLANNER_MINUTES + 1))
        _tick(one, human, fake)  # ends the planner whose time is up, then starts the next epic's
        (b,) = fs.bindings(one)
        seen.append(b["epic"])
    assert sorted(seen) == sorted(ids)  # every epic got its planner: the slot is never held for good


def test_a_dashboard_restart_does_not_use_up_a_planner_launch(fws, fa, fh, human, fake):
    eid, d = _epic(fws, fa, fh)
    for _ in range(3):  # three dashboard runs, each ended by the dashboard shutting down
        _tick(fws, human, fake)
        assert fs.planner_runs(fws, d["id"]) == 1
        factory_runner.sweep(fws, human, fake, stop_all=True)
        assert fs.planner_runs(fws, d["id"]) == 0
    assert len(fake.started) == 3


def test_another_dashboard_binding_the_planner_first_wins(fws, fa, fh, human, fake, monkeypatch):
    """The already-running check, the marker and the binding happen under the delegation's lock: a planner another
    dashboard on the same config dir bound after this round looked is seen there, and nothing else starts."""
    eid, d = _epic(fws, fa, fh)
    other = Fake()
    real = factory_runner._ready

    def other_dashboard_binds_meanwhile(ws, settings, epic, dd, t, lines, planner=False):
        out = real(ws, settings, epic, dd, t, lines, planner=planner)
        monkeypatch.setattr(factory_runner, "_ready", real)
        factory_runner._start(ws, human, other, settings, epic, dd, t, "", [], out)
        return out

    monkeypatch.setattr(factory_runner, "_ready", other_dashboard_binds_meanwhile)
    _tick(fws, human, fake)
    assert not fake.started and len(other.started) == 1 and len(fs.bindings(fws)) == 1


def test_a_missing_program_burns_no_child_launch(fws, fa, fh, human, fake, monkeypatch):
    eid, d = _epic(fws, fa, fh)
    cid = _child(fa, eid)
    good = factory_runner.resolve_bin
    monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: None)
    for _ in range(7):
        _tick(fws, human, fake)
    assert fs.runs(fws, d["id"], cid) == 0 and fs.runs(fws, d["id"]) == 0 and not fake.started
    monkeypatch.setattr(factory_runner, "resolve_bin", good)
    _tick(fws, human, fake)
    assert [b["child"] for b in fs.bindings(fws)] == [cid]


def _stop_pause(fws, fh, eid, monkeypatch, configure):
    fh.epic_pause(eid)
    return fws


def _stop_cut(fws, fh, eid, monkeypatch, configure):
    monkeypatch.setattr(ledger, "head_ok", lambda: False)
    return fws


def _stop_off(fws, fh, eid, monkeypatch, configure):
    return configure(factory={"enabled": False})


def _stop_expired(fws, fh, eid, monkeypatch, configure):
    from orch import clock
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    return fws


@pytest.mark.parametrize("stop,why", [(_stop_pause, "paused"), (_stop_cut, "cut"), (_stop_off, "switched off"),
                                      (_stop_expired, "time budget")])
def test_the_planner_stops_like_a_child(fws, fa, fh, human, fake, monkeypatch, configure, stop, why):
    eid, d = _epic(fws, fa, fh)
    _tick(fws, human, fake)
    (b,) = fs.bindings(fws)
    ws = stop(fws, fh, eid, monkeypatch, configure)
    lines = _tick(ws, human, fake)
    assert why in lines[0] and fake.stopped == [b["name"]] and fs.bindings(ws) == []
    assert permits.hook_decision(ws, _payload(b["session"])) is None


def test_an_edited_epic_and_the_dashboard_stop_end_the_planner(fws, fa, fh, human, fake):
    eid, d = _epic(fws, fa, fh)
    _tick(fws, human, fake)
    fa.set_section(eid, "Requirements", "edited")
    assert "changed" in _tick(fws, human, fake)[0] and fs.bindings(fws) == []
    e2, _ = _epic(fws, fa, fh, title="Second")
    _tick(fws, human, fake)
    assert fs.bindings(fws)
    factory_runner.sweep(fws, human, fake, stop_all=True)
    assert fs.bindings(fws) == []


# -- the hook: a planner session is a factory session of its own epic ---------------------------------------------------

def test_the_hook_treats_the_planner_as_a_session_of_its_epic(fws, fa, fh, human, fake):
    eid, d = _epic(fws, fa, fh)
    _tick(fws, human, fake)
    (b,) = fs.bindings(fws)
    out = permits.hook_decision(fws, _payload(b["session"], f"orch approve {eid} requirements"))
    assert _behavior(out) == "deny" and "never granted" in out["hookSpecificOutput"]["decision"]["message"]
    other = {"session_id": b["session"], "tool_name": "Write", "tool_input": {"file_path": "x"}}
    assert _behavior(permits.hook_decision(fws, other)) == "deny"
    permits.hook_decision(fws, _payload(b["session"]))
    (r,) = permits.open_requests(fws)
    permits.permit_grant(fws, human, r["id"], "epic", expected_sha=r["sha"])
    assert _behavior(permits.hook_decision(fws, _payload(b["session"]))) == "allow"  # the epic's grant applies


def test_a_planner_binding_never_counts_for_another_epic(fws, fa, fh, human):
    e1, d1 = _epic(fws, fa, fh)
    e2, d2 = _epic(fws, fa, fh, title="Second")
    for epic, delegation, child in ((e1, d1["id"], e2), (e1, d2["id"], e1), (e2, d1["id"], e1)):
        fs.bind(fws, human, session=UUID, epic=epic, delegation=delegation, child=child, name="fx-x")
        fs.set_pid(fws, human, UUID, 4242)
        assert permits.hook_decision(fws, _payload(UUID)) is None, (epic, delegation, child)
        fs.end(fws, UUID)
        (fs._root() / "sessions" / f"{UUID}.ended").unlink()


def test_a_dark_planner_runs_the_baseline_and_nothing_else(configure, agent, human, fake):
    from conftest import human_ops
    from orch.core.ops import Ops
    dws = configure(factory={"enabled": True})
    Ops(dws, human).set_factory_dark(True)
    eid, d = _epic(dws, Ops(dws, agent), human_ops(dws, human), dark=True)
    dark_profile.add_baseline(dws, human)
    _tick(dws, human, fake)
    (b,) = fs.bindings(dws)
    for cmd in (f"orch show {eid}", f'orch new --epic {eid} --title "Export" --size s --requirements-file '
                f"orchestrator/temporary/r.md --acceptance-file orchestrator/temporary/a.md",
                'orch section set L-0009 Plan -m "Add the export, then test it."', "orch epic auto-approve L-0009",
                'orch log L-0009 -m "a (note); quoted"'):
        assert _behavior(permits.hook_decision(dws, _payload(b["session"], cmd))) == "allow", cmd
    for cmd in (f"orch approve {eid} requirements", f"orch move {eid} done", "orch dark profile add --baseline",
                'orch log L-0009 -m "$(id)"', "orch show L-0009 && orch claim L-0009", "make deploy"):
        assert _behavior(permits.hook_decision(dws, _payload(b["session"], cmd))) == "deny", cmd


# -- the baseline ---------------------------------------------------------------------------------------------------

def _leaf(words):
    """The click command `orch <words>` reaches, consuming every word, or None."""
    cmd = typer.main.get_command(app)  # typer carries its own click: groups are told by their `commands`
    for w in words:
        if not hasattr(cmd, "commands") or w not in cmd.commands:
            return None
        cmd = cmd.commands[w]
    return None if hasattr(cmd, "commands") else cmd


@pytest.mark.parametrize("rule", dark_profile.BASELINE)
def test_every_baseline_rule_is_a_real_command_and_passes_the_checks(ws, rule):
    words = rule.split()
    assert words[0] == "orch" and _leaf(words[1:]) is not None, rule
    assert dark_profile.refusal("prefix", words) is None
    assert dark_profile.check_rule(ws, "prefix", rule) == ("prefix", words)
    assert permits.never_grantable(ws, rule) is None
    agent_form = f"{rule} L-0001"
    assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": agent_form}, "cwd": str(ws.root)}).allow
    assert permits.never_grantable(ws, agent_form) is None


HUMAN_ONLY = ("orch approve L-1 requirements", "orch answer L-1 Q1 a", "orch verdict L-1 done",
              "orch request-changes L-1 plan", "orch reopen L-1", "orch close L-1", "orch ledger adopt L-1",
              "orch epic pause L-1", "orch permit grant P-1", "orch permit deny P-1", "orch permit revoke abc",
              "orch dark profile add --baseline", "orch dark profile remove R-1", "orch factory dark on",
              "orch addon trust x", "orch addon install x", "orch move L-1 done", "orch move L-1 open",
              "orch move L-1 backlog", "orch serve")


@pytest.mark.parametrize("cmd", HUMAN_ONLY)
def test_no_human_only_command_runs_under_the_baseline(dws_baseline, cmd):
    assert permits.never_grantable(dws_baseline, cmd)
    assert dark_profile.match(dws_baseline, cmd) is None
    if not cmd.startswith("orch serve"):  # the dashboard is refused by never_grantable, the rest by the guard too
        assert not evaluate(dws_baseline, {"tool_name": "Bash", "tool_input": {"command": cmd},
                                           "cwd": str(dws_baseline.root)}).allow


def test_ask_and_human_verbs_are_not_in_the_baseline():
    firsts = {tuple(r.split()[1:3]) for r in dark_profile.BASELINE} | {(r.split()[1],) for r in dark_profile.BASELINE}
    for verb in ("approve", "answer", "verdict", "request-changes", "reopen", "close", "ledger", "ask", "serve",
                 "addon", "dark", "factory"):
        assert (verb,) not in firsts
    for pair in (("epic", "pause"), ("permit", "grant"), ("permit", "deny"), ("permit", "revoke")):
        assert pair not in firsts


@pytest.fixture
def dws_baseline(configure, human):
    from orch.core.ops import Ops
    ws = configure(factory={"enabled": True})
    Ops(ws, human).set_factory_dark(True)
    dark_profile.add_baseline(ws, human)
    return ws


def test_baseline_matches_the_agent_forms(dws_baseline):
    for rule in dark_profile.BASELINE:
        assert dark_profile.match(dws_baseline, f"{rule} L-0001 --json") is not None, rule


def test_add_baseline_is_human_only_and_idempotent(configure, human, agent):
    ws = configure(factory={"enabled": True})
    with pytest.raises(HumanOnlyError):
        dark_profile.add_baseline(ws, agent)
    assert dark_profile.rules(ws) == []
    dark_profile.add(ws, human, "prefix", "orch show")  # one already in force is skipped
    res = dark_profile.add_baseline(ws, human)
    assert len(res["added"]) == len(dark_profile.BASELINE) - 1 and res["failed"] == []
    assert len(dark_profile.rules(ws)) == len(dark_profile.BASELINE)
    n = len(ledger.entries(ws))
    assert dark_profile.add_baseline(ws, human) == {"added": [], "failed": []} and len(ledger.entries(ws)) == n
    assert dark_profile.baseline_todo(ws) == []


def test_cli_add_baseline(capsys, switch, configure):  # noqa: F811
    ws = configure(factory={"enabled": True})
    code, _ = _run(capsys, "dark", "profile", "add", "--baseline")
    assert code != 0 and dark_profile.rules(ws) == []  # an agent never adds
    switch.human("wrong")
    code, _ = _run(capsys, "dark", "profile", "add", "--baseline")
    assert code != 0 and dark_profile.rules(ws) == []
    code, _ = _run(capsys, "dark", "profile", "add", "--baseline", "--prefix", "make test")
    assert code != 0
    switch.human("BASELINE")
    out = _ok(capsys, "dark", "profile", "add", "--baseline")
    assert f"added {len(dark_profile.BASELINE)} baseline rules" in out and "orch epic auto-approve" in out
    assert len(dark_profile.rules(ws)) == len(dark_profile.BASELINE)
    assert json.loads(_ok(capsys, "dark", "profile", "add", "--baseline", "--json")) == {"added": [], "failed": []}


# -- the prompt names only what the CLI has, and what the baseline runs ---------------------------------------------------

@pytest.fixture
def dws_both(configure, human):
    """The orch and git-basic baselines, in a workspace that lets agents commit."""
    from orch.core.ops import Ops
    ws = configure(factory={"enabled": True}, git={"agent_may": {"commit": True}})
    Ops(ws, human).set_factory_dark(True)
    dark_profile.add_baseline(ws, human)
    dark_profile.add_baseline(ws, human, name="git-basic")
    return ws


NAMED_AS_REFUSED = (("ask",), ("permit", "request"), ("instructions", "sync"), ("setup",))


@pytest.mark.parametrize("prompt", [factory_runner.planner_prompt("L-0001"),
                                    factory_runner.factory_work_prompt("L-0002")], ids=["planner", "worker"])
def test_every_command_in_a_built_in_prompt_exists_and_the_baselines_run_it(dws_both, prompt):
    import subprocess
    snippets = re.findall(r"`([^`]+)`", prompt)
    orch_cmds = [s for s in snippets if s.startswith("orch ")]
    assert len(orch_cmds) >= 6 and "\n" not in prompt
    for s in orch_cmds:
        words = s.split()
        cut = next((i for i in range(len(words), 0, -1) if _leaf(words[1:i]) is not None), None)
        assert cut, s
        cmd = _leaf(words[1:cut])
        opts = {o for p in cmd.params for o in (*p.opts, *p.secondary_opts)}
        for w in words[cut:]:
            if w.startswith("-"):
                assert w in opts, (s, w)
        if not any(tuple(words[1:1 + len(n)]) == n for n in NAMED_AS_REFUSED):
            real = (s.replace("CHILD", "L-0002").replace("FILE", "r.md").replace("SIZE", "s")
                    .replace("TN", "T1").replace("TASK", "Write the export"))
            assert dark_profile.match(dws_both, real) is not None, real
    for s in (s for s in snippets if s.startswith("git ")):
        real = s.replace("FILES", "src/a.py")
        r = subprocess.run(["git", real.split()[1], "-h"], capture_output=True, text=True, cwd=dws_both.root)
        assert r.returncode == 129 and "usage: git" in r.stdout, s
        assert dark_profile.match(dws_both, real) is not None, real


def test_the_worker_prompt_is_built_in_and_says_the_plain_rules():
    p = factory_runner.factory_work_prompt("L-0002")
    assert p.startswith("You work on L-0002, a child of an AI Factory epic.") and "orch-work-on-ticket" in p
    for words in ("one plain command per tool call", "`&&`", "`2>&1`", "`|| true`", "actually denied with a request id",
                  "never retry variants", "`orch instructions sync`", "`orch setup`", "with no -m",
                  "`git commit -m \"L-0002 short text\"`", "`orch move L-0002 testing`"):
        assert words in p, words
    for bad in ("-x", "L-1\n", "l-1", "", None, "L-1 --model x"):
        assert factory_runner.factory_work_prompt(bad) is None
    assert "one plain command per tool call" in factory_runner.planner_prompt("L-0001")


def test_baseline_partial_failure_says_what_was_added_and_what_failed(configure, human, monkeypatch, capsys, switch):  # noqa: F811
    ws = configure(factory={"enabled": True})
    switch.human("BASELINE")  # a human process: the direct calls below and the CLI
    real = dark_profile.add

    def flaky(w, actor, kind, value):
        if value == "orch move":
            from orch.errors import ValidationError
            raise ValidationError("the ledger refused it")
        return real(w, actor, kind, value)

    monkeypatch.setattr(dark_profile, "add", flaky)
    res = dark_profile.add_baseline(ws, human)
    assert res["failed"] == [{"rule": "orch move", "error": "the ledger refused it"}]
    assert len(res["added"]) == len(dark_profile.BASELINE) - 1
    monkeypatch.setattr(dark_profile, "add", real)
    dark_profile.remove(ws, human, dark_profile.rule_id("prefix", ["orch", "show"]))
    monkeypatch.setattr(dark_profile, "add", lambda *a: (_ for _ in ()).throw(__import__("orch.errors").errors.ValidationError("no")))
    code, out = _run(capsys, "dark", "profile", "add", "--baseline")
    assert code != 0 and "FAILED orch show: no" in out.out and "FAILED orch move: no" in out.out
    assert "baseline rules were not added" in out.err


# -- a factory session works on its own epic only -----------------------------------------------------------------------

def _session_ops(ws, b):
    from orch.core.events import Actor
    from orch.core.ops import Ops
    return Ops(ws, Actor("agent", "claude-code", "cli", b["session"]))


def test_a_planner_creates_and_approves_children_of_its_own_epic_only(fws, fa, fh, human, fake):
    from orch.errors import ValidationError
    e1, _ = _epic(fws, fa, fh)
    e2, _ = _epic(fws, fa, fh, title="Second")
    _tick(fws, human, fake)
    p1 = next(b for b in fs.bindings(fws) if b["epic"] == e1)
    ops = _session_ops(fws, p1)
    mine = ops.new("mine", epic=e1)  # the planner's own flow works
    _refine(ops, mine.id)
    ops.epic_auto_approve(mine.id)
    ops.log(mine.id, "decided X because the epic says Y")
    ops.new("a follow-up for later")  # backlog items outside any epic stay possible
    with pytest.raises(ValidationError, match=f"works on epic {e1}"):
        ops.new("theirs", epic=e2)
    theirs = _child(fa, e2, approve=False)  # written by another agent under e2
    with pytest.raises(ValidationError, match=f"works on epic {e1}"):
        ops.epic_auto_approve(theirs)
    free = fa.new("free ticket")
    with pytest.raises(ValidationError, match=f"works on epic {e1}"):
        ops.link(free.id, epic=e2)
    fa.epic_auto_approve(theirs)  # an agent without a binding: as before


def test_a_worker_claims_releases_and_moves_its_own_epics_children_only(fws, fa, fh, human, fake):
    from orch.errors import ValidationError
    e1, _ = _epic(fws, fa, fh)
    c1 = _child(fa, e1)
    e2, _ = _epic(fws, fa, fh, title="Second")
    c2 = _child(fa, e2)
    _tick(fws, human, fake)
    w1 = next(b for b in fs.bindings(fws) if b["child"] == c1)
    ops = _session_ops(fws, w1)
    ops.claim(c1)
    ops.release(c1)
    ops.claim(c1)
    for call in (lambda: ops.claim(c2), lambda: ops.move(c2, "waiting"), lambda: ops.release(c2)):
        with pytest.raises(ValidationError, match=f"works on epic {e1}"):
            call()
    fa.claim(c2)  # an agent without a binding: as before


# -- an agent hands orch only files inside the workspace ------------------------------------------------------------------

def _bound_session(ws, human, monkeypatch=None):
    """A runner binding for UUID (pid 4242, which conftest puts in this process's ancestry); with `monkeypatch`, this
    process's CLI runs as that agent session."""
    fs.bind(ws, human, session=UUID, epic="L-0001", delegation="sha256:x", child="L-0001", name="fx-L-0001-aaaaaa")
    fs.set_pid(ws, human, UUID, 4242)
    if monkeypatch is not None:
        monkeypatch.setenv("ORCH_HARNESS", "test-agent")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", UUID)
    from orch.core.events import Actor
    return Actor("agent", "claude-code", "cli", UUID)


def test_agent_file_options_stay_inside_the_workspace(ws, tmp_path, capsys, monkeypatch, human):
    secret = tmp_path / "home" / ".ssh" / "id_ed25519"
    secret.parent.mkdir(parents=True)
    secret.write_text("PRIVATE KEY", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    inside = ws.root / "orchestrator" / "temporary" / "r.md"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("the requirements", encoding="utf-8")
    link = ws.root / "orchestrator" / "temporary" / "link.md"
    link.symlink_to(secret)
    (ws.root / "sub").mkdir()
    linked_dir = ws.root / "dirlink"
    linked_dir.symlink_to(secret.parent)
    outside_ok = json.loads(_ok(capsys, "new", "-t", "unbound", "--requirements-file", str(secret), "--json"))
    assert outside_ok["id"]  # an agent outside the factory: as before (its harness asks about the command)
    store.load(ws, outside_ok["id"])[0].unlink()
    _bound_session(ws, human, monkeypatch)
    t = json.loads(_ok(capsys, "new", "-t", "ok", "--requirements-file", str(inside), "--json"))
    assert "the requirements" in store.load(ws, t["id"])[1].section("Requirements")
    for bad in (str(secret), "~/.ssh/id_ed25519", "../home/.ssh/id_ed25519", "sub/../../home/.ssh/id_ed25519",
                str(link), "dirlink/id_ed25519"):
        for args in (("new", "-t", "x", "--requirements-file", bad), ("new", "-t", "x", "--body-file", bad),
                     ("new", "-t", "x", "--acceptance-file", bad), ("new", "-t", "x", "--summary-file", bad),
                     ("new", "-t", "x", "--out-of-scope-file", bad), ("section", "set", t["id"], "Plan", "--file", bad),
                     ("state", t["id"], "--file", bad), ("task", "add", t["id"], "--file", bad),
                     ("artifact", "add", t["id"], bad)):
            code, out = _run(capsys, *args)
            assert code != 0 and "PRIVATE KEY" not in json.dumps(store.load(ws, t["id"])[1].sections), (args, out)
            assert "an agent cannot hand orch the file" in out.err or "not a file" in out.err \
                or "does not exist" in out.err, (args, out.err)
    for bad, why in ((str(secret), "outside the workspace"), ("../home/.ssh/id_ed25519", "outside the workspace"),
                     (str(link), "symbolic link"), ("dirlink/id_ed25519", "symbolic link")):
        code, out = _run(capsys, "new", "-t", "x", "--requirements-file", bad)
        assert code != 0 and why in out.err, (bad, out.err)
    assert not list((ws.root / "orchestrator").rglob("id_ed25519"))  # nothing was copied into an artifact
    assert "PRIVATE KEY" not in "".join(p.read_text(encoding="utf-8", errors="replace")
                                        for p in ws.root.rglob("*") if p.is_file() and not p.is_symlink())


def test_agent_source_refuses_the_config_dir_and_lets_a_human_pass_anything(ws, tmp_path, monkeypatch, human):
    from orch.core import fsutil
    from orch.errors import ValidationError
    cfg = ws.root / "cfg"
    (cfg / "permits").mkdir(parents=True)
    rec = cfg / "permits" / "x.json"
    rec.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ledger, "base_dir", lambda: cfg)
    agent = _bound_session(ws, human)  # its binding lives in that config dir too
    with pytest.raises(ValidationError, match="config dir"):
        fsutil.agent_source(ws, agent, rec)
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="outside the workspace"):
        fsutil.agent_source(ws, agent, outside)
    fsutil.agent_source(ws, human, outside)  # a human: as before
    fsutil.agent_source(ws, human, rec)
    from orch.core.ops import Ops
    t = Ops(ws, human).new("t")
    assert Ops(ws, human).artifact_add(t.id, outside).name == "elsewhere.txt"
    with pytest.raises(ValidationError, match="outside the workspace"):
        Ops(ws, agent).artifact_add(t.id, outside, name="other.txt")


def test_a_plan_on_the_epic_itself_is_refused(fws, fa, fh):
    from orch.errors import UsageError
    eid, d = _epic(fws, fa, fh)
    with pytest.raises(UsageError, match="has no plan of its own"):
        fa.set_section(eid, "Plan", "anything")
    assert epics.delegation(fws, store.load(fws, eid)[1])["active"]


def test_a_claim_records_the_bound_session_id_in_the_ticket_and_that_grants_nothing(fws, fa, fh, human, fake,
                                                                                     monkeypatch):
    """Pins today's behaviour (docs/factory.md, "Session binding"): the runner writes the id nowhere, but the agent's
    own claim puts the full id into the ticket's frontmatter; a process outside the session's tree gets nothing."""
    eid, d = _epic(fws, fa, fh)
    cid = _child(fa, eid)
    _tick(fws, human, fake)
    (b,) = fs.bindings(fws)
    path = store.load(fws, cid)[0]
    assert b["session"] not in path.read_text(encoding="utf-8")  # the runner wrote it nowhere
    _session_ops(fws, b).claim(cid)
    t = store.load(fws, cid)[1]
    assert t.meta["claim"]["session"] == b["session"] and any(s["id"] == b["session"] for s in t.meta["sessions"])
    assert permits.hook_decision(fws, _payload(b["session"])) is not None  # inside the session's process tree
    monkeypatch.setattr(fs, "chain_pids", lambda: {1})  # the id copied into another process
    assert permits.hook_decision(fws, _payload(b["session"])) is None


# -- fail closed, and one scope for every change a factory session makes ----------------------------------------------

def _old(ws, tid):
    """Make ticket `tid` older than any binding (its `created` stamp in 2000)."""
    path, t = store.load(ws, tid)
    t.meta["created"] = "2000" + str(t.meta["created"])[4:]
    store.save(ws, t, path)


def test_agent_source_fails_closed(ws, human, tmp_path, monkeypatch):
    from orch.core import fsutil
    from orch.core.events import Actor
    from orch.errors import ValidationError
    inside = ws.root / "inside.md"
    inside.write_text("x", encoding="utf-8")
    unbound = Actor("agent", "claude-code", "cli", "7f3c9a21-0000")
    fsutil.agent_source(ws, unbound, inside)  # a conclusive "no binding": as before
    for bad in (ws.root / "missing.md", ws.root / "broken-link"):
        if bad.name == "broken-link":
            bad.symlink_to(ws.root / "nowhere")
        with pytest.raises(ValidationError, match="cannot be resolved"):
            fsutil.agent_source(ws, unbound, bad)
        fsutil.agent_source(ws, human, inside)  # humans: unchanged
    agent = _bound_session(ws, human)
    fsutil.agent_source(ws, agent, inside)
    with monkeypatch.context() as m:  # never monkeypatch.undo(): it would drop conftest's isolated config dirs
        m.setattr(fs, "trusted", lambda *a: (_ for _ in ()).throw(OSError("ps failed")))
        with pytest.raises(ValidationError, match="cannot tell whether this session"):
            fsutil.agent_source(ws, agent, inside)  # the lookup raised
    (fs._root() / "sessions" / f"{UUID}.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError, match="cannot tell whether this session"):
        fsutil.agent_source(ws, agent, inside)  # a malformed binding record
    from orch.core.ops import Ops
    t = Ops(ws, human).new("t")
    with pytest.raises(ValidationError, match="cannot tell whether this session"):
        Ops(ws, agent).log(t.id, "x")  # and no change goes through either
    Ops(ws, human).log(t.id, "x")


def test_cli_agent_with_a_malformed_binding_is_refused_a_file(ws, human, capsys, monkeypatch):
    inside = ws.root / "r.md"
    inside.write_text("r", encoding="utf-8")
    _bound_session(ws, human, monkeypatch)
    (fs._root() / "sessions" / f"{UUID}.json").write_text("[]", encoding="utf-8")
    code, out = _run(capsys, "new", "-t", "x", "--requirements-file", str(inside))
    assert code != 0 and "cannot tell whether this session" in out.err


# every Ops method that takes a ticket ref and is reachable by an agent changes it through one of these, and each of
# them runs the scope check (_in_bound_epic) on the loaded ticket before it changes anything
_ROUTES = re.compile(r"self\.(_mutate|_mutate_events|_edit_tasks|_task_move|_write_section|_artifact_add|approve)\(")
_UNSCOPED = {"replace_raw": "human only: require_human refuses every agent before it reads the ticket"}


def test_every_ticket_mutator_goes_through_the_scope_check():
    import inspect
    from orch.core.ops import Ops
    for helper in ("_mutate", "_mutate_events"):
        assert "self._in_bound_epic(ticket" in inspect.getsource(getattr(Ops, helper)), helper
    assert "self._mutate(" in inspect.getsource(Ops._edit_tasks)
    assert "self._mutate(" in inspect.getsource(Ops._write_section)
    assert "self._edit_tasks(" in inspect.getsource(Ops._task_move)
    assert "self._mutate(" in inspect.getsource(Ops._artifact_add)
    names = [n for n, f in inspect.getmembers(Ops, inspect.isfunction)
             if not n.startswith("_") and list(inspect.signature(f).parameters)[1:2] == ["ref"]]
    assert len(names) > 25
    for n in names:
        src = inspect.getsource(getattr(Ops, n))
        assert n in _UNSCOPED or _ROUTES.search(src), f"{n} changes a ticket without the factory scope check"
    for n, why in _UNSCOPED.items():
        assert "require_human(" in inspect.getsource(getattr(Ops, n)), (n, why)


def _loose(ws, tid):
    d = ws.artifacts_dir / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / "loose.txt").write_text("x", encoding="utf-8")


def _calls(ws):
    inside = ws.root / "proof.txt"
    inside.write_text("proof", encoding="utf-8")
    return {
        "log": lambda o, t: o.log(t, "x"), "set_section": lambda o, t: o.set_section(t, "Context", "x"),
        "append_section": lambda o, t: o.append_section(t, "Context", "x"), "set_state": lambda o, t: o.set_state(t, "x"),
        "set_extra": lambda o, t: o.set_extra(t, "x-k", "v"), "link": lambda o, t: o.link(t, external="ABC-1"),
        "claim": lambda o, t: o.claim(t), "release": lambda o, t: o.release(t), "move": lambda o, t: o.move(t, "waiting"),
        "task_add": lambda o, t: o.task_add(t, [{"text": "x"}]), "task_start": lambda o, t: o.task_start(t, "T1"),
        "task_done": lambda o, t: o.task_done(t, "T1", "x"), "task_skip": lambda o, t: o.task_skip(t, "T1", "x"),
        "task_block": lambda o, t: o.task_block(t, "T1", "x"), "task_reopen": lambda o, t: o.task_reopen(t, "T1", "x"),
        "task_edit": lambda o, t: o.task_edit(t, "T1", text="y"),
        "artifact_add": lambda o, t: o.artifact_add(t, inside, name="p.txt"),
        "artifact_link": lambda o, t: o.artifact_link(t, "https://example.com/run"),
        "artifact_scan": lambda o, t: (_loose(ws, t), o.artifact_scan(t)),  # a loose file: something to link
        "ask": lambda o, t: o.ask(t, [{"text": "Which?", "why": "w", "options": [{"label": "A"}, {"label": "B"}],
                                       "recommended": "A"}]),
        "epic_auto_approve": lambda o, t: o.epic_auto_approve(t),
        "follow-up": lambda o, t: o.new("follow-up", from_ref=t),
    }


def test_a_factory_session_changes_only_its_epic_its_children_and_what_it_filed(fws, fa, fh, human, fake):
    from orch.errors import ValidationError
    e1, _ = _epic(fws, fa, fh)
    c1 = _child(fa, e1)
    e2, _ = _epic(fws, fa, fh, title="Second")
    other_child = _child(fa, e2)
    old = fa.new("an old backlog ticket")
    _old(fws, old.id)
    _tick(fws, human, fake)
    w = _session_ops(fws, next(b for b in fs.bindings(fws) if b["child"] == c1))
    for name, call in _calls(fws).items():
        for target in (other_child, old.id):
            try:
                call(w, target)
            except ValidationError as e:
                assert "works on epic" in str(e), (name, target, e)
            else:
                raise AssertionError(f"{name} on {target} was not refused")
    # its own flow: its child, and a follow-up it files and then refines
    w.claim(c1)
    w.log(c1, "working")
    w.task_add(c1, [{"text": "build it"}])
    w.task_start(c1, "T1")
    fu = w.new("follow-up for later", from_ref=c1)
    w.set_section(fu.id, "Requirements", "r")
    w.set_section(fu.id, "Acceptance criteria", "- [ ] a")
    w.log(fu.id, "filed while working on the child")
    w.set_state(fu.id, "waits for the human's review")
    w.log(e1, "a note on the epic")
    # unbound agents and humans: as before
    fa.log(old.id, "x")
    fa.log(other_child, "x")
    from orch.core.ops import Ops
    Ops(fws, human).log(old.id, "x")


def test_a_ticket_in_another_epic_stays_refused_even_when_new(fws, fa, fh, human, fake):
    from orch.errors import ValidationError
    e1, _ = _epic(fws, fa, fh)
    e2, _ = _epic(fws, fa, fh, title="Second")
    _tick(fws, human, fake)
    p1 = _session_ops(fws, next(b for b in fs.bindings(fws) if b["epic"] == e1))
    fresh = fa.new("new child of the other epic", epic=e2)  # created after the binding, but in another epic
    with pytest.raises(ValidationError, match="works on epic"):
        p1.set_section(fresh.id, "Requirements", "r")
    with pytest.raises(ValidationError, match="works on epic"):
        p1.log(e2, "x")  # another epic itself


# -- review 6: regressions for the reviewer's probes ---------------------------------------------------------------------

def test_an_external_key_on_its_own_epic_adopts_nothing(fws, fa, fh, human, fake):
    from orch.core.ops import Ops
    from orch.errors import ValidationError
    e1, _ = _epic(fws, fa, fh)
    c1 = _child(fa, e1)
    victim = Ops(fws, human).new("someone else's old ticket")
    path, t = store.load(fws, victim.id)
    t.meta["parent"] = "PROJ-12"  # e.g. a tracker's epic key from an import; no ticket has that id
    store.save(fws, t, path)
    _tick(fws, human, fake)
    w = _session_ops(fws, next(b for b in fs.bindings(fws) if b["child"] == c1))
    w.link(e1, external="PROJ-12")  # its own epic may carry the key ...
    assert epics.parent_epic(fws, store.load(fws, victim.id)[1]) is None  # ... parents resolve by ticket id only
    assert [e.id for e in epics.children(fws, e1)] == [c1]
    for call in (lambda: w.set_section(victim.id, "Requirements", "x"), lambda: w.log(victim.id, "x")):
        with pytest.raises(ValidationError, match="works on epic"):
            call()


def test_tickets_others_file_during_the_run_stay_out_of_reach(fws, fa, fh, human, fake):
    from orch.core.ops import Ops
    from orch.errors import ValidationError
    e1, _ = _epic(fws, fa, fh)
    c1 = _child(fa, e1)
    e2, _ = _epic(fws, fa, fh, title="Second")
    c2 = _child(fa, e2)
    _tick(fws, human, fake)
    w1 = _session_ops(fws, next(b for b in fs.bindings(fws) if b["child"] == c1))
    w2 = _session_ops(fws, next(b for b in fs.bindings(fws) if b["child"] == c2))
    humans = Ops(fws, human).new("the human's own new backlog idea")
    theirs = w2.new("E2's worker's follow-up", from_ref=c2)
    for target in (humans.id, theirs.id):
        with pytest.raises(ValidationError, match="works on epic"):
            w1.set_section(target, "Requirements", "rewritten by E1's worker")
    w2.set_section(theirs.id, "Requirements", "its own follow-up")  # the session that created it may refine it
    mine = w1.new("E1's own note for later")
    w1.set_section(mine.id, "Requirements", "r")
    path, t = store.load(fws, humans.id)
    t.meta["created"] = store.load(fws, mine.id)[1].meta["created"]  # a forged stamp no longer helps
    store.save(fws, t, path)
    with pytest.raises(ValidationError, match="works on epic"):
        w1.log(humans.id, "x")


def test_a_hard_link_into_the_workspace_is_refused(ws, human, tmp_path, capsys, monkeypatch):
    from orch.core.ops import Ops
    from orch.errors import ValidationError
    secret = tmp_path / "home" / ".ssh" / "id_ed25519"
    secret.parent.mkdir(parents=True)
    secret.write_text("PRIVATE KEY", encoding="utf-8")
    hl = ws.root / "orchestrator" / "temporary" / "notes.txt"
    hl.parent.mkdir(parents=True, exist_ok=True)
    os.link(secret, hl)
    agent = _bound_session(ws, human, monkeypatch)
    t = Ops(ws, human).new("t")
    with pytest.raises(ValidationError, match="more than one hard link"):
        Ops(ws, agent).artifact_add(t.id, hl, name="n.txt")
    code, out = _run(capsys, "section", "set", t.id, "Context", "--file", str(hl))
    assert code != 0 and "more than one hard link" in out.err
    assert "PRIVATE KEY" not in store.load(ws, t.id)[0].read_text(encoding="utf-8")
    ok = ws.root / "orchestrator" / "temporary" / "ok.txt"
    ok.write_text("fine", encoding="utf-8")
    assert Ops(ws, agent).artifact_add(t.id, ok, name="ok.txt").read_text(encoding="utf-8") == "fine"
    _ok(capsys, "section", "set", t.id, "Context", "--file", str(ok))
    assert "fine" in store.load(ws, t.id)[1].section("Context")


@pytest.mark.parametrize("swap", ["symlink", "other file"])
def test_a_swap_between_the_check_and_the_open_is_refused(ws, human, tmp_path, monkeypatch, swap):
    from orch.core import fsutil
    from orch.errors import ValidationError
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE", encoding="utf-8")
    f = ws.root / "notes.txt"
    f.write_text("fine", encoding="utf-8")
    agent = _bound_session(ws, human)
    real_open = os.open

    def swapping_open(path, flags, *a, **kw):
        if str(path) == str(f.resolve()):
            f.unlink()
            if swap == "symlink":
                f.symlink_to(secret)
            else:
                f.write_text("PRIVATE", encoding="utf-8")  # a new file under the same name: another inode
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", swapping_open)
    with pytest.raises(ValidationError, match="cannot be opened without following a link|changed while orch checked"):
        fsutil.agent_source(ws, agent, f)


def test_new_fails_closed_for_an_unverifiable_binding(ws, human):
    from orch.core.ops import Ops
    from orch.errors import ValidationError
    agent = _bound_session(ws, human)
    (fs._root() / "sessions" / f"{UUID}.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError, match="cannot tell"):
        Ops(ws, agent).new("created although orch cannot tell whether this is a factory session")
    assert store.scan(ws) == []


def test_a_permission_request_names_its_own_epic_only(fws, fa, fh, human, fake):
    from orch.errors import ValidationError
    e1, _ = _epic(fws, fa, fh)
    c1 = _child(fa, e1)
    e2, _ = _epic(fws, fa, fh, title="Second")
    c2 = _child(fa, e2)
    _tick(fws, human, fake)
    w1 = _session_ops(fws, next(b for b in fs.bindings(fws) if b["child"] == c1))
    with pytest.raises(ValidationError, match=f"works on epic {e1}"):
        permits.request(fws, w1.actor, store.load(fws, c2)[1], "make deploy", reason="E2 needs it")
    assert permits.request(fws, w1.actor, store.load(fws, c1)[1], "make deploy")["epic"] == e1
    assert permits.request(fws, fa.actor, store.load(fws, c2)[1], "make deploy")["epic"] == e2  # unbound: as before

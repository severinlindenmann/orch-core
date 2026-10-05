"""AI Factory: the planner session that splits a childless epic, and the Dark baseline (docs/factory.md, "The planner"
and "The baseline"). A fake launcher stands in for tmux: no test starts a real agent or touches a real tmux server."""
import json
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


def test_the_first_child_does_not_stop_the_planner_and_no_planner_starts_after_it(fws, fa, fh, human, fake):
    eid, d = _epic(fws, fa, fh)
    _tick(fws, human, fake)
    (p,) = fs.bindings(fws)
    cid = _child(fa, eid)  # the planner's work
    _tick(fws, human, fake)
    assert sorted(b["child"] for b in fs.bindings(fws)) == sorted([eid, cid])  # it goes on; the child starts
    permits.hook_decision(fws, _payload(p["session"]))
    fake.names.discard(p["name"])
    _tick(fws, human, fake)
    (r,) = permits.open_requests(fws)
    permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])  # would wake a childless planner
    _tick(fws, human, fake)
    assert [b["child"] for b in fs.bindings(fws)] == [cid] and fs.planner_runs(fws, d["id"]) == 1


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
                'orch section set L-0009 Plan -m "Add the export, then test it."', "orch epic auto-approve L-0009"):
        assert _behavior(permits.hook_decision(dws, _payload(b["session"], cmd))) == "allow", cmd
    for cmd in (f"orch approve {eid} requirements", f"orch move {eid} done", "orch dark profile add --baseline",
                "orch section set L-0009 Plan --file plan.md", 'orch log L-0009 -m "a (note)"', "make deploy"):
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
    added = dark_profile.add_baseline(ws, human)
    assert len(added) == len(dark_profile.BASELINE) - 1 and len(dark_profile.rules(ws)) == len(dark_profile.BASELINE)
    n = len(ledger.entries(ws))
    assert dark_profile.add_baseline(ws, human) == [] and len(ledger.entries(ws)) == n
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
    assert json.loads(_ok(capsys, "dark", "profile", "add", "--baseline", "--json")) == {"added": []}


# -- the prompt names only what the CLI has, and what the baseline runs ---------------------------------------------------

def test_every_command_in_the_planner_prompt_exists_and_the_baseline_runs_it(dws_baseline):
    prompt = factory_runner.planner_prompt("L-0001")
    snippets = [s for s in re.findall(r"`([^`]+)`", prompt) if s.startswith("orch ")]
    assert len(snippets) >= 6
    for s in snippets:
        words = s.split()
        cut = next((i for i in range(len(words), 0, -1) if _leaf(words[1:i]) is not None), None)
        assert cut, s
        cmd = _leaf(words[1:cut])
        opts = {o for p in cmd.params for o in (*p.opts, *p.secondary_opts)}
        for w in words[cut:]:
            if w.startswith("-"):
                assert w in opts, (s, w)
        if words[1] != "ask":  # named only as refused
            real = s.replace("CHILD", "L-0002").replace("FILE", "r.md").replace("SIZE", "s")
            assert dark_profile.match(dws_baseline, real) is not None, real

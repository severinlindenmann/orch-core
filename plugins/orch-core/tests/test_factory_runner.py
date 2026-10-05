"""AI Factory, phase 4 (#2, #31): the runner and the session binding. A fake launcher stands in for tmux: no test
starts a real agent or touches a real tmux server."""
import json
from pathlib import Path
import os
from datetime import timedelta

import pytest

from orch import actor as orch_actor
from orch.core import epics, factory_runner, factory_sessions as fs, ledger, permits, store
from orch.core.events import Actor
from orch.dashboard import launch
from orch.errors import HumanOnlyError, ValidationError

_REAL_BLOCKER = factory_runner.user_settings_blocker  # the autouse fixture below stands in for these two
_REAL_RESOLVE = factory_runner.resolve_bin
UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class Fake:
    """list, start and stop sessions in memory"""
    def __init__(self):
        self.names, self.started, self.stopped = set(), [], []

    def alive(self):
        return set(self.names)

    def start(self, name, cwd, argv):
        self.names.add(name)
        self.started.append((name, cwd, argv))
        return 4242

    def stop(self, name):
        self.names.discard(name)
        self.stopped.append(name)


def _refine(ops, tid, plan="1. do it"):
    ops.set_section(tid, "Requirements", "r")
    ops.set_section(tid, "Acceptance criteria", "- [ ] a")
    if plan:
        ops.set_section(tid, "Plan", plan)


@pytest.fixture
def fws(configure):
    return configure(factory={"enabled": True})


@pytest.fixture
def fa(fws, agent):
    from orch.core.ops import Ops
    return Ops(fws, agent)


@pytest.fixture
def fh(fws, human):
    from conftest import human_ops
    return human_ops(fws, human)


@pytest.fixture(autouse=True)
def _trusted_programs(monkeypatch):
    """No real programs or user settings are looked at: every program resolves under /opt/test and the user-scope
    settings are fine, unless a test says otherwise."""
    monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: f"/opt/test/{os.path.basename(name)}")
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: None)


@pytest.fixture
def fake():
    return Fake()


def _started(fws, fa, fh, n=1, arm=True, **limits):
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True, **limits})
    kids = []
    for i in range(n):
        c = fa.new(f"child {i}", epic=e.id)
        _refine(fa, c.id)
        fa.epic_auto_approve(c.id)
        kids.append(c.id)
    d = epics.delegation(fws, store.load(fws, e.id)[1])
    if arm:
        fs.arm(fws, fh.actor, d["id"])
    return e.id, kids, d


def _tick(fws, human, fake, settings=None):
    return factory_runner.tick(fws, human, fake, settings=settings or launch.load_settings())


def _payload(session, command="make e2e"):
    return {"session_id": session, "tool_name": "Bash", "tool_input": {"command": command}}


def _behavior(out):
    return out["hookSpecificOutput"]["decision"]["behavior"] if out else None


# -- launching ----------------------------------------------------------------------------------------------------

def test_nothing_runs_until_the_human_started_it_from_the_dashboard(fws, fa, fh, human, fake):
    _started(fws, fa, fh, arm=False)
    assert _tick(fws, human, fake) == [] and not fake.started


def test_nothing_runs_with_the_factory_off(fws, fa, fh, human, fake, configure):
    _started(fws, fa, fh)
    off = configure(factory={"enabled": False})
    assert _tick(off, human, fake) == [] and not fake.started


def test_launch_binds_one_session_per_child_before_it_starts(fws, fa, fh, human, fake):
    eid, (cid,), d = _started(fws, fa, fh)
    lines = _tick(fws, human, fake)
    ((name, cwd, argv),) = fake.started
    (b,) = fs.bindings(fws)
    assert lines == [f"started {name}"] and name == b["name"] and cwd == str(fws.root)
    assert (b["epic"], b["delegation"], b["child"]) == (eid, d["id"], cid)
    i = argv.index("/opt/test/claude")
    assert argv[:2] == ["/opt/test/env", "-i"] and argv[2].startswith("PATH=/opt/test:")
    assert argv[i + 1:i + 4] == ["--setting-sources", "user", "--strict-mcp-config"]
    assert argv[i + 4] == "--session-id" and argv[i + 5] == b["session"] and cid in argv[i + 6]
    assert not any("dangerously" in a or "bypass" in a for a in argv)
    assert _tick(fws, human, fake) == [] and len(fake.started) == 1  # one session per child


def test_default_launch_command_grants_nothing_itself():
    assert launch.load_settings()["factory_command"] == launch.DEFAULT_FACTORY_COMMAND


def _cmd(*extra, tail=("--setting-sources", "user", "--strict-mcp-config", "--session-id", "{session}", "{prompt}")):
    return ["claude", *extra, *tail]


@pytest.mark.parametrize("argv,why", [
    (_cmd("--dangerously-skip-permissions"), "not an allowed"),
    (_cmd("--allowedTools", "Bash"), "not an allowed"),
    (_cmd("--allowed-tools=Bash"), "not an allowed"),
    (_cmd("--settings", "x.json"), "not an allowed"),
    (_cmd("--add-dir", "/"), "not an allowed"),
    (_cmd("--mcp-config", "x.json"), "not an allowed"),
    (_cmd("--plugin-dir", "x"), "not an allowed"),
    (_cmd("--agents", "{}"), "not an allowed"),
    (_cmd("--permission-prompt-tool", "x"), "not an allowed"),
    (_cmd("--permission-mode", "bypassPermissions"), "permission-mode"),
    (_cmd("--permission-mode=acceptEdits"), "permission-mode"),
    (_cmd("--permission-mode", "dontAsk"), "permission-mode"),
    (_cmd("--permission-mode", "auto"), "permission-mode"),
    (_cmd("--setting-sources", "user"), "more than once"),
    (_cmd("--model", "x; rm -rf ~"), "model"),
    (["claude", "--session-id", "{session}", "{prompt}"], "setting-sources"),
    (["claude", "--setting-sources", "user,project", "--strict-mcp-config", "--session-id", "{session}", "{prompt}"],
     "must be user"),
    (["claude", "--setting-sources", "user", "--session-id", "{session}", "{prompt}"], "strict-mcp"),
    (["claude", "--setting-sources", "user", "--strict-mcp-config", "{prompt}"], "session-id"),
    (["claude", "--setting-sources", "user", "--strict-mcp-config", "--session-id", "abc", "{prompt}"], "{session}"),
    (["claude", "--setting-sources", "user", "--strict-mcp-config", "--session-id", "{session}"], "{prompt}"),
    (_cmd(tail=("--setting-sources", "user", "--strict-mcp-config", "--session-id", "{session}", "{prompt}", "x")),
     "end with"),
    (["node", "--setting-sources", "user", "--strict-mcp-config", "--session-id", "{session}", "{prompt}"], "claude"),
    (["sh", "-c", "claude"], "claude"),
    ([], "non-empty"),
])
def test_launch_command_outside_the_allowlist_is_refused(argv, why):
    path = launch.factory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"command": argv}), encoding="utf-8")
    got, error = launch.load_factory_command()
    assert why in error
    assert got == launch.DEFAULT_FACTORY_COMMAND
    assert launch.load_settings()["factory_command"] == launch.DEFAULT_FACTORY_COMMAND


def test_the_allowed_arguments_pass():
    ok = ["/usr/local/bin/claude", "--model", "claude-opus-4", "--verbose", "--permission-mode=plan",
          "--setting-sources=user", "--strict-mcp-config", "--session-id", "{session}", "{prompt}"]
    assert launch.factory_command_error(ok) is None
    assert launch.factory_command_error(launch.DEFAULT_FACTORY_COMMAND) is None


def test_a_damaged_launch_file_means_the_default(fws):
    path = launch.factory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for raw in ("{", "[]", '{"command": ["a"], "x": 1}', '{"command": "claude"}'):
        path.write_text(raw, encoding="utf-8")
        assert launch.load_factory_command()[0] == launch.DEFAULT_FACTORY_COMMAND


def test_launch_command_is_configurable_per_user(fws, fa, fh, human, fake):
    path = launch.factory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"command": ["claude", "--model", "my-model", "--setting-sources=user", "--strict-mcp-config", "--session-id={session}", "{prompt}"]}), encoding="utf-8")
    _started(fws, fa, fh)
    _tick(fws, human, fake)
    ((_, _, argv),) = fake.started
    assert f"--session-id={fs.bindings(fws)[0]['session']}" in argv and "my-model" in argv


def test_concurrency_cap_is_three_and_config_only_lowers_it(fws, fa, fh, human, fake, configure):
    _started(fws, fa, fh, n=5)
    _tick(fws, human, fake)
    assert len(fake.started) == 3
    assert factory_runner.concurrency(configure(factory={"enabled": True, "max_concurrency": 1})) == 1
    assert factory_runner.concurrency(configure(factory={"enabled": True, "max_concurrency": 99})) == 3
    assert factory_runner.concurrency(configure(factory={"enabled": True, "max_concurrency": "9"})) == 3


def test_children_the_budget_does_not_cover_are_not_started(fws, fa, fh, human, fake):
    eid, (c1, c2), d = _started(fws, fa, fh, n=2, max_children=2)
    # two other children of this delegation were started already: the signed limit of 2 is reached
    fs.mark_run(fws, d["id"], "X-1")
    fs.mark_run(fws, d["id"], "X-2")
    _tick(fws, human, fake)
    assert not fake.started


def test_children_the_human_or_the_size_rules_hold_back_are_left_alone(fws, fa, fh, human, fake):
    eid, (c1,), d = _started(fws, fa, fh)
    big = fa.new("big", epic=eid, size="l")
    _refine(fa, big.id)
    pending = fa.new("not approved", epic=eid)
    _refine(fa, pending.id)
    _tick(fws, human, fake)
    assert [b["child"] for b in fs.bindings(fws)] == [c1]


def test_launch_failure_leaves_no_trusted_binding(fws, fa, fh, human, fake):
    _started(fws, fa, fh)
    fake.start = lambda *a: (_ for _ in ()).throw(OSError("no tmux"))
    assert "could not start" in _tick(fws, human, fake)[0]
    assert fs.bindings(fws) == []


# -- session binding (#31) ----------------------------------------------------------------------------------------

def test_hook_uses_the_binding_and_ignores_claims(fws, fa, fh, human, fake):
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, fake)
    (b,) = fs.bindings(fws)
    out = permits.hook_decision(fws, _payload(b["session"]))
    assert _behavior(out) == "deny" and "P-" in out["hookSpecificOutput"]["decision"]["message"]
    (r,) = permits.open_requests(fws)
    assert (r["epic"], r["ticket"]) == (eid, cid)
    # a session claiming the very child under another id, or an unbound one, gets no factory treatment
    spoof = "99999999-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    from orch.core.ops import Ops
    Ops(fws, Actor("agent", "claude-code", "cli", spoof)).claim(cid)
    assert permits.hook_decision(fws, _payload(spoof, "make other")) is None
    assert len(permits.open_requests(fws)) == 1


def test_hook_allows_a_granted_command_only_for_the_bound_session(fws, fa, fh, human, fake):
    _started(fws, fa, fh)
    _tick(fws, human, fake)
    (b,) = fs.bindings(fws)
    permits.hook_decision(fws, _payload(b["session"]))
    (r,) = permits.open_requests(fws)
    permits.permit_grant(fws, human, r["id"], "epic", expected_sha=r["sha"])
    assert _behavior(permits.hook_decision(fws, _payload(b["session"]))) == "allow"
    assert permits.hook_decision(fws, _payload(UUID)) is None


def test_a_binding_for_another_delegation_or_epic_answers_nothing(fws, fa, fh, human):
    eid, (cid,), d = _started(fws, fa, fh)
    fs.bind(fws, human, session=UUID, epic=eid, delegation="sha256:old", child=cid, name="fx-x")
    fs.set_pid(fws, human, UUID, 4242)
    assert permits.hook_decision(fws, _payload(UUID)) is None


def test_ledger_cut_denies_a_bound_session_and_nobody_else(fws, fa, fh, human, fake, monkeypatch):
    _started(fws, fa, fh)
    _tick(fws, human, fake)
    (b,) = fs.bindings(fws)
    monkeypatch.setattr(ledger, "head_ok", lambda: False)
    assert _behavior(permits.hook_decision(fws, _payload(b["session"]))) == "deny"
    assert permits.hook_decision(fws, _payload(UUID)) is None


def test_binding_is_written_only_by_a_human_process_and_once(fws, human, agent, monkeypatch):
    args = dict(session=UUID, epic="E-1", delegation="d", child="C-1", name="fx-C-1")
    with pytest.raises(HumanOnlyError):
        fs.bind(fws, agent, **args)
    with pytest.raises(HumanOnlyError):
        fs.arm(fws, agent, "d")
    # a human actor built by code that runs under an agent harness is refused as well
    monkeypatch.setattr(orch_actor, "process_chain", lambda: [(1, "/opt/homebrew/bin/claude --x")])
    with pytest.raises(HumanOnlyError):
        fs.bind(fws, human, **args)
    with pytest.raises(HumanOnlyError):
        fs.arm(fws, human, "d")
    assert fs.binding(fws, UUID) is None and not fs.armed(fws, "d")
    monkeypatch.setattr(orch_actor, "process_chain", lambda: [])
    fs.bind(fws, human, **args)
    with pytest.raises(ValidationError, match="bound already"):
        fs.bind(fws, human, **args)
    with pytest.raises(ValidationError, match="UUID"):
        fs.bind(fws, human, **{**args, "session": "7f3c9a21-0000"})


def test_the_runner_refuses_an_agent_harness(fws, fa, fh, human, agent, fake, monkeypatch):
    _started(fws, fa, fh)
    with pytest.raises(HumanOnlyError):
        _tick(fws, agent, fake)
    monkeypatch.setattr(orch_actor, "process_chain", lambda: [(1, "claude")])
    with pytest.raises(HumanOnlyError):
        _tick(fws, human, fake)
    assert not fake.started and fs.bindings(fws) == []


def test_damaged_or_foreign_bindings_are_not_bindings(fws, fa, fh, human, configure):
    eid, (cid,), d = _started(fws, fa, fh)
    fs.bind(fws, human, session=UUID, epic=eid, delegation=d["id"], child=cid, name="fx-x")
    fs.set_pid(fws, human, UUID, 4242)
    path = fs._root() / "sessions" / f"{UUID}.json"
    good = json.loads(path.read_text(encoding="utf-8"))
    for bad in ("", "{", "[]", json.dumps({**good, "workspace": "0" * 16}), json.dumps({**good, "extra": "x"}),
                json.dumps({**good, "session": "11111111-bbbb-4ccc-8ddd-eeeeeeeeeeee"}),
                json.dumps({**good, "child": 5})):
        path.write_text(bad, encoding="utf-8")
        assert fs.binding(fws, UUID) is None, bad
    path.write_text(json.dumps(good), encoding="utf-8")
    assert fs.binding(fws, UUID) is not None
    path.unlink()
    path.symlink_to(path.parent / "elsewhere")
    assert fs.binding(fws, UUID) is None  # never through a link
    assert fs.binding(fws, "../../ledger") is None and fs.binding(fws, None) is None


@pytest.mark.parametrize("cmd", [
    "rm {base}/permits/sessions/{u}.json", "cat {base}/permits/armed/x", "touch $ORCH_STATE_DIR/permits/runs/x",
    "echo x > ~/.config/orch/permits/sessions/{u}.json",
])
def test_guard_keeps_agents_away_from_the_runner_records(ws, cmd):
    from orch.core.ledger import base_dir
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd.format(base=base_dir(), u=UUID)},
                      "cwd": str(ws.root)})
    assert not d.allow
    f = evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(base_dir() / "permits" / "sessions" / f"{UUID}.json"),
                                                          "content": "{}"}, "cwd": str(ws.root)})
    assert not f.allow


def test_runner_records_are_never_grantable(fws):
    assert permits.never_grantable(fws, f"cp x $ORCH_STATE_DIR/permits/sessions/{UUID}.json")


# -- stopping -----------------------------------------------------------------------------------------------------

def _stop_pause(fws, fa, fh, eid, kids, monkeypatch, configure):
    fh.epic_pause(eid)


def _stop_edit(fws, fa, fh, eid, kids, monkeypatch, configure):
    fa.set_section(eid, "Requirements", "edited after the start")


def _stop_budget(fws, fa, fh, eid, kids, monkeypatch, configure):
    from orch import clock
    real = clock.now()
    monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))


def _stop_cut(fws, fa, fh, eid, kids, monkeypatch, configure):
    monkeypatch.setattr(ledger, "head_ok", lambda: False)


def _stop_off(fws, fa, fh, eid, kids, monkeypatch, configure):
    configure(factory={"enabled": False})  # rewrites the config the open workspace object reads again below


@pytest.mark.parametrize("stop,why", [(_stop_pause, "paused"), (_stop_edit, "changed"), (_stop_budget, "time budget"),
                                      (_stop_cut, "cut")])
def test_stop_conditions_end_the_session_and_its_binding(fws, fa, fh, human, fake, monkeypatch, configure, stop, why):
    eid, kids, d = _started(fws, fa, fh)
    _tick(fws, human, fake)
    (b,) = fs.bindings(fws)
    stop(fws, fa, fh, eid, kids, monkeypatch, configure)
    lines = _tick(fws, human, fake)
    assert why in lines[0] and fake.stopped == [b["name"]]
    assert fs.binding(fws, b["session"]) is None and fs.bindings(fws) == []
    assert permits.hook_decision(fws, _payload(b["session"])) is None
    assert _tick(fws, human, fake) == [] and len(fake.started) == 1  # and nothing starts again


def test_switching_the_factory_off_stops_every_session(fws, fa, fh, human, fake, configure):
    _started(fws, fa, fh, n=2)
    _tick(fws, human, fake)
    off = configure(factory={"enabled": False})
    lines = _tick(off, human, fake)
    assert len(fake.stopped) == 2 and all("switched off" in x for x in lines) and fs.bindings(off) == []


def test_a_finished_child_stops_its_session_and_the_others_go_on(fws, fa, fh, human, fake):
    eid, (c1, c2), d = _started(fws, fa, fh, n=2)
    _tick(fws, human, fake)
    fh.close(c1, "done elsewhere")
    lines = _tick(fws, human, fake)
    assert len(lines) == 1 and "child is done" in lines[0]
    assert [b["child"] for b in fs.bindings(fws)] == [c2] and len(fake.stopped) == 1


def test_an_epic_approved_again_stops_what_the_old_start_launched(fws, fa, fh, human, fake):
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, fake)
    fa.set_section(eid, "Requirements", "a new text")
    fh.approve(eid, "requirements", delegate={"factory": True})  # a new Start: a new delegation, not armed yet
    lines = _tick(fws, human, fake)
    assert "approved again" in lines[0] and fs.bindings(fws) == []
    assert len(fake.started) == 1  # the new delegation waits for the dashboard Start


# -- waking -------------------------------------------------------------------------------------------------------

def test_a_parked_child_wakes_after_a_grant_and_only_then(fws, fa, fh, human, fake):
    eid, (cid,), d = _started(fws, fa, fh)
    _tick(fws, human, fake)
    (b1,) = fs.bindings(fws)
    permits.hook_decision(fws, _payload(b1["session"]))  # parks: files a request
    fake.names.clear()  # the agent ended its session while it waited
    assert "ended" in _tick(fws, human, fake)[0]
    assert fs.bindings(fws) == [] and len(fake.started) == 1  # nothing it waits for changed: stays parked
    assert _tick(fws, human, fake) == []
    (r,) = permits.open_requests(fws)
    permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])
    assert _tick(fws, human, fake)[0].startswith("started")
    (b2,) = fs.bindings(fws)
    assert b2["session"] != b1["session"] and b2["child"] == cid
    assert permits.hook_decision(fws, _payload(b1["session"])) is None  # the old session's id is not trusted again


def _dark_ws(configure, human):
    from orch.core.ops import Ops
    dws = configure(factory={"enabled": True})
    Ops(dws, human).set_factory_dark(True)
    return dws


def test_a_parked_dark_child_wakes_when_the_dark_profile_changes(configure, agent, human, fake):
    from conftest import human_ops
    from orch.core import dark_profile
    from orch.core.ops import Ops
    dws = _dark_ws(configure, human)
    eid, (cid,), d = _started(dws, Ops(dws, agent), human_ops(dws, human), dark=True)
    assert d["dark"] is True
    _tick(dws, human, fake)
    (b1,) = fs.bindings(dws)
    assert _behavior(permits.hook_decision(dws, _payload(b1["session"]))) == "deny"  # not in the profile: parks
    fake.names.clear()
    assert "ended" in _tick(dws, human, fake)[0]
    assert _tick(dws, human, fake) == []  # nothing it waits for changed: stays parked
    rule = dark_profile.add(dws, human, "prefix", "make e2e")
    assert _tick(dws, human, fake)[0].startswith("started")
    (b2,) = fs.bindings(dws)
    assert _behavior(permits.hook_decision(dws, _payload(b2["session"]))) == "allow"
    fake.names.clear()
    _tick(dws, human, fake)
    assert _tick(dws, human, fake) == []
    dark_profile.remove(dws, human, rule["rule_id"])  # a removal changes what it waits for too
    assert _tick(dws, human, fake)[0].startswith("started")


def test_the_dark_profile_does_not_wake_an_ordinary_factory_child(fws, fa, fh, human, fake, configure):
    from orch.core import dark_profile
    _started(fws, fa, fh)
    _tick(fws, human, fake)
    (b1,) = fs.bindings(fws)
    permits.hook_decision(fws, _payload(b1["session"]))
    fake.names.clear()
    _tick(fws, human, fake)
    dws = _dark_ws(configure, human)
    dark_profile.add(dws, human, "prefix", "make e2e")
    assert _tick(dws, human, fake) == []


def test_flipping_the_dark_switch_alone_wakes_nothing(configure, agent, human, fake):
    from conftest import human_ops
    from orch.core import dark_profile
    from orch.core.ops import Ops
    dws = _dark_ws(configure, human)
    _started(dws, Ops(dws, agent), human_ops(dws, human), dark=True)

    def park():
        (b,) = fs.bindings(dws)
        permits.hook_decision(dws, _payload(b["session"]))
        fake.names.clear()
        _tick(dws, human, fake)
        assert _tick(dws, human, fake) == []

    _tick(dws, human, fake)
    park()
    for on in (False, True, False, True):  # toggling the switch alone: no relaunch storm
        Ops(dws, human).set_factory_dark(on)
        assert _tick(dws, human, fake) == []
    Ops(dws, agent).set_factory_dark(False)
    rule = dark_profile.add(dws, human, "prefix", "make e2e")  # a profile change wakes it, even with Dark off
    assert _tick(dws, human, fake)[0].startswith("started")
    park()
    Ops(dws, human).set_factory_dark(True)
    assert _tick(dws, human, fake) == []
    dark_profile.remove(dws, human, rule["rule_id"])  # with Dark on, too
    assert _tick(dws, human, fake)[0].startswith("started")


def test_wake_token_takes_the_profile_only_for_a_dark_charter():
    from types import SimpleNamespace
    epic = SimpleNamespace(id="L-0001")
    child = SimpleNamespace(id="L-0002", meta={}, title="c", section=lambda name: "")
    dark = [{"ticket": "L-0001", "kind": "charter", "delegate": {"factory": True, "dark": True}}]
    plain = [{"ticket": "L-0001", "kind": "charter", "delegate": {"factory": True}}]
    marks = ["mac-1"]
    t = factory_runner.wake_token
    assert t(epic, child, dark, None) == t(epic, child, plain, marks) == t(epic, child, plain, None)
    assert t(epic, child, dark, []) != t(epic, child, dark, marks) != t(epic, child, dark, marks + ["mac-2"])


def test_a_child_is_started_at_most_a_few_times(fws, fa, fh, human, fake):
    eid, (cid,), d = _started(fws, fa, fh)
    for i in range(fs.MAX_LAUNCHES + 3):
        _tick(fws, human, fake)
        if not fs.bindings(fws):
            break
        (b,) = fs.bindings(fws)
        permits.hook_decision(fws, _payload(b["session"], f"make t{i}"))
        (r,) = permits.open_requests(fws)
        permits.permit_grant(fws, human, r["id"], "once", expected_sha=r["sha"])  # a change that wakes it
        fake.names.clear()  # and the agent ends its session again
    _tick(fws, human, fake)
    assert len(fake.started) == fs.MAX_LAUNCHES


# -- the dashboard Start arms the runner --------------------------------------------------------------------------

def test_only_the_dashboard_factory_start_arms_the_runner(fws, fa, fh):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(fws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    e = fa.new("Billing revamp", type="epic")
    _refine(fa, e.id, plan=None)
    seen = epics.charter(fws, store.load(fws, e.id)[1])["content_hash"]
    r = c.post(f"/t/{e.id}/approve", data={"gate": "requirements", "seen": seen, "factory": "1"}, follow_redirects=False)
    assert "err=" not in r.headers["location"]
    d = epics.delegation(fws, store.load(fws, e.id)[1])
    assert fs.armed(fws, d["id"]) and not fs.armed(fws, "sha256:other")
    # the terminal's Start (Ops) arms nothing
    e2 = fa.new("Second", type="epic")
    _refine(fa, e2.id, plan=None)
    fh.approve(e2.id, "requirements", delegate={"factory": True})
    assert not fs.armed(fws, epics.delegation(fws, store.load(fws, e2.id)[1])["id"])


def test_run_once_does_nothing_without_tmux_or_the_factory(fws, monkeypatch):
    from orch.dashboard import factory_runner as dash_runner, terminals
    monkeypatch.setattr(terminals, "which", lambda name: None)
    assert dash_runner.run_once(fws, Fake()) == []


# -- review findings: launch inputs, session secrets, credentials, gates --------------------------------------------

def test_a_copied_session_id_gets_nothing_outside_its_own_process_tree(fws, fa, fh, human, fake, monkeypatch):
    _started(fws, fa, fh)
    _tick(fws, human, fake)
    (b,) = fs.bindings(fws)
    assert permits.hook_decision(fws, _payload(b["session"])) is not None
    monkeypatch.setattr(fs, "chain_pids", lambda: {1, 99})  # another process (e.g. an agent started with the copied id)
    assert permits.hook_decision(fws, _payload(b["session"])) is None


def test_a_binding_without_a_recorded_pid_is_not_trusted(fws, fa, fh, human):
    eid, (cid,), d = _started(fws, fa, fh)
    fs.bind(fws, human, session=UUID, epic=eid, delegation=d["id"], child=cid, name="fx-x")
    assert permits.hook_decision(fws, _payload(UUID)) is None
    with pytest.raises(ValidationError):
        fs.set_pid(fws, human, UUID, 1)
    fs.set_pid(fws, human, UUID, 4242)
    with pytest.raises(ValidationError):
        fs.set_pid(fws, human, UUID, 4343)  # once


def test_pid_is_written_only_by_a_human_process(fws, fa, fh, human, agent):
    eid, (cid,), d = _started(fws, fa, fh)
    fs.bind(fws, human, session=UUID, epic=eid, delegation=d["id"], child=cid, name="fx-x")
    with pytest.raises(HumanOnlyError):
        fs.set_pid(fws, agent, UUID, 4242)


def test_an_agent_edited_workspace_config_cannot_change_the_launch(fws, fa, fh, human, fake, configure):
    _started(fws, fa, fh)
    evil = configure(factory={"enabled": True}, agents={"prompts": {"work": "rm -rf ~ {key}"},
                                                        "harnesses": {"claude": ["evil", "{prompt}"]}},
                     dashboard={"terminal": "custom", "terminal_command": ["evil"]})
    _tick(evil, human, fake)
    ((_, _, argv),) = fake.started
    assert "evil" not in argv and "/opt/test/claude" in argv and not any("rm -rf" in a for a in argv)
    assert argv[-1] == factory_runner.factory_work_prompt("L-0002")  # the built-in one (shared checkout), never the config's


def test_ticket_text_never_reaches_the_launch(fws, fa, fh, human, fake):
    eid, (cid,), d = _started(fws, fa, fh)
    fa.set_section(cid, "Requirements", "r")  # same text; the title is what an agent could set
    fa.new("$(touch /tmp/x); `id` & rm -rf ~", epic=eid)
    _tick(fws, human, fake)
    ((_, _, argv),) = fake.started
    assert not any(c in " ".join(argv) for c in ("$(", "`id`", "rm -rf", "touch"))


def test_the_launch_command_is_not_writable_by_agents(ws):
    from orch.hooks.guard import evaluate
    path = str(launch.factory_path())
    assert not evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": path, "content": "{}"},
                             "cwd": str(ws.root)}).allow
    assert launch.factory_path().parent.name == "permits"


def test_the_agent_starts_with_an_allowlisted_environment_only(fws, fa, fh, human, fake, monkeypatch):
    for k, v in {"ORCH_DASHBOARD_TOKEN": "tok-secret", "ANTHROPIC_API_KEY": "sk-secret", "AWS_SECRET_ACCESS_KEY": "x",
                 "HOME": "/home/u", "PATH": "/usr/bin"}.items():
        monkeypatch.setenv(k, v)
    _started(fws, fa, fh)
    _tick(fws, human, fake)
    ((_, _, argv),) = fake.started
    i = argv.index("/opt/test/claude")
    assert argv[:2] == ["/opt/test/env", "-i"]
    names = {a.split("=", 1)[0] for a in argv[2:i]}
    assert names <= set(factory_runner.ENV_ALLOW) | {"PATH"} and {"HOME", "PATH"} <= names
    assert not any("secret" in a for a in argv)


def test_session_ids_stay_out_of_events_logs_names_and_pages(fws, fa, fh, human, fake, caplog):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    _started(fws, fa, fh)
    with caplog.at_level("DEBUG"):
        lines = _tick(fws, human, fake)
    (b,) = fs.bindings(fws)
    sid = b["session"]
    assert sid not in " ".join(lines) and sid not in caplog.text and sid[:8] not in b["name"]
    assert sid not in (fws.state_dir / "events.jsonl").read_text(encoding="utf-8") if (fws.state_dir / "events.jsonl").exists() else True
    permits.hook_decision(fws, _payload(sid))
    assert sid not in json.dumps([e.__dict__ for e in __import__("orch.core.events", fromlist=["x"]).read_events(fws)], default=str)
    c = TestClient(create_app(fws, "tok"))
    c.get("/?token=tok")
    for url in ("/", "/board", f"/t/{b['epic']}", f"/t/{b['child']}"):
        assert sid not in c.get(url).text
    assert sid not in "".join(p.read_text(encoding="utf-8") for p in fws.root.rglob("*.md"))


def test_session_id_comes_from_the_system_random_source():
    ids = {fs.new_session_id() for _ in range(50)}
    assert len(ids) == 50 and all(fs.SESSION_ID.match(i) for i in ids)


def _git_worktree(path, branch, common=None):
    """A linked git worktree's files (as `git worktree add` writes them): a .git gitfile naming a gitdir in the common
    dir's worktrees/ folder, with HEAD and commondir."""
    path.mkdir(parents=True)
    common = common or path.parent / "common.git"
    gitdir = common / "worktrees" / path.name
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (path / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")


def test_start_dir_is_the_childs_own_linked_worktree_only(fws, fa, fh, tmp_path):
    eid, (cid,), d = _started(fws, fa, fh)
    t = store.load(fws, cid)[1]
    root = str(fws.root.resolve())
    wt = fws.root / ".claude" / "worktrees" / "child"
    _git_worktree(wt, f"feat/{cid.lower()}")
    for v in (str(wt), ".claude/worktrees/child"):
        t.meta["worktrees"] = {"app": v}
        assert factory_runner.start_dir(fws, t) == str(wt.resolve())
    # a plain folder, another child's worktree, a default branch, a detached HEAD: never taken on the agent's word
    plain = fws.root / ".claude" / "worktrees" / "plain"
    plain.mkdir(parents=True)
    _git_worktree(fws.root / ".claude" / "worktrees" / "other", "feat/l-9999")
    _git_worktree(fws.root / ".claude" / "worktrees" / "main", "main")
    _git_worktree(fws.root / ".claude" / "worktrees" / "Master", "MASTER")
    outside = tmp_path / "elsewhere"
    _git_worktree(outside, f"feat/{cid.lower()}")
    for bad in (str(outside), "../elsewhere", "/", str(fws.root), "wt/missing", "", ".claude/worktrees/plain",
                ".claude/worktrees/other", ".claude/worktrees/main", ".claude/worktrees/Master", "src"):
        t.meta["worktrees"] = {"app": bad}
        assert factory_runner.start_dir(fws, t) == root, bad
    (fws.root / "link").symlink_to(outside)
    (fws.root / ".claude" / "worktrees" / "out").symlink_to(outside)
    for bad in ("link", ".claude/worktrees/out"):
        t.meta["worktrees"] = {"app": bad}
        assert factory_runner.start_dir(fws, t) == root, bad
    t.meta["worktrees"] = {"a": str(wt), "b": str(wt)}
    assert factory_runner.start_dir(fws, t) == root


def test_a_git_worktree_whose_branch_names_the_child_is_taken_elsewhere_in_the_workspace(fws, fa, fh):
    eid, (cid,), d = _started(fws, fa, fh)
    t = store.load(fws, cid)[1]
    mine = fws.root / "wt" / "mine"
    _git_worktree(mine, f"feat/{cid.lower()}-thing")
    t.meta["worktrees"] = {"app": str(mine)}
    assert factory_runner.start_dir(fws, t) == str(mine.resolve())
    for branch in ("main", "feat/other-thing", f"feat/{cid.lower()}9-x", f"{cid}1"):
        theirs = fws.root / "wt" / f"x{abs(hash(branch))}"
        _git_worktree(theirs, branch)
        t.meta["worktrees"] = {"app": str(theirs)}
        assert factory_runner.start_dir(fws, t) == str(fws.root.resolve()), branch


def test_a_worktree_with_settings_the_workspace_does_not_carry_is_refused_until_they_are_identical(fws, fa, fh):
    eid, (cid,), d = _started(fws, fa, fh)
    t = store.load(fws, cid)[1]
    wt = fws.root / ".claude" / "worktrees" / "child"
    _git_worktree(wt, f"feat/{cid.lower()}")
    (wt / ".claude").mkdir(parents=True)
    (wt / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    t.meta["worktrees"] = {"app": str(wt)}
    assert factory_runner.start_dir(fws, t) is None
    (fws.root / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    assert factory_runner.start_dir(fws, t) == str(wt.resolve())
    (wt / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash(*)"]}}', encoding="utf-8")
    assert factory_runner.start_dir(fws, t) is None


def test_every_launch_rechecks_all_the_gates_right_before_it_starts(fws, fa, fh, human, fake, monkeypatch):
    eid, kids, d = _started(fws, fa, fh)
    real = factory_runner._launchable

    def pause_then_say_yes(*a, **kw):
        ok = real(*a, **kw)
        fh.epic_pause(eid)  # the human pauses between the plan and the launch
        return ok

    monkeypatch.setattr(factory_runner, "_launchable", pause_then_say_yes)
    _tick(fws, human, fake)
    assert not fake.started and fs.bindings(fws) == []


@pytest.mark.parametrize("what", ["off", "cut", "unarmed", "budget", "edited", "paused"])
def test_launch_gate_fails_closed(fws, fa, fh, configure, monkeypatch, what):
    eid, kids, d = _started(fws, fa, fh)
    ws = fws
    if what == "off":
        ws = configure(factory={"enabled": False})
    elif what == "cut":
        monkeypatch.setattr(ledger, "head_ok", lambda: False)
    elif what == "unarmed":
        assert not factory_runner._gate(fws, eid, "sha256:not-armed")
    elif what == "budget":
        from orch import clock
        real = clock.now()
        monkeypatch.setattr(clock, "now", lambda: real + timedelta(hours=73))
    elif what == "edited":
        fa.set_section(eid, "Requirements", "edited")
    else:
        fh.epic_pause(eid)
    if what != "unarmed":
        assert not factory_runner._gate(ws, eid, d["id"])
    else:
        assert factory_runner._gate(fws, eid, d["id"])


@pytest.mark.parametrize("name,body", [
    (".claude/settings.json", '{"permissions": {"allow": ["Bash(*)"]}}'),
    (".claude/settings.local.json", '{"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}}'),
    (".mcp.json", '{"mcpServers": {"x": {"command": "evil"}}}'),
])
def test_a_worktree_with_agent_written_settings_is_refused(fws, fa, fh, human, fake, name, body):
    eid, (cid,), d = _started(fws, fa, fh)
    wt = fws.root / ".claude" / "worktrees" / "wt"
    _git_worktree(wt, f"feat/{cid.lower()}")
    (wt / name).parent.mkdir(parents=True, exist_ok=True)
    (wt / name).write_text(body, encoding="utf-8")
    fa.link(cid, repo="app", worktree=str(wt))
    lines = _tick(fws, human, fake)
    assert not fake.started and fs.bindings(fws) == [] and "harness settings" in lines[0]


def test_the_default_command_switches_project_settings_and_mcp_servers_off():
    argv = launch.DEFAULT_FACTORY_COMMAND
    assert argv[argv.index("--setting-sources") + 1] == "user" and "--strict-mcp-config" in argv


@pytest.mark.parametrize("cmd", [
    "cat {base}/permits/factory-command.json", "echo x > $ORCH_STATE_DIR/permits/factory-command.json",
    "echo x > ${{XDG_CONFIG_HOME}}/orch/permits/factory-command.json", "rm ~/.config/orch/permits/factory-command.json",
    "cd {base}/permits && cat factory-command.json", "cd {base} && sed -i s/a/b/ permits/factory-command.json",
    "tee {base}/permits/sessions/{u}.json < /dev/null", "python3 -c \"open('{base}/permits/armed/x','w')\"",
    "cat {base}/permits/*", "cp /dev/null {base}/permits/runs/x",
])
def test_guard_covers_every_spelling_of_the_runner_state(ws, cmd):
    from orch.core.ledger import base_dir
    from orch.hooks.guard import evaluate
    d = evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd.format(base=base_dir(), u=UUID)},
                      "cwd": str(ws.root)})
    assert not d.allow


@pytest.mark.parametrize("tool,inp", [
    ("Write", {"file_path": "{p}/factory-command.json", "content": "{}"}),
    ("Edit", {"file_path": "{p}/factory-command.json", "old_string": "a", "new_string": "b"}),
    ("MultiEdit", {"file_path": "{p}/factory-command.json", "edits": []}),
    ("NotebookEdit", {"notebook_path": "{p}/factory-command.json", "new_source": "x"}),
    ("Read", {"file_path": "{p}/sessions/" + UUID + ".json"}),
    ("Grep", {"pattern": "x", "path": "{p}/sessions"}),
    ("Glob", {"pattern": "*", "path": "{p}"}),
    ("Write", {"file_path": "{p}/../permits/armed/x", "content": ""}),
])
def test_guard_covers_the_runner_state_in_every_file_tool(ws, tool, inp):
    from orch.core.ledger import base_dir
    from orch.hooks.guard import evaluate
    p = str(base_dir() / "permits")
    got = {k: v.replace("{p}", p) if isinstance(v, str) else v for k, v in inp.items()}
    assert not evaluate(ws, {"tool_name": tool, "tool_input": got, "cwd": str(ws.root)}).allow


# -- second review: user-scope hooks, binaries, process start, tmux answers, guard probes --------------------------

def _user_settings(tmp_path, monkeypatch, data):
    d = tmp_path / "claude-home"
    d.mkdir(exist_ok=True)
    (d / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))


HOOKS = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "orch guard --hook-json"}]}],
         "PermissionRequest": [{"matcher": "*", "hooks": [{"type": "command", "command": "orch permit hook"}]}]}


def test_user_scope_settings_must_carry_orchs_hooks_for_a_launch(tmp_path, monkeypatch):
    real = _REAL_BLOCKER
    d = tmp_path / "empty-home"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    assert "user-scope" in real()  # no settings file
    (d / "settings.json").write_text("{", encoding="utf-8")
    assert real()
    for data in ({}, {"hooks": {"PreToolUse": HOOKS["PreToolUse"]}}, {"hooks": {"PermissionRequest": HOOKS["PermissionRequest"]}},
                 {"enabledPlugins": {"orch-core@x": False}}, {"enabledPlugins": {"other@x": True}}):
        _user_settings(tmp_path, monkeypatch, data)
        assert real(), data
    for data in ({"hooks": HOOKS}, {"enabledPlugins": {"orch-core@orch-core": True}}):
        _user_settings(tmp_path, monkeypatch, data)
        assert real() is None, data


def test_nothing_starts_and_live_sessions_stop_without_the_user_scope_hooks(fws, fa, fh, human, fake, monkeypatch):
    eid, kids, d = _started(fws, fa, fh, n=2)
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: "no hooks at user scope")
    lines = _tick(fws, human, fake)
    assert not fake.started and "no hooks at user scope" in lines[0]
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: None)
    _tick(fws, human, fake)
    assert len(fake.started) == 2
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: "no hooks at user scope")
    lines = _tick(fws, human, fake)
    assert len(fake.stopped) == 2 and fs.bindings(fws) == []


def test_the_epic_page_says_why_the_runner_starts_nothing(fws, fa, fh, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    eid, kids, d = _started(fws, fa, fh)
    c = TestClient(create_app(fws, "tok"))
    c.get("/?token=tok")
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: "no hooks at user scope")
    assert "The runner starts nothing: no hooks at user scope" in c.get(f"/t/{eid}").text
    monkeypatch.setattr(factory_runner, "user_settings_blocker", lambda environ=None: None)
    assert "The runner starts nothing" not in c.get(f"/t/{eid}").text


def test_the_config_dir_the_child_reads_is_the_one_checked(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/u/.claude-work")
    prefix = factory_runner.env_prefix("/opt/test/env", ["/opt/test/claude"])
    assert "CLAUDE_CONFIG_DIR=/home/u/.claude-work" in prefix


def _program(path, mode=0o755):
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture
def real_resolve():
    import types
    return types.SimpleNamespace(resolve_bin=_REAL_RESOLVE, child_path=factory_runner.child_path,
                                 env_prefix=factory_runner.env_prefix, os=os)


def test_programs_resolve_to_trusted_absolute_paths_only(tmp_path, monkeypatch, real_resolve):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    good = _program(bindir / "claude")
    monkeypatch.setenv("PATH", f"relative/dir::{bindir}:/usr/bin")  # relative and empty entries are never used
    assert real_resolve.resolve_bin("claude") == str(good)
    assert real_resolve.resolve_bin(str(good)) == str(good)
    assert real_resolve.resolve_bin("./claude") is None and real_resolve.resolve_bin("") is None
    assert real_resolve.resolve_bin("no-such-program-x") is None
    for mode in (0o775, 0o757, 0o777):
        _program(bindir / "loose", mode)
        assert real_resolve.resolve_bin("loose") is None, oct(mode)
    (bindir / "link").symlink_to(bindir / "loose")  # a link to a loose file is as loose as the file
    assert real_resolve.resolve_bin("link") is None
    (bindir / "ok-link").symlink_to(good)
    assert real_resolve.resolve_bin("ok-link") == str(bindir / "ok-link")
    monkeypatch.setenv("PATH", "relative/dir:")
    assert real_resolve.resolve_bin("claude") is None


def test_a_file_owned_by_someone_else_is_not_a_trusted_program(tmp_path, monkeypatch, real_resolve):
    good = _program(tmp_path / "claude")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(real_resolve.os, "getuid", lambda: os.stat(good).st_uid + 1000)
    assert real_resolve.resolve_bin("claude") is None


def test_the_child_path_is_fixed(tmp_path, monkeypatch, real_resolve):
    monkeypatch.setenv("PATH", "/tmp/evil:relative:/usr/bin")
    path = real_resolve.child_path("/opt/test/claude", "/opt/test/env")
    assert path == "/opt/test:/usr/bin:/bin:/usr/sbin:/sbin"
    assert "evil" not in path
    assert real_resolve.env_prefix("/opt/test/env", ["/opt/test/claude"])[2] == f"PATH={path}"


def test_an_untrusted_program_means_nothing_starts(fws, fa, fh, human, fake, monkeypatch):
    _started(fws, fa, fh)
    monkeypatch.setattr(factory_runner, "resolve_bin", lambda name: None)
    lines = _tick(fws, human, fake)
    assert not fake.started and fs.bindings(fws) == [] and "trusted path" in lines[0]


def test_a_reused_pid_is_not_the_recorded_process(fws, fa, fh, human, fake, monkeypatch):
    _started(fws, fa, fh)
    _tick(fws, human, fake)
    (b,) = fs.bindings(fws)
    assert b["pid_start"] and permits.hook_decision(fws, _payload(b["session"])) is not None
    monkeypatch.setattr(fs, "proc_start", lambda pid: "Tue Oct  5 09:00:00 2026")  # same pid, a later process
    assert permits.hook_decision(fws, _payload(b["session"])) is None
    monkeypatch.setattr(fs, "proc_start", lambda pid: None)  # gone
    assert permits.hook_decision(fws, _payload(b["session"])) is None


def test_a_pid_whose_start_cannot_be_read_is_not_recorded(fws, fa, fh, human, monkeypatch):
    eid, (cid,), d = _started(fws, fa, fh)
    fs.bind(fws, human, session=UUID, epic=eid, delegation=d["id"], child=cid, name="fx-x")
    monkeypatch.setattr(fs, "proc_start", lambda pid: None)
    with pytest.raises(ValidationError):
        fs.set_pid(fws, human, UUID, 4242)


def test_dashboard_start_ends_bindings_of_sessions_that_are_not_running(fws, fa, fh, human, fake):
    from orch.dashboard import factory_runner as dash
    _started(fws, fa, fh, n=2)
    _tick(fws, human, fake)
    gone = fs.bindings(fws)[0]
    fake.names.discard(gone["name"])
    assert dash.startup(fws, fake) == [f"{gone['name']}: its session is not running"]
    assert [b["name"] for b in fs.bindings(fws)] == [x for x in fake.names]
    assert fake.stopped == []


def test_dashboard_stop_stops_every_session_and_ends_every_binding_and_the_children_start_again(fws, fa, fh, human, fake):
    from orch.dashboard import factory_runner as dash
    _started(fws, fa, fh, n=2)
    _tick(fws, human, fake)
    assert len(dash.shutdown(fws, fake)) == 2 and fs.bindings(fws) == [] and len(fake.stopped) == 2
    _tick(fws, human, fake)
    assert len(fake.started) == 4  # after a restart each child starts again


def test_a_tmux_that_does_not_answer_changes_nothing(fws, fa, fh, human, fake):
    _started(fws, fa, fh, n=2)
    _tick(fws, human, fake)
    before = fs.bindings(fws)
    real_alive = fake.alive
    fake.alive = lambda: None
    assert _tick(fws, human, fake) == [] and fs.bindings(fws) == before and not fake.stopped
    from orch.dashboard import factory_runner as dash
    assert dash.startup(fws, fake) == [] and fs.bindings(fws) == before
    fake.alive = real_alive
    assert _tick(fws, human, fake) == []


class _Run:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


@pytest.mark.parametrize("result,want", [
    (_Run(0, "a 0\nb 0\nc 1\n"), {"a", "b"}),  # c's process ended (its pane stays for reap): not alive
    (_Run(1, "", "no server running on /x/factory"), set()),
    (_Run(1, "", "error connecting to /x/factory (No such file or directory)"), set()),
    (_Run(1, "", "protocol version mismatch"), None),
    (_Run(1, "", ""), None),
])
def test_only_a_clear_no_server_answer_means_no_sessions(monkeypatch, result, want):
    from orch.dashboard import factory_runner as dash
    monkeypatch.setattr(dash, "_tmux", lambda args, timeout=10: result)
    assert dash.TmuxLauncher().alive() == want


def test_a_tmux_failure_to_run_is_not_an_answer(monkeypatch):
    import subprocess
    from orch.dashboard import factory_runner as dash

    def boom(args, timeout=10):
        raise subprocess.TimeoutExpired("tmux", 10)

    monkeypatch.setattr(dash, "_tmux", boom)
    assert dash.TmuxLauncher().alive() is None


def test_the_factory_tmux_socket_is_inside_the_guarded_permits_folder_and_private():
    from orch.core.ledger import base_dir
    from orch.dashboard import factory_runner as dash
    sock = dash.socket_path()
    import re
    assert sock.parent.parent == base_dir() / "permits" / "tmux" and sock.name == "factory"
    assert re.fullmatch(r"[0-9a-f]{16}", sock.parent.name)  # random, so listing permits/tmux does not give it away
    assert (base_dir() / "permits" / "tmux.name").read_text(encoding="utf-8") == sock.parent.name
    assert dash.socket_path() == sock  # stable
    for d in (sock.parent, sock.parent.parent):
        assert (d.stat().st_mode & 0o777) == 0o700
    sock.parent.chmod(0o755)
    dash.socket_path()
    assert (sock.parent.stat().st_mode & 0o777) == 0o700


def test_the_launcher_runs_tmux_on_that_socket_by_its_resolved_path(monkeypatch):
    from orch.dashboard import factory_runner as dash
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return _Run(0, "12345\n")

    monkeypatch.setattr(dash.subprocess, "run", run)
    dash.TmuxLauncher().start("fx-L-1-abc", "/w", ["/opt/test/env", "-i", "PATH=/x", "/opt/test/claude", "go;"])
    for argv, kw in seen:
        assert argv[0] == "/opt/test/tmux" and argv[1] == "-S" and "/permits/tmux/" in argv[2] and argv[2].endswith("/factory")
        assert set(kw["env"]) == {"PATH", "LC_ALL"}


@pytest.mark.parametrize("cmd", [
    "tmux -L $SOCK send-keys x", "S=orch; tmux -L $S send-keys x", "T=tmux; $T -L orch ls",
    "tmux -L ${S} ls", 'tmux -L "$S" ls', "tmux -L or\\ch ls", "bash -c 'tmux -L \\o\\r\\c\\h ls'",
    "alias t=tmux; t -L orch ls", "f(){ tmux \"$@\"; }; f -L orch ls", "function f { tmux $*; }; f ls",
    "eval 'tmux -L orch ls'", "tmux -S $HOME/.config/orch/permits/tmux/factory ls", "tmux -S ./factory ls $X",
    "cd ~/.config/orch/permits/tmux && tmux -S factory ls", "tmux -S /x/permits/tmux/factory ls",
    "screen -S $N -X quit", "tmux ls `echo x`", "tmux -S $(echo /x) ls",
    "tmux -L orch ls", "tmux -S /tmp/x/orch ls",
    # parser differentials: the guard must not depend on reading bash the way bash does
    "TMUX -S /x ls $'a'", "Tmux -L $'\\x6frch' ls", "tmux -S $'\\x2f\\x78' ls", "$'\\x74mux' -S $'\\x2fx' ls",
    "tmux -S x \\\nls", "tmux ls <<< $X", "tmux ls <(echo x)",
    "tmux ls; tmux -L orch ls", "tmux ls && tmux -S /x/permits/y ls", "tmux ls & tmux -L o ls", "{ tmux ls; }", "( tmux -L x ls )", "tmux -L \"o\"\"rch\" ls",
    "t''mux -L orch ls", "tm\\ux -L orch ls", "tmux 'ls' $(echo -L orch)",
    "tmux ls\\\n -L orch", "tmux -S /x/PERMITS/y ls", "tmux -S /x/Permits/y ls", "tmux -S=./a ls", "tmux -L ./*ch ls",
    "screen -S $'q' ls",
])
def test_guard_refuses_tmux_with_a_socket_that_is_not_a_literal(ws, cmd):
    from orch.hooks.guard import evaluate
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)}).allow


def test_guard_leaves_an_agents_own_plain_tmux_alone(ws):
    from orch.hooks.guard import evaluate
    for cmd in ("tmux ls", "tmux -L mine new-session -d -s w", "tmux -S /tmp/mine.sock ls", "screen -ls", "ls -L /tmp",
                "printf '\\x41'"):
        from orch.hooks import guard
        assert not guard._mux_risky(cmd), cmd
    assert evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": "ls -L $HOME"}, "cwd": str(ws.root)}).allow


# -- third review: the guard resolves where a command runs, it does not only read its text ----------------------

def _bash(ws, cmd, cwd=None):
    from orch.hooks.guard import evaluate
    return evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd or ws.root)})


@pytest.mark.parametrize("cmd", [
    'cd "$ORCH_STATE_DIR"; cd permits; cd tmux; tmux -S factory ls',
    'cd "$ORCH_STATE_DIR" && cd p* && cd t* && tmux -S ./factory ls',
    'cd $ORCH_STATE_DIR/permits/tmux; tmux -Sfactory ls',
    'cd "${{ORCH_STATE_DIR}}"/permits; cd tmux; tmux -S ../tmux/factory ls',
    'cd "$ORCH_STATE_DIR"/../orch-state*/permits; ls',
    'cd "$ORCH_STATE_DIR"/../o*; cd p*',
    "cd {base}; ls", "pushd {base}", "cd {base}/permits", "cd {parent}; cd {name}",
    "cd {parent} && cd orch*", "cd '{base}'", 'cd "{base}"', "cd -- {base}", "cd -P {base}", "cd {base}/../{name}",
    "builtin cd {base}", "cd {base}/permits/..",
])
def test_guard_refuses_a_cd_into_the_config_dir_in_every_spelling(ws, cmd):
    from orch.core.ledger import base_dir
    base = base_dir()
    d = _bash(ws, cmd.format(base=base, parent=base.parent, name=base.name))
    assert not d.allow


def test_guard_refuses_every_command_run_from_inside_the_config_dir(ws):
    from orch.core.ledger import base_dir
    for sub in ("", "permits", "permits/tmux"):
        (base_dir() / sub).mkdir(parents=True, exist_ok=True)
        assert not _bash(ws, "ls", cwd=base_dir() / sub).allow
        assert not _bash(ws, "tmux -S factory ls", cwd=base_dir() / sub).allow
    assert _bash(ws, "ls", cwd=ws.root).allow


@pytest.mark.parametrize("cmd", [
    "tmux -Sfactory ls", "tmux -S factory ls", "tmux -S ./factory ls", "tmux -S ../tmux/factory ls",
    "tmux -f /dev/null -S factory ls", "tmux -CC -S factory attach", 'tmux -S "."/factory ls', "tmux -S=factory ls",
    "tmux -CCS factory attach", "tmux -uS factory ls", "tmux -S /x/../y/factory ls", "tmux -S //x/y ls",
    "tmux -S {base}/permits/tmux/abc/factory ls", "tmux -S {base}/x ls", "tmux -S {base}/../{name}/x ls",
    "screen -S factory -X quit", "tmux -L a/b ls", "tmux -L ../x ls", "tmux -S ls", "tmux -S",
])
def test_guard_refuses_a_tmux_socket_that_is_relative_or_odd(ws, cmd):
    from orch.core.ledger import base_dir
    assert not _bash(ws, cmd.format(base=base_dir(), name=base_dir().name)).allow


def test_guard_allows_an_absolute_literal_socket_elsewhere(ws):
    for cmd in ("tmux -S /tmp/mine/sock ls", "tmux -L mine ls", "tmux -f /dev/null -S /tmp/Mine.sock ls"):
        assert _bash(ws, cmd).allow, cmd


@pytest.mark.parametrize("cmd", [
    'ls -R "$ORCH_STATE_DIR"', 'ls "$ORCH_STATE_DIR"/../*/permits', 'ls "$ORCH_STATE_DIR"', "ls {base}/permits/tmux",
    "find {base} -type s", "find {base}/permits", "du -a {base}", "stat {base}/permits", "tree {base}", "ls {base}/p*",
    "ls -la {base}/../{name}*", "ls $ORCH_STATE_DIR/permits/tmux",
])
def test_guard_refuses_listing_the_config_dir(ws, cmd):
    from orch.core.ledger import base_dir
    base = base_dir()
    (base / "permits").mkdir(parents=True, exist_ok=True)
    assert not _bash(ws, cmd.format(base=base, parent=base.parent, name=base.name)).allow


def test_guard_leaves_ordinary_listings_alone(ws):
    for cmd in ("ls", "ls -la src", "find . -name '*.py'", "du -sh .", "ls /usr", "find /usr -maxdepth 1",
                "stat README.md"):
        assert _bash(ws, cmd).allow, cmd


def test_the_guard_says_best_effort_not_fail_closed():
    import inspect
    from orch.hooks import guard
    src = inspect.getsource(guard).split("_MUX_WORD =")[0].split("The AI Factory's own tmux server sits")[1]
    assert "Best effort" in src and "fail closed" not in src.lower()


def test_hooks_disabled_or_only_mentioned_do_not_count(tmp_path, monkeypatch):
    real = _REAL_BLOCKER

    def hooks(guard_cmd, permit_cmd):
        return {"PreToolUse": [{"hooks": [{"command": guard_cmd}]}],
                "PermissionRequest": [{"hooks": [{"command": permit_cmd}]}]}

    for data in ({"hooks": HOOKS, "disableAllHooks": True},
                 {"enabledPlugins": {"orch-core@x": True}, "disableAllHooks": True},
                 {"hooks": hooks("echo orch guard", "echo orch permit hook")},
                 {"hooks": hooks("evil orch guard", "./orch permit hook")},
                 {"hooks": hooks("orchestra guard", "orch permit hook")},
                 {"hooks": hooks("orch guard 'unterminated", "orch permit hook")}):
        _user_settings(tmp_path, monkeypatch, data)
        assert real(), data
    for guard_cmd, permit_cmd in (("orch guard --hook-json", "orch permit hook"),
                                  ("/usr/local/bin/orch guard", '"/opt/x y/bin/orch" permit hook')):
        _user_settings(tmp_path, monkeypatch, {"hooks": hooks(guard_cmd, permit_cmd), "disableAllHooks": False})
        assert real() is None, guard_cmd


def test_a_recursive_listing_over_a_folder_that_holds_the_config_dir_is_refused(ws, tmp_path, monkeypatch):
    home = tmp_path / "elsewhere"
    (home / ".config" / "orch").mkdir(parents=True)
    monkeypatch.setenv("ORCH_STATE_DIR", str(home / ".config" / "orch"))
    for cmd in (f"find {home} -name x", f"du {home}", f"ls -R {home}", f"ls -laR {home}/.config", f"tree {home}",
                f"find {home}/.config -type s"):
        assert not _bash(ws, cmd).allow, cmd
    for cmd in (f"ls {home}", f"ls -la {home}/.config", "find . -name x"):  # one level: it does not show the folder's inside
        assert _bash(ws, cmd).allow, cmd


# -- fourth review: every tool path, every segment, bounded, one resolution, deny on error --------------------------

def _tool(ws, tool, cwd=None, **inp):
    from orch.hooks.guard import evaluate
    return evaluate(ws, {"tool_name": tool, "tool_input": inp, "cwd": str(cwd or ws.root)})


@pytest.fixture
def permits_link(ws):
    from orch.core.ledger import base_dir
    (base_dir() / "permits" / "tmux").mkdir(parents=True, exist_ok=True)
    link = ws.root / "innocent"
    link.symlink_to(base_dir() / "permits")
    return link


def test_a_symlink_to_the_permits_folder_is_refused_in_every_tool_and_command(ws, permits_link):
    base = permits_link.parent
    for cmd in ("cd innocent", "ls innocent", "cat innocent/x", "ls innocent/..", "ls -R innocent/tmux", "cd innocent/tmux",
                "tmux -S innocent/tmux/factory ls", f"tmux -S {permits_link}/tmux/factory ls", f"cd '{permits_link}'", f'cat "{permits_link}/x"'):
        assert not _bash(ws, cmd).allow, cmd
    for tool, inp in (("Read", {"file_path": "innocent/x"}), ("Write", {"file_path": "innocent/x", "content": ""}),
                      ("Edit", {"file_path": str(permits_link / "x"), "old_string": "a", "new_string": "b"}),
                      ("MultiEdit", {"file_path": "innocent/x", "edits": []}),
                      ("NotebookEdit", {"notebook_path": "innocent/x.ipynb", "new_source": "x"}),
                      ("Grep", {"pattern": "x", "path": "innocent"}), ("Grep", {"pattern": "x", "path": str(permits_link)}),
                      ("Glob", {"pattern": "*", "path": "innocent/tmux"})):
        assert not _tool(ws, tool, cwd=base, **inp).allow, (tool, inp)


def test_a_symlink_to_somewhere_else_is_left_alone(ws, tmp_path):
    (tmp_path / "plain").mkdir()
    (ws.root / "ok").symlink_to(tmp_path / "plain")
    assert _bash(ws, "ls ok").allow and _bash(ws, "cd ok").allow
    assert _tool(ws, "Read", file_path="ok/x").allow


def test_relative_paths_in_file_tools_are_taken_from_the_hooks_cwd(ws):
    from orch.core.ledger import base_dir
    (base_dir() / "permits").mkdir(parents=True, exist_ok=True)
    for tool, inp in (("Read", {"file_path": "permits/x"}), ("Write", {"file_path": "./permits/x", "content": ""}),
                      ("Grep", {"pattern": "x", "path": "permits"}), ("Read", {"file_path": "ledger.jsonl"})):
        assert not _tool(ws, tool, cwd=base_dir(), **inp).allow, (tool, inp)
    assert _tool(ws, "Read", file_path="permits/x").allow  # the same words from the workspace mean something else


@pytest.mark.parametrize("cmd", [
    'echo hi; cd "$ORCH_STATE_DIR"', 'true && ls -R "$ORCH_STATE_DIR"', 'echo a | cd {base}', "echo a\\ncd {base}",
    "false || find {base} -type s", "echo ok && echo ok2 && ls {base}/permits", "(cd {base})", "{{ cd {base}; }}",
    "echo a; echo b; echo c; tmux -S factory ls", "echo a && tmux -Sfactory ls", "echo a | tmux -S ./factory ls",
    "ls . ; ls $ORCH_STATE_DIR/permits",
])
def test_the_resolution_rules_hit_in_every_command_segment_not_only_the_first(ws, cmd):
    from orch.core.ledger import base_dir
    assert not _bash(ws, cmd.format(base=base_dir()).replace("\\n", "\n")).allow


def test_a_huge_glob_near_the_config_dir_is_refused_not_walked(ws):
    from orch.core.ledger import base_dir
    many = base_dir() / "permits" / "many"
    many.mkdir(parents=True, exist_ok=True)
    for i in range(650):
        (many / f"f{i}").write_text("", encoding="utf-8")
    for cmd in ("ls $ORCH_STATE_DIR/../*/permits/many/*", "cat $ORCH_STATE_DIR/../*/permits/many/f?*", "find . ; ls $ORCH_STATE_DIR/../*/permits/m*/*"):
        d = _bash(ws, cmd)
        assert not d.allow and "limits" in d.reason, cmd
    big = ws.root / "big"
    big.mkdir()
    for i in range(650):
        (big / f"f{i}").write_text("", encoding="utf-8")
    assert _bash(ws, "ls big/*").allow  # far from the config dir: a big glob is nothing to these rules
    (ws.root / "few").mkdir()
    for i in range(5):
        (ws.root / "few" / f"f{i}").write_text("", encoding="utf-8")
    assert _bash(ws, "ls few/*").allow


def test_a_very_deep_path_and_a_very_long_command_are_refused(ws):
    deep = "/".join(["a"] * 200)
    for cmd in (f"cd {deep}", f"ls {deep}", f"cd {deep}; ls", "cd " + "x" * 5000):
        d = _bash(ws, cmd)
        assert not d.allow and "limits" in d.reason, cmd[:30]
    huge = "cd . ; " + "x" * 300_000
    assert not _bash(ws, huge).allow
    assert not _bash(ws, "echo " + "x" * 300_000).allow  # over the cap is refused whatever it says (padding)
    assert not _tool(ws, "Read", file_path="/" + "/".join(["a"] * 300)).allow
    assert not _tool(ws, "Write", file_path="a" * 5000, content="").allow


def test_an_exception_inside_the_new_rules_denies_but_an_unrelated_error_still_fails_open(ws, monkeypatch):
    from orch.hooks import guard
    for name in ("_touches_state_dir", "_mux_risky"):  # the new rules' own steps, called from _bash
        with monkeypatch.context() as m:
            m.setattr(guard, name, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
            d = _bash(ws, "ls")
            assert not d.allow and "limits" in d.reason, name
    with monkeypatch.context() as m:  # a step inside the cd/listing resolution
        m.setattr(guard, "_resolved", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
        assert not _bash(ws, "cd somewhere").allow
    with monkeypatch.context() as m:  # the file tools' resolution
        m.setattr(guard, "_ledger_path", lambda *a, **k: (_ for _ in ()).throw(OSError("bug")))
        assert not _tool(ws, "Read", file_path="README.md").allow
    # an internal error elsewhere is not the new rules' business: evaluate raises, and the hook fails open and logs
    with monkeypatch.context() as m:
        m.setattr(guard, "_runs_human_only", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
        with pytest.raises(RuntimeError):
            _bash(ws, "echo hi")


def test_the_hook_still_fails_open_on_an_unrelated_guard_error(ws_root, ws, monkeypatch):
    import orch.hooks.guard as guard
    from test_guard import _run_guard, bash

    def boom(ws, payload):
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "evaluate", boom)
    assert _run_guard(monkeypatch, {**bash("git push"), "cwd": str(ws_root)}) == 0


def test_a_path_that_cannot_be_resolved_is_refused(ws):
    (ws.root / "loop").symlink_to(ws.root / "loop")  # a link loop
    assert not _tool(ws, "Read", file_path="loop/x").allow
    assert not _bash(ws, "ls loop/x").allow


# -- fifth review: padding, nested shells, continuations, $PWD, CDPATH, cwd ------------------------------------------

def test_a_command_over_the_cap_is_refused_whatever_it_says(ws):
    from orch.hooks import guard
    pad = "x" * (guard.MAX_CMD + 10)
    for cmd in ("echo " + pad, "true; " + pad, "ls " + pad, "git status # " + pad):
        d = _bash(ws, cmd)
        assert not d.allow and "limits" in d.reason, cmd[:12]
    assert _bash(ws, "echo " + "x" * 1000).allow


@pytest.mark.parametrize("cmd", [
    "bash -c 'cd \"$ORCH_STATE_DIR\" && ls'", "sh -c \"cd {base}; tmux -S factory ls\"", "zsh -c 'cd {base}\ncd permits'",
    "eval 'cd {base}'", 'eval "cd \\"$ORCH_STATE_DIR\\""', "bash -c $'cd {base}'", "bash -lc 'echo a && cd {base}'",
    "bash -c 'D=$ORCH_STATE_DIR; cd $D'", "D=$ORCH_STATE_DIR; cd $D/permits", "D={base}; cd \"$D\"", "D={base}; ls $D",
    "cd \\\n{base}", "cd {base}\\\n/permits", "ls \\\n -R \\\n {base}", "cd\\\n {base}",
    "cd {base}\ncd .", "cd {base}\r\nls", "echo a cd {base}", "echo a\x0bcd {base}",
    "cd {parent}; cd {name}; cd -", "cd $ORCH_STATE_DIR/permits/tmux; cd ..; cd -",
    "cd $(echo {base}); cat permits/x", "cd `echo {base}` && ls tmux", "cd $UNKNOWN_VAR_X && cat ledger.jsonl",
    "cd $'\\x2f'; cat sessions/x", "CDPATH={parent} cd {name}; cat permits/x", "export CDPATH={parent}; cd {name}; ls ledger*",
    "cd $X && cat p*", "cd $X && cat se*/a", "cd $X && cat *ledger*", "cd $X && cat l*", "cd $X && tail -f ../tmux/factory",
    "cd $X && cat *", "cd $X && cat ?*", "cd $X && cp * /tmp/out", "cd $X && ls */", "cd $X && tar cf o.tgz *",
    "cd $X && grep -r foo *", "cd $X && cat remote-humans.json", "cd $X && cat launch.json", "cd $X && cat factory-command.json",
    "cd $X && cat used/abc", "cd $X && cat requests/P-1.json", "cd $X && cat armed/a", "cd $X && cat runs/a", "cd $X && cat children/a",
    "cd $X && cat tmux.name",
    "cat $ORCH_STATE_DIR/p*/tmux/*", "cat {base}/p*/*", "cat {parent}/{name}/p*", "wc -c ~/.c*/orch/p*",
    "echo \"cd {base}\" | sh", "echo cd {base} | bash",
])
def test_the_resolution_rules_see_through_nested_shells_continuations_and_variables(ws, cmd):
    from orch.core.ledger import base_dir
    base = base_dir()
    d = _bash(ws, cmd.format(base=base, parent=base.parent, name=base.name))
    assert not d.allow, cmd


def test_cdpath_from_the_hooks_environment_is_followed(ws, monkeypatch):
    from orch.core.ledger import base_dir
    monkeypatch.setenv("CDPATH", str(base_dir().parent))
    assert not _bash(ws, f"cd {base_dir().name}").allow


def test_oldpwd_in_the_hooks_environment_is_followed_by_cd_dash(ws, monkeypatch):
    from orch.core.ledger import base_dir
    monkeypatch.setenv("OLDPWD", str(base_dir() / "permits"))
    assert not _bash(ws, "cd -").allow
    assert not _bash(ws, "cd $OLDPWD").allow
    monkeypatch.setenv("OLDPWD", str(ws.root))
    assert _bash(ws, "cd -").allow


def test_the_process_cwd_counts_as_well_as_the_cwd_in_the_hook_input(ws, monkeypatch):
    from orch.core.ledger import base_dir
    (base_dir() / "permits").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(base_dir() / "permits")
    assert not _bash(ws, "ls", cwd=ws.root).allow  # the hook input says the workspace; the process is inside permits
    assert not _tool(ws, "Read", cwd=ws.root, file_path="x").allow
    monkeypatch.chdir(ws.root)
    assert _bash(ws, "ls", cwd=ws.root).allow


def test_file_tools_relative_paths_are_judged_from_both_cwds(ws, monkeypatch):
    from orch.core.ledger import base_dir
    (base_dir() / "permits" / "tmux").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(base_dir())
    assert not _tool(ws, "Grep", cwd=ws.root, pattern="x", path="permits").allow  # process cwd = config dir
    monkeypatch.chdir(ws.root)
    assert not _tool(ws, "Grep", cwd=base_dir(), pattern="x", path="permits/tmux").allow  # hook cwd = config dir


def test_a_big_glob_far_from_the_config_dir_is_not_a_problem(ws):
    big = ws.root / "node_modules"
    big.mkdir()
    for i in range(650):
        (big / f"m{i}").write_text("", encoding="utf-8")
    assert _bash(ws, "rm -rf node_modules/*").allow
    assert _bash(ws, "cat node_modules/m1*").allow


def test_pwd_is_the_simulated_working_directory(ws):
    from orch.core.ledger import base_dir
    base = base_dir()
    for cmd in (f"cd $PWD/{base.name}", f"cd ${{PWD}}/{base.name}/permits", f"ls $PWD/{base.name}", f"cd $PWD/../{base.name}"):
        cwd = base.parent if "../" not in cmd else base.parent / "x"
        (base.parent / "x").mkdir(exist_ok=True)
        assert not _bash(ws, cmd, cwd=cwd).allow, cmd


@pytest.mark.parametrize("cmd", [
    "cd $(git rev-parse --show-toplevel)", "cd `git rev-parse --show-toplevel`", "cd $UNKNOWN_VAR_X", "cd $'\\x2f'",
    "cd ${{TMPDIR:-/tmp}} && ls",
    "CDPATH= cd src && ls", "CDPATH={parent} cd {name}", "export CDPATH={parent}; cd {name}",
    'cd "$(git rev-parse --show-toplevel)" && npm test', 'cd $WT && git status', 'for d in */; do (cd "$d" && ls); done',
    'cd "$(mktemp -d)" && ls', 'cd "$ROOT/app" && pytest', 'while read l; do cd "$l"; ls; done < dirs.txt',
    'cd "$(git rev-parse --show-toplevel)"; for f in *.md; do echo $f; done', 'cd $X && rm -rf node_modules/*',
    'cd $X && cat src/*.py', 'cd $X && cat *.json', 'cd $X && echo *', 'cd $X && git add . && git log --grep="tmux and permits notes"',
    'cd $X && ls', 'cd $X && npm test', 'cd $X && cat README.md notes/todo.txt', 'cd $X && grep foo src/app.py',
    'echo "cd $(pwd)"', 'echo "cd {base}"', "printf 'cd {base}'", "git log --grep='cd {base}'",
])
def test_a_cd_the_guard_cannot_work_out_is_allowed_and_the_rest_of_the_line_is_not_blocked_for_no_reason(ws, cmd):
    from orch.core.ledger import base_dir
    base = base_dir()
    d = _bash(ws, cmd.format(base=base, parent=base.parent, name=base.name))
    assert d.allow, (cmd, d.reason)


def test_the_unknown_cwd_denial_has_its_own_plain_text_and_the_bound_text_is_for_limits(ws):
    d = _bash(ws, "cd $X && cat permits/x")
    assert not d.allow and "cannot work out" in d.reason and "limits" not in d.reason
    d = _bash(ws, "echo " + "x" * 300_000)
    assert not d.allow and "limits" in d.reason


@pytest.mark.parametrize("cmd", [
    "brew list | grep tmux", 'tmux display-message -p "#S"', "man tmux", "which tmux", "echo tmux; ls", "grep -rn screen src/",
    "cat > f <<EOF\ncd $HOME\ntmux -L $S ls\nEOF", "cat > f <<'EOF'\ntmux -S x ls; cd $X\nEOF",
    "git log --grep='document the screen reader and tmux'", "tmux send-keys -t work 'npm test' Enter", "tmux ls | cat",
    "tmux -L ls; ls -L $X", "tmux ls # -L mine", "tmux source-file ~/.tmux.conf", "tmux -V", "TMUX=1 tmux ls",
    "ps aux | grep tmux", "echo $TMUX_PANE", "screen -ls", "tmux new -s work -d",
])
def test_only_the_tmux_command_word_and_its_own_arguments_are_judged(ws, cmd):
    d = _bash(ws, cmd.replace("\\n", "\n"))
    assert d.allow, (cmd, d.reason)


def test_the_new_guard_rules_deny_nothing_a_set_of_everyday_commands_gets_without_them(ws, monkeypatch):
    """99 everyday commands (cd with substitutions, loops, tmux and screen as ordinary tools, big heredocs, listings,
    links, worktree moves). Whatever the guard says about each without the AI Factory rules, it says with them."""
    from orch.hooks import guard
    cmds = json.loads((Path(__file__).parent / "fixtures" / "guard_everyday_commands.json").read_text(encoding="utf-8"))
    assert len(cmds) >= 99
    with_rules = {c: _bash(ws, c).allow for c in cmds}
    with monkeypatch.context() as m:
        m.setattr(guard, "_touches_state_dir", lambda *a, **k: False)
        m.setattr(guard, "_mux_risky", lambda *a, **k: False)
        without = {c: _bash(ws, c).allow for c in cmds}
    differ = [c for c in cmds if with_rules[c] != without[c]]
    assert differ == []

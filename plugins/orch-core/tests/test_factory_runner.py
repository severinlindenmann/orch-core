"""AI Factory, phase 4 (#2, #31): the runner and the session binding. A fake launcher stands in for tmux: no test
starts a real agent or touches a real tmux server."""
import json
from datetime import timedelta

import pytest

from orch import actor as orch_actor
from orch.core import epics, factory_runner, factory_sessions as fs, ledger, permits, store
from orch.core.events import Actor
from orch.dashboard import launch
from orch.errors import HumanOnlyError, ValidationError

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
    i = argv.index("claude")
    assert argv[:2] == ["env", "-i"] and argv[i + 1] == "--session-id" and argv[i + 2] == b["session"] and cid in argv[i + 3]
    assert not any("dangerously" in a or "bypass" in a for a in argv)
    assert _tick(fws, human, fake) == [] and len(fake.started) == 1  # one session per child


def test_default_launch_command_grants_nothing_itself():
    assert launch.load_settings()["factory_command"] == ["claude", "--session-id", "{session}", "{prompt}"]


@pytest.mark.parametrize("argv,why", [
    (["claude", "--dangerously-skip-permissions", "--session-id", "{session}", "{prompt}"], "grant"),
    (["claude", "--permission-mode", "bypassPermissions", "{session}", "{prompt}"], "grant"),
    (["claude", "--allowedTools", "Bash", "{session}", "{prompt}"], "grant"),
    (["claude", "--settings", "x.json", "{session}", "{prompt}"], "grant"),
    (["claude", "{prompt}"], "{session}"),
    (["claude", "{session}"], "{prompt}"),
    ([], "non-empty"),
])
def test_launch_command_that_grants_itself_is_refused(argv, why):
    path = launch.factory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"command": argv}), encoding="utf-8")
    got, error = launch.load_factory_command()
    assert why in error and got == launch.DEFAULT_FACTORY_COMMAND
    assert launch.load_settings()["factory_command"] == launch.DEFAULT_FACTORY_COMMAND


def test_a_damaged_launch_file_means_the_default(fws):
    path = launch.factory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for raw in ("{", "[]", '{"command": ["a"], "x": 1}', '{"command": "claude"}'):
        path.write_text(raw, encoding="utf-8")
        assert launch.load_factory_command()[0] == launch.DEFAULT_FACTORY_COMMAND


def test_launch_command_is_configurable_per_user(fws, fa, fh, human, fake):
    path = launch.factory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"command": ["my-agent", "--id={session}", "{prompt}"]}), encoding="utf-8")
    _started(fws, fa, fh)
    _tick(fws, human, fake)
    ((_, _, argv),) = fake.started
    i = argv.index("my-agent")
    assert argv[i + 1] == f"--id={fs.bindings(fws)[0]['session']}"


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
    assert "evil" not in argv and "claude" in argv and not any("rm -rf" in a for a in argv)
    assert argv[-1] == "Work on ticket L-0002 with the orch-work-on-ticket skill."


def test_ticket_text_never_reaches_the_launch(fws, fa, fh, human, fake):
    eid, (cid,), d = _started(fws, fa, fh)
    fa.set_section(cid, "Requirements", "r")  # same text; the title is what an agent could set
    fa.new("$(touch /tmp/x); `id` & rm -rf ~", epic=eid)
    _tick(fws, human, fake)
    ((_, _, argv),) = fake.started
    assert not any(c in " ".join(argv) for c in ("$(", "`", "rm -rf", "touch"))


def test_the_launch_command_is_not_writable_by_agents(ws):
    from orch.core.ledger import base_dir
    from orch.hooks.guard import evaluate
    path = str(base_dir() / "factory.json")
    assert not evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": path, "content": "{}"},
                             "cwd": str(ws.root)}).allow
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": f"echo '{{}}' > {path}"},
                             "cwd": str(ws.root)}).allow
    assert not evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": "echo x > ~/.config/orch/factory.json"},
                             "cwd": str(ws.root)}).allow
    assert evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(ws.root / ".vscode" / "launch.json"),
                                                               "content": "{}"}, "cwd": str(ws.root)}).allow


def test_the_agent_starts_with_an_allowlisted_environment_only(fws, fa, fh, human, fake, monkeypatch):
    for k, v in {"ORCH_DASHBOARD_TOKEN": "tok-secret", "ANTHROPIC_API_KEY": "sk-secret", "AWS_SECRET_ACCESS_KEY": "x",
                 "HOME": "/home/u", "PATH": "/usr/bin"}.items():
        monkeypatch.setenv(k, v)
    _started(fws, fa, fh)
    _tick(fws, human, fake)
    ((_, _, argv),) = fake.started
    i = argv.index("claude")
    assert argv[:2] == ["env", "-i"]
    names = {a.split("=", 1)[0] for a in argv[2:i]}
    assert names <= set(factory_runner.ENV_ALLOW) and {"HOME", "PATH"} <= names
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


def test_start_dir_is_the_childs_worktree_inside_the_workspace_only(fws, fa, fh, tmp_path):
    eid, (cid,), d = _started(fws, fa, fh)
    t = store.load(fws, cid)[1]
    wt = fws.root / "wt" / "child"
    wt.mkdir(parents=True)
    t.meta["worktrees"] = {"app": str(wt)}
    assert factory_runner.start_dir(fws, t) == str(wt.resolve())
    t.meta["worktrees"] = {"app": "wt/child"}
    assert factory_runner.start_dir(fws, t) == str(wt.resolve())
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for bad in (str(outside), "../elsewhere", "/", str(fws.root), "wt/missing", ""):
        t.meta["worktrees"] = {"app": bad}
        assert factory_runner.start_dir(fws, t) == str(fws.root.resolve()), bad
    (fws.root / "link").symlink_to(outside)
    t.meta["worktrees"] = {"app": "link"}
    assert factory_runner.start_dir(fws, t) == str(fws.root.resolve())
    (wt / ".claude").mkdir()
    (wt / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    t.meta["worktrees"] = {"app": str(wt)}
    assert factory_runner.start_dir(fws, t) == str(fws.root.resolve())  # its own harness settings: not used
    t.meta["worktrees"] = {"a": str(wt), "b": str(wt)}
    assert factory_runner.start_dir(fws, t) == str(fws.root.resolve())


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

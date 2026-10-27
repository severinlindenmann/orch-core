"""``orch stop`` / ``orch start`` / ``orch services``: the plan per case, the commands per platform, the agents."""

from __future__ import annotations

import io
import json
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from orch.install import service
from orch.install.state import Layout, save
from orch.lifecycle import agents as A
from orch.lifecycle import cli
from orch.lifecycle.plan import plan_start, plan_stop
from orch.lifecycle.units import Env, Svc, discover, stopped_by_orch

WID = "ab" * 16
OTHER = "cd" * 16


def svc(key, state="running", *, name=None, installed=True, kind="launchd"):
    return Svc(key, key, "person", kind, name=name or key, installed=installed, state=state)


ALL = [svc(k) for k in ("host", "runner", "records", "dashboard", "hub", "relay", "publish")]


def keys(plan):
    return [(s.action, s.key) for s in plan.steps]


# -------------------------------------------------------------------------------------------------------- plan


def test_stop_all_orders_agents_before_host_and_server_parts_last():
    assert [k for _, k in keys(plan_stop("all", ALL, set()))] == [
        "runner", "dashboard", "host", "records", "hub", "relay", "publish",
    ]  # fmt: skip


def test_stop_all_leaves_out_what_is_not_installed_or_already_stopped_by_orch():
    svcs = [svc("host"), svc("runner", "stopped"), svc("hub", installed=False), svc("dashboard", "stopped")]
    assert keys(plan_stop("all", svcs, {"runner"})) == [("stop", "dashboard"), ("stop", "host")]


def test_stop_workspace_with_other_workspaces_only_ends_its_agents_and_says_why():
    p = plan_stop("workspace", ALL, set(), other_workspaces=2)
    assert keys(p) == [("agents", "")]
    assert "shared with 2 other workspaces" in p.notes[0] and "orch stop all" in p.notes[0]


def test_stop_workspace_alone_also_stops_the_shared_services_but_never_hub_relay_publish():
    p = plan_stop("workspace", ALL, set(), other_workspaces=0)
    assert keys(p) == [("agents", ""), ("stop", "runner"), ("stop", "dashboard"), ("stop", "host"), ("stop", "records")]


def test_start_all_reverses_the_order_and_workspace_start_skips_server_parts():
    assert [k for _, k in keys(plan_start("all", ALL))] == [
        "publish", "relay", "hub", "host", "runner", "records", "dashboard",
    ]  # fmt: skip
    assert [k for _, k in keys(plan_start("workspace", ALL))] == ["host", "runner", "records", "dashboard"]


# ---------------------------------------------------------------------------------------------- a fake machine


@dataclass
class FakeRunner:
    kind: str = "launchd"

    def __post_init__(self):
        self.up = {"hub": True, "dashboard": True}
        self.calls = []

    def pause(self, part):
        self.calls.append(("pause", part))
        self.up[part] = False

    def resume(self, part):
        self.calls.append(("resume", part))
        self.up[part] = True

    def info(self, part):
        return {"running": self.up.get(part, False), "pid": 1, "service": part}


class Machine:
    """launchctl / systemctl in memory: which labels are loaded and which are disabled."""

    def __init__(self, platform, home, labels):
        self.platform, self.home, self.labels = platform, home, labels
        self.loaded, self.disabled, self.log = set(labels), set(), []

    def run(self, argv):
        self.log.append(list(argv))
        if argv[0] == "launchctl":
            cmd, target = argv[1], argv[-1].rsplit("/", 1)[-1]
            if cmd == "print":
                return (0, "\tstate = running\n") if target in self.loaded else (113, "")
            if cmd == "disable":
                self.disabled.add(target)
            elif cmd == "enable":
                self.disabled.discard(target)
            elif cmd == "bootout":
                self.loaded.discard(target)
            elif cmd == "bootstrap":
                if Path(argv[-1]).stem in self.disabled:
                    return 5, "disabled"
                self.loaded.add(Path(argv[-1]).stem)
            return 0, ""
        verb, name = argv[2], argv[-1]
        if verb == "is-active":
            return (0, "active\n") if name in self.loaded else (3, "inactive\n")
        if verb == "disable":
            self.loaded.discard(name)
            self.disabled.add(name)
        elif verb == "enable":
            self.loaded.add(name)
            self.disabled.discard(name)
        return 0, ""


def machine(tmp_path, platform):
    home = tmp_path / "home"
    state = tmp_path / "state"
    if platform == "darwin":
        names = ["io.orch.host", "io.orch.runner", "io.orch.records"]
        for n in names:
            (home / "Library" / "LaunchAgents").mkdir(parents=True, exist_ok=True)
            (home / "Library" / "LaunchAgents" / f"{n}.plist").write_text("x")
    else:
        names = ["orch-host.service", "orch-runner.service", "orch-records.timer"]
        for n in names:
            (home / ".config" / "systemd" / "user").mkdir(parents=True, exist_ok=True)
            (home / ".config" / "systemd" / "user" / n).write_text("x")
    env = {"HOME": str(home), "ORCH_STATE_DIR": str(state), "PATH": "/usr/bin"}
    m = Machine(platform, home, names)
    runner = FakeRunner()
    layout = Layout(env)
    save(
        layout,
        {
            "v": 1,
            "address": None,
            "domain": None,
            "workspaces": [{"dir": str(tmp_path / "ws1"), "port": 8151}],
            "parts": {"hub": {"url": "http://127.0.0.1:8100", "runner": "launchd"}},
            "dashboard": {"url": "http://127.0.0.1:8151/", "runner": "launchd", "port": 8151, "bind": "127.0.0.1"},
        },
    )
    out, err = io.StringIO(), io.StringIO()
    e = Env(
        env=env, out=out, err=err, platform=platform, uid=501, run=m.run, sleep=lambda s: None,
        answers=lambda p: True, runner_for=lambda kind: runner, dashboard_entry=lambda: None, unload_wait=1.0,
    )  # fmt: skip
    return e, m, runner, out, err


def run_cli(e, argv, cmd, cwd=None):
    e.out.truncate(0), e.out.seek(0)
    return cli.main(argv, env=e.env, out=e.out, err=e.err, cmd=cmd, e=e, cwd=cwd)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_stop_all_then_start_all_keeps_units_and_survives_a_reboot(tmp_path, platform):
    e, m, runner, out, err = machine(tmp_path, platform)
    assert {s.key: s.state for s in discover(e) if s.installed} == {
        "host": "running", "runner": "running", "records": "running", "dashboard": "running", "hub": "running",
    }  # fmt: skip
    assert run_cli(e, ["all"], "stop") == 0, err.getvalue()
    text = out.getvalue()
    assert "Everything is stopped, also after a restart" in text and "Nothing was removed" in text
    assert m.loaded == set() and runner.up == {"hub": False, "dashboard": False}
    kinds = (
        [c[1] for c in m.log if c[0] == "launchctl"]
        if platform == "darwin"
        else [c[2] for c in m.log if c[0] == "systemctl"]
    )
    assert "disable" in kinds  # persistent: a reboot does not load it again
    if platform == "darwin":
        assert m.disabled == {"io.orch.host", "io.orch.runner", "io.orch.records"}
    for unit in (m.home / ("Library/LaunchAgents" if platform == "darwin" else ".config/systemd/user")).iterdir():
        assert unit.exists()  # nothing is removed
    assert stopped_by_orch(e.state) >= {
        "io.orch.host" if platform == "darwin" else "orch-host.service",
        "hub",
        "dashboard",
    }
    rows = {s.key: s.state for s in discover(e) if s.installed}
    assert set(rows.values()) == {"stopped"}
    assert "stopped by orch stop" in text  # the table says why
    # a second stop has nothing left to do
    assert run_cli(e, ["all"], "stop") == 0 and "Nothing to do" in out.getvalue()
    # start all
    assert run_cli(e, ["all"], "start") == 0, err.getvalue()
    assert {s.key: s.state for s in discover(e) if s.installed} == {k: "running" for k in rows}
    assert m.disabled == set() and runner.up == {"hub": True, "dashboard": True}
    assert stopped_by_orch(e.state) == set()


def test_order_of_commands_stops_the_runner_before_the_host(tmp_path):
    e, m, runner, out, err = machine(tmp_path, "darwin")
    run_cli(e, ["all"], "stop")
    boots = [c[-1] for c in m.log if c[:2] == ["launchctl", "bootout"]]
    assert boots == ["gui/501/io.orch.runner", "gui/501/io.orch.host", "gui/501/io.orch.records"]
    assert runner.calls == [("pause", "dashboard"), ("pause", "hub")]


def test_dry_run_changes_nothing(tmp_path):
    e, m, runner, out, err = machine(tmp_path, "darwin")
    m.log.clear()
    assert run_cli(e, ["all", "--dry-run"], "stop") == 0
    assert "would stop" in out.getvalue() and "dry run" in out.getvalue()
    assert not [c for c in m.log if c[1] in ("disable", "bootout")] and runner.calls == []


def test_a_host_that_does_not_come_back_is_reported(tmp_path):
    e, m, runner, out, err = machine(tmp_path, "linux")
    run_cli(e, ["all"], "stop")
    e.answers = lambda p: False
    e.clock = iter(range(0, 10_000, 5)).__next__
    assert run_cli(e, ["all"], "start") == 1
    assert "FAILED" in out.getvalue() and "does not answer" in out.getvalue()


def test_services_table_and_json(tmp_path):
    e, m, runner, out, err = machine(tmp_path, "darwin")
    assert run_cli(e, [], "services") == 0
    text = out.getvalue()
    assert "the host service" in text and "running" in text and "publish" in text and "not installed" in text
    assert "NetBird" not in text
    assert run_cli(e, ["--json"], "services") == 0
    doc = json.loads(out.getvalue())
    assert {r["service"] for r in doc["services"]} == {
        "host",
        "runner",
        "records",
        "hub",
        "relay",
        "publish",
        "dashboard",
    }


def test_platforms_without_units_say_so(tmp_path):
    e, m, runner, out, err = machine(tmp_path, "win32")
    e.answers = lambda p: False
    run_cli(e, [], "services")
    assert "not available on this system" in out.getvalue()
    assert run_cli(e, ["all"], "stop") == 0  # the installed parts still stop
    assert runner.up == {"hub": False, "dashboard": False}


# -------------------------------------------------------------------------------------------- scopes and refusals


def test_stop_outside_a_workspace_explains_and_names_stop_all(tmp_path):
    e, m, runner, out, err = machine(tmp_path, "darwin")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run_cli(e, [], "stop", cwd=str(plain)) == 1
    msg = err.getvalue()
    assert "not an orch workspace" in msg and "orch stop all" in msg and "orch services" in msg
    assert m.loaded  # nothing was stopped


def test_an_agent_is_refused(tmp_path):
    e, *_ = machine(tmp_path, "darwin")
    err = io.StringIO()
    for k in ("ORCH_GRANT", "ORCH_SESSION"):
        assert cli.main(["all"], env={**e.env, k: "x"}, out=io.StringIO(), err=err, cmd="stop", e=e) == 3
    assert "never does it" in err.getvalue()


def test_bad_words_print_usage(tmp_path):
    e, *_ = machine(tmp_path, "darwin")
    for cmd, argv in (("stop", ["now"]), ("start", ["workspace"]), ("services", ["x"]), ("stop", ["--force"])):
        err = io.StringIO()
        assert cli.main(argv, env=e.env, out=io.StringIO(), err=err, cmd=cmd, e=e) == 2
        assert "usage: orch stop" in err.getvalue()


def make_workspace(tmp_path, name, wid):
    from orch import layout

    root = tmp_path / name
    store = root / layout.STORE_DIR
    store.mkdir(parents=True)
    (store / "config.json").write_text(json.dumps({"schema": "orch.workspace/2", "workspace": {"id": f"w_{wid}"}}))
    return root


def test_workspace_scope_in_a_folder_uses_the_workspace_and_counts_the_others(tmp_path, monkeypatch):
    e, m, runner, out, err = machine(tmp_path, "darwin")
    root = tmp_path / "ws1"
    monkeypatch.setattr(cli, "_here", lambda env, cwd: (str(root), WID))
    (e.state / "host").mkdir(parents=True)
    (e.state / "host" / "workspaces.json").write_text(
        json.dumps(
            {
                "workspaces": [
                    {"root": str(root), "workspace_id": WID},
                    {"root": str(tmp_path / "ws2"), "workspace_id": OTHER},
                ]
            }
        )
    )
    assert run_cli(e, [], "stop") == 0, err.getvalue()
    text = out.getvalue()
    assert "no agents running in this workspace" in text and "shared with 1 other workspace on this computer" in text
    assert (
        m.loaded == {"io.orch.host", "io.orch.runner", "io.orch.records"} and runner.up["dashboard"]
    )  # others keep running


def test_workspace_scope_alone_stops_the_person_services(tmp_path, monkeypatch):
    e, m, runner, out, err = machine(tmp_path, "darwin")
    monkeypatch.setattr(cli, "_here", lambda env, cwd: (str(tmp_path / "ws1"), WID))
    assert run_cli(e, [], "stop") == 0, err.getvalue()
    assert m.loaded == set() and runner.up == {"hub": True, "dashboard": False}  # the hub is a server part: untouched
    assert run_cli(e, [], "start") == 0
    assert m.loaded == {"io.orch.host", "io.orch.runner", "io.orch.records"} and runner.up["dashboard"]


def test_discovery_of_a_real_workspace_folder(tmp_path):
    root = make_workspace(tmp_path, "real", WID)
    sub = root / "src"
    sub.mkdir()
    got = cli._here({}, str(sub))
    assert got is not None and got[0] == str(root.resolve()) and got[1] == WID
    assert cli._here({}, str(tmp_path)) is None


# -------------------------------------------------------------------------------------------------------- agents


class FakeProcs:
    def __init__(self, procs):
        self.procs = procs  # pid -> ppid
        self.sent = []

    def info(self, pid):
        if pid not in self.procs:
            return None
        return type("I", (), {"ppid": self.procs[pid], "birth": 7, "zombie": False})()

    def signal_verified(self, pid, birth, sig):
        self.sent.append((pid, sig))
        if sig == signal.SIGTERM:
            self.procs.pop(pid, None)  # the agent ends on SIGTERM
        return True

    def alive(self, pid, birth):
        return pid in self.procs


def write_run(state, wid, run, pid, st="running"):
    d = state / "hosts" / wid / "runs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run}.json").write_text(json.dumps({"run": run, "workspace_id": wid, "pid": pid, "state": st}))


def test_agents_of_one_workspace_end_with_hup_and_term_never_kill(tmp_path):
    e, *_ = machine(tmp_path, "darwin")
    write_run(e.state, WID, "r_1", 500)
    write_run(e.state, WID, "r_2", 501, "exited")
    write_run(e.state, OTHER, "r_3", 502)
    e.procs = FakeProcs({500: 99, 502: 99})
    runs = A.live_runs(e.state, WID)
    assert [r["pid"] for r in runs] == [500]
    ended, notes = A.end_agents(e, runs, rpid=99, grace=1)
    assert (ended, notes) == (1, [])
    assert e.procs.sent == [(500, signal.SIGHUP), (500, signal.SIGTERM)]
    assert 502 in e.procs.procs  # the other workspace's agent is untouched


def test_a_process_that_is_not_the_runners_child_is_never_signalled(tmp_path):
    e, *_ = machine(tmp_path, "darwin")
    write_run(e.state, WID, "r_1", 500)
    e.procs = FakeProcs({500: 1})
    ended, notes = A.end_agents(e, A.live_runs(e.state, WID), rpid=99, grace=1)
    assert ended == 0 and "not a child of the runner" in notes[0] and e.procs.sent == []
    ended, notes = A.end_agents(e, A.live_runs(e.state, WID), rpid=None, grace=1)
    assert e.procs.sent == []


def test_an_agent_that_will_not_end_is_reported_not_killed(tmp_path):
    e, *_ = machine(tmp_path, "darwin")
    write_run(e.state, WID, "r_1", 500)
    procs = FakeProcs({500: 99})
    procs.signal_verified = lambda pid, birth, sig: True  # ignores the signal
    e.procs = procs
    ticks = iter(range(0, 1000))
    e.clock = lambda: next(ticks)
    ended, notes = A.end_agents(e, A.live_runs(e.state, WID), rpid=99, grace=3)
    assert ended == 0 and "did not end within 3 seconds: left running" in notes[0]


def test_stop_workspace_ends_its_agents_through_the_cli(tmp_path, monkeypatch):
    e, m, runner, out, err = machine(tmp_path, "darwin")
    monkeypatch.setattr(cli, "_here", lambda env, cwd: (str(tmp_path / "ws1"), WID))
    (e.state / "host").mkdir(parents=True)
    (e.state / "host" / "workspaces.json").write_text(
        json.dumps({"workspaces": [{"root": str(tmp_path / "ws2"), "workspace_id": OTHER}]})
    )
    write_run(e.state, WID, "r_1", 500)
    e.procs = FakeProcs({500: 4242})
    m.run_orig = m.run

    def run(argv):  # the runner's pid in launchctl print
        rc, text = m.run_orig(argv)
        return (rc, text + "\tpid = 4242\n") if argv[1] == "print" and rc == 0 else (rc, text)

    e.run = run
    assert run_cli(e, [], "stop") == 0, err.getvalue()
    assert "agents of this workspace: 1 of 1 ended" in out.getvalue()


# --------------------------------------------------------------------------------------------- install runners


@pytest.mark.slow  # stop() waits for its own unreaped child
def test_pidfile_runner_pause_and_resume_start_the_same_command(tmp_path):
    layout = Layout({"HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path / "s")})
    r = service.PidfileRunner(layout)
    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    try:
        r.start("hub", argv, {}, tmp_path / "log" / "hub.log")
        assert r.info("hub")["running"]
        r.pause("hub")
        assert not r.info("hub")["running"]
        r.resume("hub")
        assert r.info("hub")["running"]
    finally:
        r.stop("hub")


def test_pidfile_runner_resume_without_a_record_says_what_to_do(tmp_path):
    r = service.PidfileRunner(Layout({"HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path / "s")}))
    with pytest.raises(RuntimeError, match="orch install hub"):
        r.resume("hub")


def test_launchd_runner_pause_disables_and_keeps_the_plist_resume_enables_then_loads(tmp_path):
    calls = []
    layout = Layout({"HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path / "s")})
    r = service.LaunchdRunner(
        layout, run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
    )
    r.plist("hub").parent.mkdir(parents=True)
    r.plist("hub").write_bytes(b"x")
    r.pause("hub")
    assert [c[1] for c in calls] == ["disable", "bootout"] and r.plist("hub").exists()
    calls.clear()
    r.resume("hub")
    assert [c[1] for c in calls] == ["enable", "bootstrap"]


def test_systemd_runner_pause_and_resume(tmp_path):
    calls = []
    layout = Layout({"HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path / "s")})
    r = service.SystemdRunner(
        layout, run=lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
    )
    r.path("hub").parent.mkdir(parents=True)
    r.path("hub").write_text("x")
    r.pause("hub")
    r.resume("hub")
    assert [c[2] for c in calls] == ["disable", "enable"] and r.path("hub").exists()


# ------------------------------------------------------------------------------------------- the host's own start


def test_orch_host_enable_after_orch_stop_enables_the_label_again(tmp_path):
    from orch.host import enable
    from orch.lifecycle.units import _write_marker

    state = tmp_path / "state"
    _write_marker(state, {"io.orch.host"})
    ran = []

    def fake(argv):
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    unit = enable.Plan("launchd", tmp_path / "u.plist", b"x", state, state / "run", tmp_path / "rel", 501)
    enable.Manager(unit, fake).start(changed=False)
    assert ["launchctl", "enable", "gui/501/io.orch.host"] in ran
    assert stopped_by_orch(state) == set()

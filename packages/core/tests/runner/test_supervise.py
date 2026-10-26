"""Supervision (D-P2-29, D-P2-37, D-P2-39): dedupe, stop and cleanup states, sweeps, birth identity, crash sweep.

Every process signalled here was started by the test (directly or as a run); the tests that check "never signalled"
use their own sleepers and kill them in teardown.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from orch.runner import procs
from orch.runner.supervise import SpawnError, Supervisor, peek_exit, watch_exit
from orch.runner.table import RunTable
from tests.runner.helpers import kill_own, py, read_pid, run_id, spec, wait_for

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")


def test_spawn_is_deduped_by_run_id(sup, tmp_path):
    """A lost spawn ack (the host resends the same run id) gives exactly one process."""
    marker = tmp_path / "count"
    s = spec(tmp_path, ["/bin/sh", "-c", f"echo x >> {marker}; sleep 60"])
    first = sup.spawn(s)
    second = sup.spawn(s)
    assert first["duplicate"] is False and second["duplicate"] is True
    assert first["pid"] == second["pid"]
    wait_for(lambda: marker.exists())
    time.sleep(0.3)
    assert marker.read_text() == "x\n"
    assert sup.stop(s.run, grace=1)["cleanup"] == "complete"


def test_spawn_dedupe_survives_a_runner_restart(state, tmp_path):
    marker = tmp_path / "count"
    s = spec(tmp_path, ["/bin/sh", "-c", f"echo x >> {marker}; sleep 60"])
    one = Supervisor(state, cgroups=None, scan_interval=0.1, exit_grace=0.5)
    one.open()
    first = one.spawn(s)
    wait_for(lambda: marker.exists())  # the run has really started before it is stopped
    one.close(stop_runs=True, grace=0.5)
    two = Supervisor(state, cgroups=None, scan_interval=0.1, exit_grace=0.5)
    two.open()
    try:
        again = two.spawn(s)
        assert again["duplicate"] is True and again["pid"] == first["pid"]
        assert marker.read_text() == "x\n"
    finally:
        two.close()


def test_stop_reports_complete_and_kills_an_in_session_double_fork(sup, tmp_path):
    """A grandchild that double-forks but stays in the run's session is found by the session sweep."""
    pidfile = tmp_path / "gc.pid"
    script = f"(sh -c 'echo $$ > {pidfile}; exec sleep 60' &) ; sleep 60"
    s = spec(tmp_path, ["/bin/sh", "-c", script])
    sup.spawn(s)
    gc, birth = read_pid(pidfile)
    assert procs.info(gc).sid == sup.get(s.run)["sid"]
    try:
        res = sup.stop(s.run, grace=1)
        assert res == {"state": "exited", "cleanup": "complete"}
        wait_for(lambda: not procs.alive(gc, birth), timeout=3)
    finally:
        kill_own(gc, birth)
    rec = sup.get(s.run)
    assert rec["exit"]["reason"] == "stopped"


def test_setsid_escapee_gives_incomplete(sup, tmp_path):
    """Tie-break acceptance: on macOS (and on Linux without a delegated cgroup) a ``setsid()`` descendant gives
    ``cleanup: "incomplete"``, never ``complete``."""
    _escapee(sup, tmp_path)


def _escapee(sup, tmp_path):
    pidfile = tmp_path / "esc.pid"
    code = (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "time.sleep(60)\n"
    )
    s = spec(tmp_path, py(code))
    sup.spawn(s)
    esc, birth = read_pid(pidfile)
    try:
        assert procs.info(esc).sid != sup.get(s.run)["sid"]  # it left the session
        sup.scan()
        res = sup.stop(s.run, grace=1)
        assert res["cleanup"] == "incomplete"
        assert sup.table.get(s.run)["escaped"] is True
    finally:
        kill_own(esc, birth)


def test_reused_pid_is_never_signalled(state, sleeper):
    """A recorded run whose pid now belongs to another process (other birth time) is never signalled: not the pid,
    not its group, not its session."""
    other = sleeper()
    info = procs.info(other.pid)
    table = RunTable(state / "runner", os.getuid())
    table.load()
    run = run_id()
    table.put(
        run,
        {
            "workspace_id": "0" * 32,
            "kind": "agent",
            "state": "running",
            "pid": other.pid,
            "birth": info.birth - 1,  # the run's process was born earlier: this pid was reused
            "pgid": other.pid,
            "sid": other.pid,
            "cgroup": None,
        },
    )
    sup = Supervisor(state, cgroups=None, scan_interval=0.1)
    swept = sup.open()
    try:
        assert swept == [{"run": run, "workspace_id": "0" * 32, "swept": 0}]
        time.sleep(0.2)
        assert other.poll() is None and procs.alive(other.pid, info.birth)
        rec = sup.get(run)
        assert rec["state"] == "lost" and rec["cleanup"] == "unknown" and rec["exit"]["reason"] == "lost"
        assert sup.stop(run) == {"state": "lost", "cleanup": "unknown"}
        assert procs.signal_verified(other.pid, info.birth - 1, signal.SIGKILL) is False
        assert other.poll() is None
    finally:
        sup.close()


def test_crash_sweep_stops_recorded_processes_that_match(state, tmp_path):
    """After a runner crash the recorded run (birth identity matches) is swept and reported lost/unknown."""
    one = Supervisor(state, cgroups=None, scan_interval=0.1)
    one.open()
    s = spec(tmp_path, ["/bin/sh", "-c", "exec sleep 60"])  # one process (dash forks a last command, zsh execs it)
    first = one.spawn(s)
    # a crash: no stop, the I/O thread just ends (the process keeps running, as after a runner SIGKILL)
    crash(one)
    pid, birth = first["pid"], first["birth"]
    assert procs.alive(pid, birth)
    two = Supervisor(state, cgroups=None, scan_interval=0.1)
    try:
        swept = two.open()
        assert swept == [{"run": s.run, "workspace_id": "0" * 32, "swept": 1}]
        one.live[s.run].proc.wait(timeout=5)  # reap our own child
        assert not procs.alive(pid, birth)
        assert two.get(s.run)["state"] == "lost"
    finally:
        kill_own(pid, birth)
        two.close()
        one.close()


def crash(sup: Supervisor) -> None:
    """A runner crash, simulated: the I/O thread stops, the lock is released, the runs keep running."""
    sup._closing = True
    sup._wake()
    sup._thread.join()
    os.close(sup._lock_fd)
    sup._lock_fd = None


def test_process_outside_the_run_is_never_signalled(sup, tmp_path, sleeper):
    other = sleeper()
    info = procs.info(other.pid)
    s = spec(tmp_path, ["/bin/sh", "-c", "sleep 60"])
    sup.spawn(s)
    assert sup.stop(s.run, grace=0.5)["cleanup"] == "complete"
    assert other.poll() is None and procs.alive(other.pid, info.birth)


def test_natural_exit_records_code_and_cleans_up(state, tmp_path):
    events = []
    sup = Supervisor(state, cgroups=None, scan_interval=0.1, exit_grace=0.5, on_event=lambda *a: events.append(a))
    sup.open()
    try:
        s = spec(tmp_path, ["/bin/sh", "-c", "echo bye; exit 3"])
        sup.spawn(s)
        # both events: the table's cleanup is written before the cleanup event is emitted
        wait_for(lambda: len([e for e in events if e[1] == s.run]) >= 2)
        rec = sup.get(s.run)
        assert rec["state"] == "exited" and rec["exit"]["code"] == 3 and rec["exit"]["reason"] == "exited"
        assert rec["cleanup"] == "complete"
        assert b"bye" in sup.ring(s.run).read()[2]
        kinds = [e[0] for e in events if e[1] == s.run]
        assert kinds == ["exit", "cleanup"]
    finally:
        sup.close()


def test_leader_exit_stops_leftover_session_members(sup, tmp_path):
    pidfile = tmp_path / "bg.pid"
    s = spec(tmp_path, ["/bin/sh", "-c", f"sh -c 'echo $$ > {pidfile}; exec sleep 60' & sleep 0.3; exit 0"])
    sup.spawn(s)
    bg, birth = read_pid(pidfile)
    try:
        wait_for(lambda: sup.get(s.run)["cleanup"] is not None, timeout=10)
        assert sup.get(s.run)["cleanup"] == "complete"
        assert not procs.alive(bg, birth)
    finally:
        kill_own(bg, birth)


def test_failed_spawn_is_recorded_and_never_retried(sup, tmp_path):
    s = spec(tmp_path, [str(tmp_path / "missing")])
    with pytest.raises(SpawnError):
        sup.spawn(s)
    again = sup.spawn(s)
    assert again["duplicate"] is True and again["state"] == "exited"


def test_ring_never_blocks_a_writer_without_viewers(sup, tmp_path):
    """50 MiB of output with nobody attached: the run finishes; the ring keeps the last 1 MiB."""
    total = 50 * 1024 * 1024
    code = f"import os\nb = b'x' * 65536\nfor _ in range({total // 65536}):\n    os.write(1, b)\n"
    s = spec(tmp_path, py(code))
    sup.spawn(s)
    wait_for(lambda: sup.get(s.run)["state"] == "exited", timeout=60)
    ring = sup.ring(s.run)
    wait_for(lambda: ring.end >= total, timeout=10)
    start, _, data = ring.read()
    assert ring.end == total and start == total - len(data) and len(data) == ring.size


def test_table_is_private(sup, state, tmp_path):
    s = spec(tmp_path, ["/bin/sh", "-c", "exit 0"])
    sup.spawn(s)
    assert os.stat(state / "runner").st_mode & 0o777 == 0o700
    assert os.stat(state / "runner" / "runs.json").st_mode & 0o777 == 0o600


def test_ended_rings_are_dropped_after_their_time(sup, tmp_path, monkeypatch):
    from orch.runner import supervise

    s = spec(tmp_path, ["/bin/sh", "-c", "exit 0"])
    sup.spawn(s)
    wait_for(lambda: sup.get(s.run)["cleanup"] is not None)
    wait_for(lambda: sup.live[s.run].pty_closed)
    monkeypatch.setattr(supervise, "KEEP_RING_SECONDS", 0.0)
    wait_for(lambda: s.run not in sup.live)
    assert sup.get(s.run)["state"] == "exited" and sup.ring(s.run) is None  # the record stays, the ring goes


# -- security scan follow-up: no fail-open on timeouts, no unsupervised process ---------------------------------------


def test_hanging_trampoline_is_killed_and_stays_recorded(state, tmp_path, monkeypatch):
    """A spawn that times out after the fork: the process was recorded (pid, birth) before anything could fail, is
    killed, and the run is kept in the table as a failed spawn, never dropped."""
    from orch.runner import supervise

    hang = tmp_path / "hang.sh"
    hang.write_text("#!/bin/sh\nexec sleep 60\n")
    hang.chmod(0o755)
    monkeypatch.setattr(supervise, "SPAWN_TIMEOUT", 0.3)
    sup = Supervisor(state, cgroups=None, scan_interval=0.1, python=str(hang))
    sup.open()
    try:
        s = spec(tmp_path, ["/bin/sh", "-c", "exit 0"])
        with pytest.raises(SpawnError) as err:
            sup.spawn(s)
        assert err.value.code == "run.spawn_failed" and "timeout" in err.value.message
        rec = sup.table.get(s.run)
        assert isinstance(rec["pid"], int) and isinstance(rec["birth"], int)
        assert not procs.alive(rec["pid"], rec["birth"])
        assert rec["state"] == "exited" and rec["exit"]["reason"] == "spawn_failed" and rec["cleanup"] == "complete"
    finally:
        sup.close()


def test_leader_ignoring_term_is_killed(sup, tmp_path):
    ready = tmp_path / "ready"
    s = spec(tmp_path, ["/bin/sh", "-c", f"trap '' TERM HUP; touch {ready}; exec sleep 60"])
    res = sup.spawn(s)
    wait_for(ready.exists)
    assert sup.stop(s.run, grace=0.3) == {"state": "exited", "cleanup": "complete"}
    assert not procs.alive(res["pid"], res["birth"])


def test_a_survivor_keeps_the_run_running_incomplete_and_supervised(sup, tmp_path, monkeypatch):
    """If even SIGKILL does not end the leader, the stop answers ``incomplete``, the run stays ``running`` in the
    table and the runner keeps killing it; it is never reported ended or dropped."""
    from orch.runner import supervise

    real = procs.signal_verified

    def no_kill(pid, birth, sig):
        return False if sig == signal.SIGKILL else real(pid, birth, sig)

    ready = tmp_path / "ready"
    s = spec(tmp_path, ["/bin/sh", "-c", f"trap '' TERM HUP; touch {ready}; exec sleep 60"])
    res = sup.spawn(s)
    wait_for(ready.exists)
    monkeypatch.setattr(supervise.procs, "signal_verified", no_kill)
    monkeypatch.setattr(supervise, "KILL_WAIT", 0.3)
    out = sup.stop(s.run, grace=0.2)
    assert out == {"state": "running", "cleanup": "incomplete"}
    assert procs.alive(res["pid"], res["birth"])
    assert sup.live[s.run].lingering and s.run in sup.live
    monkeypatch.setattr(supervise.procs, "signal_verified", real)  # the retry loop now gets through
    wait_for(lambda: sup.get(s.run)["cleanup"] == "complete", timeout=10)
    assert sup.get(s.run)["state"] == "exited" and not procs.alive(res["pid"], res["birth"])


def test_crash_sweep_also_stops_leftovers_of_ended_runs(state, sleeper):
    """An ended run whose cleanup was incomplete still has its process recorded: a restarted runner sweeps it."""
    other = sleeper()  # stands in for a leftover of the run (its own session, recorded by birth identity)
    info = procs.info(other.pid)
    table = RunTable(state / "runner", os.getuid())
    table.load()
    run = run_id()
    rec = {"workspace_id": "0" * 32, "kind": "agent", "state": "exited", "cleanup": "incomplete", "cgroup": None}
    table.put(run, {**rec, "pid": other.pid, "birth": info.birth, "pgid": other.pid, "sid": other.pid})
    sup = Supervisor(state, cgroups=None, scan_interval=0.1)
    try:
        swept = sup.open()
        assert swept == [{"run": run, "workspace_id": "0" * 32, "swept": 1}]
        other.wait(timeout=5)
        assert sup.get(run)["cleanup"] == "unknown" and sup.get(run)["state"] == "exited"
    finally:
        sup.close()


# -- Opus review of #428 ------------------------------------------------------------------------------------------


def _daemon(tmp_path):
    """A classic double-forked daemon started by the test: setsid in the middle process, which then exits.
    Returns (middle pid, middle birth, daemon pid, daemon birth)."""
    import subprocess

    info = tmp_path / "daemon.txt"
    code = (
        "import os, sys, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    mid = os.getpid()\n"
        "    if os.fork() == 0:\n"
        f"        open({str(info)!r}, 'w').write(f'{{mid}} {{os.getpid()}}')\n"
        "        time.sleep(60)\n"
        "        os._exit(0)\n"
        "    time.sleep(0.3)\n"
        "    os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    text = wait_for(lambda: info.exists() and info.read_text())
    mid, dpid = (int(x) for x in text.split())
    dinfo = wait_for(lambda: procs.info(dpid))
    wait_for(lambda: (info := procs.info(mid)) is None or info.zombie, timeout=5)  # one read: it may vanish between two
    return mid, dinfo.birth - 1, dpid, dinfo.birth


def test_old_record_never_matches_a_leaderless_session(state, tmp_path):
    """Opus repro: an ended ``incomplete`` record whose sid equals a double-forked daemon's exited middle process. The
    crash sweep matches exact identities only, so the daemon survives."""
    mid, mbirth, dpid, dbirth = _daemon(tmp_path)
    try:
        table = RunTable(state / "runner", os.getuid())
        table.load()
        run = run_id()
        table.put(run, {"workspace_id": "0" * 32, "kind": "agent", "state": "exited", "cleanup": "incomplete",
                        "pid": mid, "birth": mbirth, "pgid": mid, "sid": mid, "cgroup": None,
                        "boot": procs.boot_id()})  # fmt: skip
        sup = Supervisor(state, cgroups=None, scan_interval=0.1)
        try:
            assert sup.open() == []
            time.sleep(0.2)
            assert procs.alive(dpid, dbirth)
        finally:
            sup.close()
    finally:
        kill_own(dpid, dbirth)


def test_records_of_another_boot_are_never_matched(state, sleeper):
    other = sleeper()
    info = procs.info(other.pid)
    table = RunTable(state / "runner", os.getuid())
    table.load()
    run = run_id()
    table.put(run, {"workspace_id": "0" * 32, "kind": "agent", "state": "running", "pid": other.pid,
                    "birth": info.birth, "pgid": other.pid, "sid": other.pid, "cgroup": None,
                    "boot": "another-boot"})  # fmt: skip
    sup = Supervisor(state, cgroups=None, scan_interval=0.1)
    try:
        assert sup.open() == [{"run": run, "workspace_id": "0" * 32, "swept": 0}]
        assert other.poll() is None
    finally:
        sup.close()


def test_a_member_of_another_uid_is_never_signalled_and_makes_cleanup_incomplete(state, sleeper, monkeypatch):
    from orch.runner import supervise

    other = sleeper()
    real = procs.info(other.pid)
    fake = procs.ProcInfo(real.pid, real.ppid, real.pgid, real.sid, os.getuid() + 1, real.birth)
    monkeypatch.setattr(supervise.procs, "snapshot", lambda uid=None: {other.pid: fake})
    sent = []
    monkeypatch.setattr(supervise.procs, "signal_verified", lambda p, b, s: sent.append(p) or True)
    monkeypatch.setattr(supervise, "KILL_WAIT", 0.2)
    sup = Supervisor(state, cgroups=None, scan_interval=60)
    rec = {"run": run_id(), "pid": other.pid, "birth": real.birth, "pgid": other.pid, "sid": other.pid}
    assert sup._terminate(rec, {}, 0.1) == "incomplete"
    assert sent == [] and other.poll() is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc")
def test_linux_uid_comes_from_status():
    assert procs._linux_uid(os.getpid()) == os.getuid()


def test_signal_permission_error_is_not_sent(sleeper, monkeypatch):
    other = sleeper()
    info = procs.info(other.pid)

    def denied(*a):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(procs.os, "kill", denied)
    if hasattr(signal, "pidfd_send_signal"):
        monkeypatch.setattr(procs.signal, "pidfd_send_signal", denied)
    assert procs.signal_verified(other.pid, info.birth, signal.SIGTERM) is False


def test_a_failing_cleanup_never_dies_silently(sup, tmp_path, monkeypatch):
    s = spec(tmp_path, ["/bin/sh", "-c", "exec sleep 60"])
    res = sup.spawn(s)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sup, "_terminate", boom)
    out = sup.stop(s.run, grace=0.1)
    assert out["cleanup"] == "incomplete"
    assert sup.live[s.run].lingering  # still supervised: the retry loop keeps killing it
    monkeypatch.undo()
    wait_for(lambda: not procs.alive(res["pid"], res["birth"]), timeout=10)


def test_runner_env_deny_list(tmp_path):
    cases = [({k: "x"}, "agent") for k in ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_ENV", "PYTHONPATH", "GIT_DIR")]
    cases += [({"ENV": "x"}, "agent"), ({"ORCH_GRANT": "g"}, "shell"), ({"ORCH_SESSION": "s"}, "shell")]
    for env, kind in cases:
        with pytest.raises(SpawnError) as err:
            spec(tmp_path, ["/bin/sh"], env=env, kind=kind).check()
        assert err.value.code == "invalid.input"
    spec(tmp_path, ["/bin/sh"], env={"ORCH_GRANT": "g", "PATH": "/bin"}, kind="agent").check()


def test_cgroup_path_must_be_the_runs_own(tmp_path):
    from orch.runner.cgroup import CgroupTree

    sup = Supervisor(tmp_path / "s", cgroups=CgroupTree(tmp_path / "base"))
    run = run_id()
    assert sup._cgroup_of({"run": run, "cgroup": str(tmp_path / "base" / f"run-{run}")}) is not None
    assert sup._cgroup_of({"run": run, "cgroup": "/sys/fs/cgroup/user.slice"}) is None
    assert sup._cgroup_of({"run": run, "cgroup": str(tmp_path / "base" / f"run-{run_id()}")}) is None


def test_spawn_of_a_run_id_older_than_retention_is_refused(sup, tmp_path):
    with pytest.raises(SpawnError) as err:
        sup.spawn(spec(tmp_path, ["/bin/sh", "-c", "exit 0"], run="r_0" + "1" * 25))
    assert err.value.code == "invalid.input" and "older" in err.value.message


# -- Sonnet review of #428 ----------------------------------------------------------------------------------------


def _open_fds() -> set[int]:
    return {int(n) for n in os.listdir("/dev/fd" if sys.platform == "darwin" else "/proc/self/fd")}


def test_a_spawn_that_never_forks_leaks_nothing_and_ran_nothing(sup, tmp_path, monkeypatch):
    from orch.runner import supervise

    def no_fork(*a, **k):
        raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(supervise.subprocess, "Popen", no_fork)
    before = _open_fds()
    s = spec(tmp_path, ["/bin/sh", "-c", "exit 0"])
    with pytest.raises(SpawnError) as err:
        sup.spawn(s)
    assert err.value.code == "run.spawn_failed" and err.value.cleanup == "complete"
    assert _open_fds() <= before  # pty and both pipes closed
    rec = sup.table.get(s.run)
    assert rec["cleanup"] == "complete" and rec["exit"]["reason"] == "spawn_failed"


def test_a_table_failure_after_the_fork_kills_the_child(sup, tmp_path, monkeypatch):
    real = sup.table.update
    calls = []

    def failing(run, **fields):
        calls.append(fields)
        if "pid" in fields and len(calls) == 1:
            raise OSError(28, "No space left on device")
        return real(run, **fields)

    monkeypatch.setattr(sup.table, "update", failing)
    marker = tmp_path / "ran"
    with pytest.raises(SpawnError) as err:
        sup.spawn(spec(tmp_path, ["/bin/sh", "-c", f"touch {marker}; exec sleep 60"]))
    assert err.value.code == "run.spawn_failed" and err.value.cleanup in ("complete", "incomplete")
    time.sleep(0.3)
    assert not marker.exists()  # the trampoline was stopped before it ran the program


def test_the_io_thread_survives_an_unexpected_error(sup, tmp_path, monkeypatch):
    real = sup._read
    hit = []

    def broken(live):
        if not hit:
            hit.append(live.run)
            raise RuntimeError("boom")
        return real(live)

    monkeypatch.setattr(sup, "_read", broken)
    s = spec(tmp_path, ["/bin/sh", "-c", "echo hello; exit 2"])
    sup.spawn(s)
    wait_for(lambda: sup.get(s.run)["exit"] is not None, timeout=10)
    assert hit and sup._thread.is_alive()
    assert sup.get(s.run)["exit"]["code"] == 2


def test_a_second_runner_on_the_same_state_refuses_and_sweeps_nothing(sup, state, tmp_path):
    from orch.runner.supervise import RunnerBusy

    s = spec(tmp_path, ["/bin/sh", "-c", "exec sleep 60"])
    res = sup.spawn(s)
    other = Supervisor(state, cgroups=None)
    with pytest.raises(RunnerBusy):
        other.open()
    assert procs.alive(res["pid"], res["birth"]) and sup.get(s.run)["state"] == "running"


def test_runs_start_with_the_captured_umask(state, tmp_path):
    from orch.host.custody import ensure_private_dir, write_private

    ensure_private_dir(state / "runner", os.getuid())
    write_private(state / "runner" / "settings.json", {"v": 1, "umask": 0o027})
    sup = Supervisor(state, cgroups=None, scan_interval=0.1)
    sup.open()
    try:
        out = tmp_path / "umask.txt"
        sup.spawn(spec(tmp_path, ["/bin/sh", "-c", f"umask > {out}"]))
        assert wait_for(lambda: out.exists() and out.read_text().strip()) in ("0027", "027")
    finally:
        sup.close()


# -- Opus delta on #428 -------------------------------------------------------------------------------------------


def test_controlled_stop_never_matches_a_reused_session(state, tmp_path):
    """D1, Opus's probe: an escaped, incomplete run whose leader was reaped; its sid/pgid now name a double-forked
    daemon's exited middle (the state after PID reuse). ``close(stop_runs=True)`` uses exact identities only, so the
    daemon survives."""
    sup = Supervisor(state, cgroups=None, scan_interval=0.1, exit_grace=0.5)
    sup.open()
    mid, _mbirth, dpid, dbirth = _daemon(tmp_path)
    try:
        s = spec(tmp_path, ["/bin/sh", "-c", "exec sleep 60"])
        sup.spawn(s)
        assert sup.stop(s.run, grace=0.5)["cleanup"] == "complete"
        live = sup.live[s.run]
        assert live.reaped
        live.cleanup, live.escaped, live.lingering = "incomplete", True, True  # as an escaped run
        live.sid = live.pgid = mid  # the run's session id now names the daemon's (exited) middle process
        sup.close(stop_runs=True, grace=0.3)
        time.sleep(0.2)
        assert procs.alive(dpid, dbirth)
    finally:
        kill_own(dpid, dbirth)


def test_session_sweep_still_works_before_the_leader_is_reaped(sup, tmp_path):
    """The other side of D1: while the leader is unreaped (seen with waitid WNOWAIT), the session sweep still finds
    a member that stayed in the session after the leader exited."""
    pidfile = tmp_path / "bg.pid"
    s = spec(tmp_path, ["/bin/sh", "-c", f"sh -c 'echo $$ > {pidfile}; exec sleep 60' & sleep 0.3; exit 0"])
    sup.spawn(s)
    bg, birth = read_pid(pidfile)
    try:
        wait_for(lambda: sup.get(s.run)["cleanup"] is not None, timeout=10)
        assert sup.get(s.run)["cleanup"] == "complete" and not procs.alive(bg, birth)
    finally:
        kill_own(bg, birth)


def test_enumeration_sees_processes_of_other_uids():
    """D2, unpatched: the all-UID snapshot really contains processes of another UID (root's launchd or init)."""
    snap = procs.snapshot(None)
    assert any(p.uid != os.getuid() for p in snap.values())
    assert procs.info(1) is not None and procs.info(1).uid == 0
    me = procs.info(os.getpid())
    assert me is not None and me.uid == os.getuid() and me.ppid == os.getppid()


def test_boot_id_is_stable():
    assert procs.boot_id() == procs.boot_id() and len(procs.boot_id()) >= 8


def test_env_deny_list_is_anchored(tmp_path):
    for name in ("ENVIRONMENT", "IFS2", "PS4X", "MY_LD_PATH"):
        spec(tmp_path, ["/bin/sh"], env={name: "x"}, kind="agent").check()  # exact names do not deny prefixes
    for name in ("ENV", "IFS", "PS4", "LD_X", "PYTHONX"):
        with pytest.raises(SpawnError):
            spec(tmp_path, ["/bin/sh"], env={name: "x"}, kind="agent").check()


@pytest.mark.parametrize(
    "code,want", [("raise SystemExit(3)", 3), ("import os, signal; os.kill(os.getpid(), 15)", -15)]
)
def test_peek_exit_sees_the_status_without_reaping(code, want):
    """waitid(WNOWAIT) on Linux, the kqueue watch on macOS (no os.waitid there): the leader stays a zombie, so its PID
    and group id are not reused, and the status is seen again on the next peek."""
    proc = subprocess.Popen([sys.executable, "-c", code])
    watch_exit(proc)
    try:
        assert wait_for(lambda: peek_exit(proc) is not None, 10)
        assert peek_exit(proc) == want and proc.returncode is None
        assert procs.info(proc.pid) is not None  # not reaped
    finally:
        assert proc.wait(timeout=10) == want
    assert peek_exit(proc) == want


def test_peek_exit_of_a_live_process_is_none():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    watch_exit(proc)
    try:
        assert peek_exit(proc) is None
    finally:
        proc.kill()
        proc.wait()

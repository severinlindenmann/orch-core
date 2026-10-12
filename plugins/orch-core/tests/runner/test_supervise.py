"""Supervision (D-P2-29, D-P2-37, D-P2-39): dedupe, stop and cleanup states, sweeps, birth identity, crash sweep.

Every process signalled here was started by the test (directly or as a run); the tests that check "never signalled"
use their own sleepers and kill them in teardown.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from orch.runner import procs
from orch.runner.supervise import SpawnError, Supervisor
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
    one._closing = True
    one._wake()
    one._thread.join()
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
        wait_for(lambda: sup.get(s.run)["cleanup"] is not None)
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
    s = spec(tmp_path, ["/bin/sh", "-c", "trap '' TERM HUP; exec sleep 60"])
    res = sup.spawn(s)
    time.sleep(0.2)
    assert sup.stop(s.run, grace=0.3) == {"state": "exited", "cleanup": "complete"}
    assert not procs.alive(res["pid"], res["birth"])


def test_a_survivor_keeps_the_run_running_incomplete_and_supervised(sup, tmp_path, monkeypatch):
    """If even SIGKILL does not end the leader, the stop answers ``incomplete``, the run stays ``running`` in the
    table and the runner keeps killing it; it is never reported ended or dropped."""
    from orch.runner import supervise

    real = procs.signal_verified

    def no_kill(pid, birth, sig):
        return False if sig == signal.SIGKILL else real(pid, birth, sig)

    s = spec(tmp_path, ["/bin/sh", "-c", "trap '' TERM HUP; exec sleep 60"])
    res = sup.spawn(s)
    time.sleep(0.2)
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

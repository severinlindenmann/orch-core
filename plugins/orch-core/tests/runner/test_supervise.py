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

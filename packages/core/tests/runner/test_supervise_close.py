"""Supervisor.close (#522 candidate): the wake pipe is closed exactly once, nothing writes to or reads from its fd
numbers after the close (they may name someone else's file by then), and fds the I/O thread may still use are never
closed under it."""

from __future__ import annotations

import errno
import logging
import os
import sys
import threading
import time

import pytest

from orch.runner import supervise
from orch.runner.supervise import RunnerBusy, SpawnError, Supervisor, peek_exit
from tests.runner.helpers import kill_own, spec, wait_for

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only")


def _spy(monkeypatch, name: str) -> list[int]:
    real = getattr(os, name)
    seen: list[int] = []

    def spy(fd, *args):
        seen.append(fd)
        return real(fd, *args)

    monkeypatch.setattr(os, name, spy)
    return seen


def _opened(state) -> Supervisor:
    sup = Supervisor(state, cgroups=None, scan_interval=0.1, exit_grace=0.5)
    sup.open()
    return sup


def test_close_twice_closes_each_wake_fd_exactly_once(state, monkeypatch):
    sup = _opened(state)
    wake = (sup._wake_r, sup._wake_w)
    closed = _spy(monkeypatch, "close")
    sup.close()
    sup.close()
    assert [closed.count(fd) for fd in wake] == [1, 1]


def test_wake_after_close_writes_nothing(state, monkeypatch):
    sup = _opened(state)
    old = sup._wake_w
    sup.close()
    written = _spy(monkeypatch, "write")
    sup._wake()
    assert old not in written and written == []


def test_spawn_after_close_is_refused_before_anything_starts(state, tmp_path, monkeypatch):
    sup = _opened(state)
    sup.close()
    written = _spy(monkeypatch, "write")
    s = spec(tmp_path, ["/bin/sh", "-c", "exit 0"])
    with pytest.raises(SpawnError) as err:
        sup.spawn(s)
    assert err.value.cleanup == "complete"
    assert sup.table.get(s.run) is None and s.run not in sup.live
    assert written == []


def test_leader_exit_after_close_writes_to_no_recycled_fd(state, tmp_path, monkeypatch):
    sup = _opened(state)
    s = spec(tmp_path, ["/bin/sh", "-c", "sleep 0.3"])
    sup.spawn(s)
    live = sup.live[s.run]
    old = sup._wake_w
    sup._closing = True  # stop the I/O thread first, so the leader's exit is not seen by it
    sup._wake()
    sup._thread.join(5)
    sup.close()
    wait_for(lambda: peek_exit(live.proc) is not None)
    written = _spy(monkeypatch, "write")
    sup._leader_exit(live)  # what a late clean-up thread does
    assert old not in written
    assert live.exit is not None
    live.proc.wait()


def test_a_stuck_io_thread_releases_its_fds_and_the_lock_itself_once_it_ends(state, monkeypatch, caplog):
    """close() never closes fds under a running I/O thread; the thread releases them (and runner.lock, last) on exit."""
    monkeypatch.setattr(supervise, "JOIN_SECONDS", 0.2)
    sup = _opened(state)
    wake, lock_fd = (sup._wake_r, sup._wake_w), sup._lock_fd
    entered, go = threading.Event(), threading.Event()
    real_step = sup._step

    def stuck(next_scan):
        entered.set()
        go.wait(10)
        return real_step(next_scan)

    sup._step = stuck  # type: ignore[method-assign]
    assert entered.wait(5)
    closed = _spy(monkeypatch, "close")
    with caplog.at_level(logging.ERROR, logger="orch.runner.supervise"):
        sup.close()
    assert sup._thread.is_alive()
    assert not {*wake, lock_fd} & set(closed)
    assert all(os.fstat(fd) for fd in (*wake, lock_fd))  # still ours, still open
    assert any("I/O thread" in r.getMessage() for r in caplog.records)
    probe = Supervisor(state, cgroups=None)
    try:
        with pytest.raises(RunnerBusy):  # no other runner starts while the old thread still runs
            probe.open()
    finally:
        probe.close()
    go.set()
    sup._thread.join(5)
    assert not sup._thread.is_alive()
    assert [closed.count(fd) for fd in (*wake, lock_fd)] == [1, 1, 1]
    assert closed.index(lock_fd) > max(closed.index(fd) for fd in wake)  # the lock is released last
    sup.close()  # nothing left to release
    assert [closed.count(fd) for fd in (*wake, lock_fd)] == [1, 1, 1]
    nxt = Supervisor(state, cgroups=None)
    nxt.open()
    nxt.close()


def test_a_spawn_racing_close_is_stopped_with_the_others_never_orphaned(state, tmp_path):
    """close(stop_runs=True) fences spawns before it takes its stop snapshot: one in progress finishes first and is
    stopped with the rest."""
    sup = _opened(state)
    entered, go = threading.Event(), threading.Event()
    real_start = sup._start

    def slow_start(s):
        entered.set()
        go.wait(10)
        return real_start(s)

    sup._start = slow_start  # type: ignore[method-assign]
    s = spec(tmp_path, ["/bin/sh", "-c", "exec sleep 60"])
    spawned: list = []
    spawner = threading.Thread(target=lambda: spawned.append(sup.spawn(s)))
    spawner.start()
    assert entered.wait(5)
    closer = threading.Thread(target=lambda: sup.close(stop_runs=True, grace=0.5))
    closer.start()
    time.sleep(0.2)
    assert closer.is_alive()  # waits for the spawn in progress
    go.set()
    spawner.join(10)
    closer.join(20)
    assert not closer.is_alive()
    try:
        assert spawned and spawned[0]["duplicate"] is False
        live = sup.live[s.run]
        assert peek_exit(live.proc) is not None  # stopped, not left running behind a closed PTY
        rec = sup.table.get(s.run)
        assert rec["state"] == "exited" and rec["cleanup"] == "complete"
    finally:
        if spawned:
            kill_own(spawned[0]["pid"], spawned[0]["birth"])


def test_a_run_pruned_before_its_stop_does_not_leave_close_half_done(state, tmp_path, monkeypatch, caplog):
    """A run in the stop snapshot that is gone by the time it is stopped (not_found) is logged; the other runs are
    still stopped and close() still ends the I/O thread and releases every fd and runner.lock."""
    sup = _opened(state)
    first, second = (spec(tmp_path, ["/bin/sh", "-c", "exec sleep 60"]) for _ in range(2))
    spawned = [sup.spawn(first), sup.spawn(second)]
    real_stop = sup.stop

    def stop(run, *, grace):
        if run == first.run:
            raise SpawnError("not_found", "no such run")  # pruned between the snapshot and the stop
        return real_stop(run, grace=grace)

    sup.stop = stop  # type: ignore[method-assign]
    wake = (sup._wake_r, sup._wake_w)
    closed = _spy(monkeypatch, "close")
    try:
        with caplog.at_level(logging.ERROR, logger="orch.runner.supervise"):
            sup.close(stop_runs=True, grace=0.5)
        assert any(first.run in r.getMessage() for r in caplog.records)
        assert peek_exit(sup.live[second.run].proc) is not None
        assert sup._closing and not sup._thread.is_alive()
        assert [closed.count(fd) for fd in wake] == [1, 1]
        nxt = Supervisor(state, cgroups=None)
        nxt.open()  # runner.lock was released
        nxt.close()
    finally:
        for rec in spawned:
            kill_own(rec["pid"], rec["birth"])


def test_a_failing_selector_close_still_releases_the_wake_pipe_and_the_lock(state, monkeypatch):
    sup = _opened(state)
    wake, lock_fd = (sup._wake_r, sup._wake_w), sup._lock_fd

    def broken():
        raise OSError(errno.EBADF, "injected")

    monkeypatch.setattr(sup._sel, "close", broken)
    closed = _spy(monkeypatch, "close")
    sup.close()
    assert [closed.count(fd) for fd in (*wake, lock_fd)] == [1, 1, 1]


def test_a_failing_lock_close_is_logged_not_raised(state, monkeypatch, caplog):
    sup = _opened(state)
    lock_fd = sup._lock_fd
    real = os.close

    def close(fd):
        if fd == lock_fd:
            raise OSError(errno.EBADF, "injected")
        return real(fd)

    monkeypatch.setattr(os, "close", close)
    with caplog.at_level(logging.ERROR, logger="orch.runner.supervise"):
        sup.close()
    assert any("runner.lock" in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    os.close(lock_fd)  # the injected failure left it open

"""Supervisor.close (#522 candidate): the wake pipe is closed exactly once, nothing writes to or reads from its fd
numbers after the close (they may name someone else's file by then), and fds the I/O thread may still use are never
closed under it."""

from __future__ import annotations

import logging
import os
import sys

import pytest

from orch.runner.supervise import SpawnError, Supervisor, peek_exit
from tests.runner.helpers import spec, wait_for

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


class _StuckThread:
    def join(self, timeout=None):
        pass

    def is_alive(self):
        return True


def test_close_with_a_stuck_io_thread_leaves_its_fds_open(state, monkeypatch, caplog):
    sup = _opened(state)
    real = sup._thread
    wake = (sup._wake_r, sup._wake_w)
    sup._thread = _StuckThread()  # type: ignore[assignment]
    closed = _spy(monkeypatch, "close")
    with caplog.at_level(logging.ERROR, logger="orch.runner.supervise"):
        sup.close()
    assert not set(wake) & set(closed)
    assert all(os.fstat(fd) for fd in wake)  # still ours, still open
    assert any("I/O thread" in r.getMessage() for r in caplog.records)
    # once the thread has really ended, a later close releases them, once
    real.join(5)
    sup._thread = real
    sup.close()
    assert [closed.count(fd) for fd in wake] == [1, 1]

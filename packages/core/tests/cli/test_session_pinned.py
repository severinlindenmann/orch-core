"""Session records (``.state/sessions``) through the store's pinned root (#564): a planted ``.lock`` symlink, a FIFO
session file and a symlinked ``sessions/`` are refused, never followed, never block."""

from __future__ import annotations

import os
import signal

import pytest

from orch.cli.session import FileRecords
from orch.ops.errors import OrchError
from orch.store.rootfs import RootFS
from tests.store.outside import snapshot, watch

SESSION = "s_01J9ZP0000000000000000000S"


@pytest.fixture
def deadline():
    def boom(*_):
        raise TimeoutError("blocked")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(20)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / ".state" / "sessions").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("precious")
    fs = RootFS.at(root)
    yield fs, root / ".state" / "sessions", outside
    fs.close()


def test_records_work_through_the_pinned_root(ws):
    fs, d, _ = ws
    rec = FileRecords(d, fs=fs)
    rec.remember(SESSION, "k", 1000.0, {"x": 1})
    assert FileRecords(d, fs=fs).recall(SESSION, "k", 1000.0) == {"x": 1}


def test_a_planted_lock_symlink_is_never_followed(ws, deadline):
    fs, d, outside = ws
    os.symlink(outside / "created-by-lock", d / ".lock")
    before = snapshot(outside)
    with watch(outside) as seen, pytest.raises((OrchError, OSError)):
        FileRecords(d, fs=fs).remember(SESSION, "k", 1000.0, {"x": 1})
    assert seen == [] and snapshot(outside) == before


def test_a_fifo_session_file_fails_closed_without_blocking(ws, deadline):
    fs, d, _ = ws
    os.mkfifo(d / f"{SESSION}.json")
    with pytest.raises(OrchError) as e:
        FileRecords(d, fs=fs).recall(SESSION, "k", 1000.0)
    assert e.value.code == "internal"


def test_a_symlinked_sessions_dir_is_never_written(ws, tmp_path, deadline):
    fs, d, outside = ws
    d.rename(tmp_path / "sessions-moved")
    os.symlink(outside, d)
    before = snapshot(outside)
    with watch(outside) as seen, pytest.raises((OrchError, OSError)):
        FileRecords(d, fs=fs).remember(SESSION, "k", 1000.0, {"x": 1})
    assert seen == [] and snapshot(outside) == before


def test_the_host_and_the_store_hooks_hand_the_store_root(tmp_path):
    from types import SimpleNamespace

    from orch.cli.store_hooks import records_for

    root = tmp_path / "ws"
    (root / ".state").mkdir(parents=True)
    with RootFS.at(root) as fs:
        rec = records_for(SimpleNamespace(state_dir=root / ".state", fs=fs))
        assert rec._fs() is fs

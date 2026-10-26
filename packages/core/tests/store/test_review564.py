"""#564 review round: the lock follows the pinned ``.state`` (Codex P1/P2), ``safe_join`` is lexical (Codex P2), the
index has no SQLite file path at all (Opus/Codex), a FIFO at ``.state/lock`` is ``validation.path`` (Opus) and a
closed store releases its cached directory fds."""

from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from orch.store import StoreError
from orch.store.lock import FileLock, LockTimeout
from tests.store import workers
from tests.store.outside import snapshot, swap, watch


def _hold(lock: Path, tmp: Path, seconds: float = 30.0):
    """Another process holding the flock of ``lock`` (the file at that path now)."""
    marker = tmp / f"held-{time.monotonic_ns()}"
    p = mp.get_context("spawn").Process(target=_hold_path, args=(str(lock), str(marker), seconds))
    p.start()
    deadline = time.monotonic() + 30
    while not marker.exists():
        assert time.monotonic() < deadline and p.is_alive(), "the holder never took the lock"
        time.sleep(0.01)
    return p


def _hold_path(path: str, marker: str, seconds: float) -> None:
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    Path(marker).write_text("held")
    time.sleep(seconds)


def _stop(p) -> None:
    p.terminate()
    p.join(10)


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "outside"
    d.mkdir()
    return d


# -- 1. the lock is taken in the .state the store will use (Codex P1)


def test_a_state_dir_replaced_between_two_lock_takes_never_gives_two_holders(env, tmp_path):
    s = env.bootstrap()
    env.new_ticket()
    s.refresh()  # the cached .state fd is the old directory's
    old = tmp_path / "state-old"
    shutil.copytree(env.store_dir / ".state", old)
    shutil.rmtree(env.store_dir / ".state")
    (env.store_dir / ".state").mkdir()
    for d in os.listdir(old):  # the same content in a new directory (another inode), lock file included
        src = old / d
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, env.store_dir / ".state" / d)
    holder = _hold(env.store_dir / ".state" / "lock", tmp_path)  # another writer holds the lock of the new .state
    try:
        s._lock.timeout = 0.3
        with pytest.raises(LockTimeout):
            s.refresh()  # must wait for that holder, never lock the old directory's file and run alongside it
    finally:
        _stop(holder)
    s.refresh()


def test_a_lock_file_swapped_after_the_open_is_retried_once_then_refused(tmp_path):
    p = tmp_path / "lock"
    calls = []

    def verify(fd: int) -> bool:
        calls.append(fd)
        return len(calls) > 1  # the first file was replaced: the retry gets the current one

    with FileLock(p, verify=verify):
        pass
    assert len(calls) == 2
    with pytest.raises(StoreError) as e, FileLock(p, verify=lambda fd: False):
        pass
    assert e.value.code == "validation.path"


# -- 2. re-entry only for the same lock file (Codex P2)


def test_reentry_is_by_inode_not_by_pathname(tmp_path):
    p = tmp_path / "lock"
    with FileLock(p):
        p.unlink()
        p.write_bytes(b"")  # a new file at the same path: another lock
        holder = _hold(p, tmp_path)
        try:
            with pytest.raises(LockTimeout), FileLock(p, timeout=0.3):
                pass
        finally:
            _stop(holder)
    q = tmp_path / "same"
    os.link(p, q)  # the same file under another name: re-entrant, no self-deadlock
    with FileLock(p), FileLock(q, timeout=0.3):
        pass


# -- 3. safe_join is lexical: RootFS is the only filesystem check (Codex P2)


def test_safe_join_never_touches_the_filesystem(tmp_path, monkeypatch):
    from orch.store import paths

    def no_fs(*a, **k):
        raise AssertionError("safe_join looked at the filesystem")

    root = tmp_path / "ws"
    for name in ("islink", "realpath", "lexists", "exists", "isdir"):
        monkeypatch.setattr(os.path, name, no_fs)
    monkeypatch.setattr(os, "lstat", no_fs)
    monkeypatch.setattr(os, "stat", no_fs)
    assert paths.safe_join(root, "tickets/01J9ZK4Q7M3R8T2V6X0B5N1C9D/ticket.json") == (
        root / "tickets/01J9ZK4Q7M3R8T2V6X0B5N1C9D/ticket.json"
    )
    for bad in ("../x", "/abs", "a/../../x", "a\x00b", "a\\b"):
        with pytest.raises(StoreError):
            paths.safe_join(root, bad)
    monkeypatch.undo()


# -- 4. the index has no file path: in memory, saved through the pinned root (Opus / Codex)


def test_the_index_never_opens_a_sqlite_file(env):
    s = env.bootstrap()
    env.new_ticket()
    seen: list[str] = []
    real = sqlite3.connect

    def spy(database, *a, **k):
        seen.append(os.fspath(database))
        return real(database, *a, **k)

    import orch.store.index as index_mod

    index_mod.sqlite3.connect = spy  # type: ignore[assignment]
    try:
        s.rebuild_index()
        s.index.query("SELECT uid FROM tickets")
        env.new_ticket()
        s.close()
        s.index.query("SELECT uid FROM tickets")  # a reconnect after close
    finally:
        index_mod.sqlite3.connect = real  # type: ignore[assignment]
    assert seen and set(seen) == {":memory:"}
    names = os.listdir(env.store_dir / ".state")
    assert "index.sqlite" in names and not [n for n in names if n.startswith("index.sqlite-")]


def test_a_symlinked_index_file_is_replaced_never_written_through(env, outside):
    s = env.bootstrap()
    env.new_ticket()
    victim = outside / "victim.db"
    victim.write_bytes(b"precious")
    idx = env.store_dir / ".state" / "index.sqlite"
    if idx.exists():
        idx.unlink()
    os.symlink(victim, idx)
    before = snapshot(outside)
    with watch(outside) as seen:
        s.rebuild_index()
        env.new_ticket()
        s.index.query("SELECT uid FROM tickets")
    assert seen == [] and snapshot(outside) == before
    assert not idx.is_symlink()


def test_a_swapped_state_dir_gets_no_index_write_after_close_or_rebuild(env, outside, tmp_path, monkeypatch):
    import orch.store.store as store_mod

    s = env.bootstrap()
    env.new_ticket()
    s.close()
    monkeypatch.setattr(store_mod, "check_state_dirs", lambda *a, **k: None)  # the swap lands after the check
    shutil.copytree(env.store_dir / ".state", outside / "state")
    before = snapshot(outside)
    swap(env.store_dir / ".state", outside / "state", tmp_path / "state-moved")
    with watch(outside) as seen:
        for call in (lambda: s.index.query("SELECT uid FROM tickets"), s.rebuild_index, env.new_ticket):
            try:
                call()
            except StoreError as e:
                assert e.code == "validation.path"
    assert seen == [] and snapshot(outside) == before


def test_a_corrupt_index_file_is_rebuilt(env):
    s = env.bootstrap()
    env.new_ticket()
    s.rebuild_index()
    s.close()
    env.store = None
    (env.store_dir / ".state" / "index.sqlite").write_bytes(b"not a database")
    s = env.open()
    assert len(s.index.query("SELECT uid FROM tickets")) == 1
    s.close()
    env.store = None


# -- 7. a FIFO at .state/lock


def test_a_fifo_at_the_lock_is_validation_path(env):
    env.bootstrap().close()
    env.store = None
    lock = env.store_dir / ".state" / "lock"
    lock.unlink()
    os.mkfifo(lock)
    with pytest.raises(StoreError) as e:
        env.open()
    assert e.value.code == "validation.path"


# -- 8. close releases the cached directory fds


def test_close_releases_the_cached_directory_fds(env):
    s = env.bootstrap()
    env.new_ticket()
    s.refresh()
    open_before = len(os.listdir("/dev/fd"))
    s.close()
    assert len(os.listdir("/dev/fd")) <= open_before - 3  # tickets, events, .state (the root fd stays: see Store.close)
    env.store = None


_ = workers


# -- Opus delta D1: two Store instances on one workspace keep each other's index rows


def test_two_store_instances_never_lose_each_others_index_rows(env):
    a = env.bootstrap()
    env.new_ticket("one")
    b = env.other()
    env.new_ticket("two")  # through a
    assert len(b.index.query("SELECT uid FROM tickets")) == 2
    b.create_ticket(actor=env.agent, ticket_type="feature", title="three", owner=env.owner.ref)
    assert len(a.index.query("SELECT uid FROM tickets")) == 3
    env.new_ticket("four")  # a appends after b saved: b's row must survive a's save
    assert sorted(t for (t,) in b.index.query("SELECT title FROM tickets")) == ["four", "one", "three", "two"]
    assert sorted(t for (t,) in a.index.query("SELECT title FROM tickets")) == ["four", "one", "three", "two"]
    b.close()

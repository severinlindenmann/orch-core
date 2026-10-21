"""The store reaches its files only through the pinned root fd (#516): a directory of the workspace swapped for a
symlink *after* every path check passed (the race the checks cannot close) is never followed, so nothing outside the
root is read or written. The path checks are switched off here to stand for "the swap landed right after them"."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import orch.store.store as store_mod
from orch.store import StoreError
from tests.store.outside import snapshot, swap, watch


def _lexical_join(root: Path, rel: str) -> Path:
    return root / rel


@pytest.fixture
def race(monkeypatch):
    """The path checks see a real directory (the swap comes right after them)."""
    monkeypatch.setattr(store_mod, "check_state_dirs", lambda *a, **k: None)
    monkeypatch.setattr(store_mod, "safe_join", _lexical_join)


@pytest.fixture
def outside(tmp_path):
    d = tmp_path / "outside"
    d.mkdir()
    return d


def test_a_swapped_checkpoint_directory_is_never_written(env, race, outside, tmp_path):
    s = env.bootstrap()
    env.new_ticket()
    (outside / "keep.txt").write_bytes(b"precious")
    before = snapshot(outside)
    swap(env.store_dir / ".state" / "checkpoints", outside, tmp_path / "cp-moved")
    with watch(outside) as seen:
        with pytest.raises(StoreError) as e:
            env.new_ticket()
    assert e.value.code == "validation.path"
    assert seen == []
    assert snapshot(outside) == before
    s.close()


def test_a_swapped_ticket_directory_is_never_read(env, race, outside, tmp_path):
    s = env.bootstrap()
    uid = env.new_ticket()
    shutil.copytree(env.store_dir / "tickets" / uid, outside / uid)
    (outside / uid / "ticket.json").write_bytes(b'{"forged": true}\n')
    swap(env.store_dir / "tickets" / uid, outside / uid, tmp_path / "t-moved")
    with watch(outside) as seen:
        with pytest.raises(StoreError) as e:
            s.ticket_json(uid)
        assert e.value.code == "validation.path"
        with pytest.raises(StoreError):
            env.log(uid)
    assert seen == []
    assert (outside / uid / "ticket.json").read_bytes() == b'{"forged": true}\n'


def test_a_swapped_events_directory_is_never_appended_to(env, race, outside, tmp_path):
    s = env.bootstrap()
    shutil.copytree(env.store_dir / "events", outside / "events")
    before = snapshot(outside)
    swap(env.store_dir / "events", outside / "events", tmp_path / "ev-moved")
    with watch(outside) as seen, pytest.raises(StoreError):
        env.new_ticket()
    assert seen == []
    assert snapshot(outside) == before
    s.close()


def test_recovery_never_follows_a_swapped_pending_directory(env, race, outside, tmp_path):
    s = env.bootstrap()
    s.close()
    env.store = None
    fake = outside / "01J9ZP0000000000000000EVIL"
    fake.mkdir()
    (fake / "manifest.json").write_text(json.dumps({"not": "a manifest"}))
    (fake / "0").write_bytes(b"data")
    before = snapshot(outside)
    swap(env.store_dir / ".state" / "pending", outside, tmp_path / "pending-moved")
    with watch(outside) as seen:
        try:
            env.open().close()
        except StoreError as e:
            assert e.code == "validation.path"
        env.store = None
    assert seen == []
    assert snapshot(outside) == before


def test_abandoned_checkpoints_are_never_listed_through_a_symlink(env, race, outside, tmp_path):
    s = env.bootstrap()
    env.new_ticket()
    (outside / "workspace-999.json").write_bytes(b"{}\n")
    (env.store_dir / ".state" / "checkpoints" / "abandoned").mkdir()
    swap(env.store_dir / ".state" / "checkpoints" / "abandoned", outside, tmp_path / "ab-moved")
    with watch(outside) as seen:
        try:
            s._checkpoints.highest_n()
        except StoreError as e:
            assert e.code == "validation.path"
    assert seen == []


def test_the_root_stays_the_directory_that_was_opened(env, outside, tmp_path):
    s = env.bootstrap()
    uid = env.new_ticket()
    shutil.copytree(env.root, outside / "ws")
    (outside / "ws" / ".orch" / "tickets" / uid / "ticket.json").write_bytes(b'{"forged": true}\n')
    before = snapshot(outside)
    moved = tmp_path / "ws-moved"
    env.root.rename(moved)
    os.symlink(outside / "ws", env.root)
    with watch(outside) as seen:
        assert s.ticket_json(uid) == (moved / ".orch" / "tickets" / uid / "ticket.json").read_bytes()
        with pytest.raises(StoreError) as e:  # the project path now names another directory: a write fails closed
            env.log(uid)
        assert e.value.code == "validation.path"
    assert all(x.endswith("/outside/ws") for x in seen)  # only the refused no-follow open of the project path itself
    assert snapshot(outside) == before
    s.close()


def test_open_refuses_a_root_that_is_not_the_one_discovery_saw(env, tmp_path):
    env.bootstrap().close()
    env.store = None
    st = os.stat(env.root)
    ident = (st.st_dev, st.st_ino)
    env.root.rename(tmp_path / "ws-old")
    shutil.copytree(tmp_path / "ws-old", env.root)
    with pytest.raises(StoreError) as e:
        env.open(ident=ident)
    assert e.value.code == "validation.path"


# -- a FIFO in place of a store file (R3 review): refused at open, never blocks the store while it holds the lock


@pytest.fixture
def deadline():
    import signal

    def boom(*_):
        raise TimeoutError("blocked on a FIFO")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(20)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


def _fifo(p):
    if p.exists() or p.is_symlink():
        p.unlink()
    os.mkfifo(p)


def test_fsio_and_rootfs_refuse_a_fifo_without_blocking(tmp_path, deadline):
    from orch.store import fsio
    from orch.store.rootfs import RootFS

    f = tmp_path / "f"
    os.mkfifo(f)
    assert fsio.read_or_none(f) is None
    assert fsio.tail_bytes(f, 10) == (0, b"")
    with pytest.raises(OSError):  # ENXIO (a FIFO without a reader) or NotRegularFile: refused, never blocked
        fsio.append_durable(f, b"x")
    fs = RootFS.at(tmp_path)
    assert fs.read_or_none("f") is None
    assert fs.tail_bytes("f", 10) == (0, b"")
    for flags in (os.O_RDONLY, os.O_WRONLY, os.O_RDWR | os.O_CREAT):
        with pytest.raises(OSError):
            fs.open("f", flags)
    with pytest.raises(OSError):
        fs.append_durable("f", b"x")
    fs.write_atomic("f", b"replaced")  # the FIFO itself is replaced, never opened
    assert f.read_bytes() == b"replaced"
    fs.close()


def test_a_fifo_in_place_of_store_files_never_hangs_the_store(env, deadline):
    s = env.bootstrap()
    uid = env.new_ticket()
    s.close()
    env.store = None
    for p in (
        env.store_dir / ".state" / "applied",
        env.store_dir / "keys.jsonl",
        env.store_dir / "tickets" / uid / "ticket.json",
        env.store_dir / "tickets" / uid / "body.md",
        env.store_dir / ".state" / "checkpoints" / "workspace.json",
    ):
        _fifo(p)
    s = env.open()  # opens (and heals: load="all") without reading any FIFO
    s.scan()  # external edits answered: the FIFOs are replaced by the projections, never read
    assert (env.store_dir / "tickets" / uid / "ticket.json").is_file()
    s.close()
    env.store = None
    _fifo(env.store_dir / "tickets" / uid / "events.jsonl")
    s = env.open()
    assert uid in s.diverged or uid not in s.state.tickets or any(log == uid for log, *_ in s.chain_errors())
    s.close()
    env.store = None

"""The pinned workspace root (#516): every access below the root goes through one root fd, every component
``O_NOFOLLOW``; a directory swapped for a symlink after the root was opened is refused, never followed."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orch.store import StoreError
from orch.store.rootfs import RootFS


def _fds() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "tickets" / "T").mkdir(parents=True)
    (root / "tickets" / "T" / "a.txt").write_bytes(b"inside")
    outside = tmp_path / "outside"
    (outside / "T").mkdir(parents=True)
    (outside / "T" / "a.txt").write_bytes(b"OUTSIDE")
    fs = RootFS.at(root)
    yield fs, root, outside
    fs.close()


def _swap(root: Path, rel: str, target: Path, tmp: Path) -> None:
    """Move the directory ``root/rel`` away and put a symlink to ``target`` in its place."""
    (root / rel).rename(tmp / f"moved-{rel.replace('/', '-')}")
    os.symlink(target, root / rel)


def test_reads_and_writes_below_the_root(ws):
    fs, root, _ = ws
    assert fs.read_or_none("tickets/T/a.txt") == b"inside"
    assert fs.read_or_none(root / "tickets" / "T" / "a.txt") == b"inside"
    fs.write_atomic("tickets/T/b.txt", b"new")
    fs.append_durable("events/workspace.jsonl", b"x\n")
    fs.append_durable("events/workspace.jsonl", b"y\n")
    assert (root / "tickets" / "T" / "b.txt").read_bytes() == b"new"
    assert (root / "events" / "workspace.jsonl").read_bytes() == b"x\ny\n"
    assert sorted(fs.listdir("tickets/T")) == ["a.txt", "b.txt"]
    assert fs.is_dir("tickets") and fs.is_file("tickets/T/a.txt") and not fs.lexists("nope")
    assert fs.tail_bytes("events/workspace.jsonl", 2) == (4, b"y\n")
    fs.replace("tickets/T/b.txt", "tickets/U/c.txt")
    assert (root / "tickets" / "U" / "c.txt").read_bytes() == b"new"
    fs.rmtree("tickets/U")
    assert not (root / "tickets" / "U").exists()


def test_a_directory_swapped_for_a_symlink_after_open_is_refused(ws, tmp_path):
    fs, root, outside = ws
    _swap(root, "tickets", outside, tmp_path)
    for call in (
        lambda: fs.read_or_none("tickets/T/a.txt"),
        lambda: fs.write_atomic("tickets/T/a.txt", b"pwned"),
        lambda: fs.append_durable("tickets/T/a.txt", b"pwned"),
        lambda: fs.listdir("tickets/T"),
        lambda: fs.mkdir("tickets/T/new"),
        lambda: fs.unlink("tickets/T/a.txt"),
        lambda: fs.replace("x", "tickets/T/a.txt"),
        lambda: fs.rmtree("tickets/T"),
    ):
        with pytest.raises((StoreError, OSError)) as e:
            call()
        if isinstance(e.value, StoreError):
            assert e.value.code == "validation.path"
    assert not fs.is_dir("tickets/T") and not fs.lexists("tickets/T/a.txt")
    assert sorted(os.listdir(outside / "T")) == ["a.txt"]
    assert (outside / "T" / "a.txt").read_bytes() == b"OUTSIDE"


def test_a_deeper_directory_swapped_for_a_symlink_is_refused(ws, tmp_path):
    fs, root, outside = ws
    _swap(root, "tickets/T", outside / "T", tmp_path)
    with pytest.raises(StoreError) as e:
        fs.read_or_none("tickets/T/a.txt")
    assert e.value.code == "validation.path"
    with pytest.raises(StoreError):
        fs.write_atomic("tickets/T/a.txt", b"pwned")
    assert (outside / "T" / "a.txt").read_bytes() == b"OUTSIDE"


def test_a_final_symlink_reads_as_absent_and_is_replaced_not_written_through(ws, tmp_path):
    fs, root, outside = ws
    os.symlink(outside / "T" / "a.txt", root / "tickets" / "T" / "l.txt")
    assert fs.read_or_none("tickets/T/l.txt") is None
    with pytest.raises(OSError):
        fs.append_durable("tickets/T/l.txt", b"pwned")
    fs.write_atomic("tickets/T/l.txt", b"mine")
    assert not (root / "tickets" / "T" / "l.txt").is_symlink()
    assert (outside / "T" / "a.txt").read_bytes() == b"OUTSIDE"


def test_the_root_stays_pinned_when_its_path_is_swapped(ws, tmp_path):
    fs, root, outside = ws
    root.rename(tmp_path / "ws-moved")
    os.symlink(outside, root)
    assert fs.read_or_none("tickets/T/a.txt") == b"inside"  # the pinned directory, wherever it is now
    fs.write_atomic("tickets/T/z.txt", b"z")
    assert not (outside / "tickets").exists() and sorted(os.listdir(outside)) == ["T"]
    assert (tmp_path / "ws-moved" / "tickets" / "T" / "z.txt").read_bytes() == b"z"


def test_open_checks_the_identity_discovery_saw(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    ident = (os.stat(root).st_dev, os.stat(root).st_ino)
    RootFS.at(root, ident=ident).close()
    root.rename(tmp_path / "old")
    root.mkdir()
    with pytest.raises(StoreError) as e:
        RootFS.at(root, ident=ident)
    assert e.value.code == "validation.path"
    os.rmdir(root)
    os.symlink(tmp_path / "old", root)
    with pytest.raises(StoreError):
        RootFS.at(root, ident=ident)  # a symlinked root is refused even to the right directory


def test_paths_outside_or_escaping_are_refused(ws, tmp_path):
    fs, root, _ = ws
    for bad in ("../outside/T/a.txt", "tickets/../../x", "/etc/passwd", tmp_path / "outside", "a\0b", "tickets//T"):
        with pytest.raises(StoreError) as e:
            fs.read_or_none(bad)
        assert e.value.code == "validation.path"


def test_fds_stay_bounded(ws):
    fs, root, _ = ws
    before = _fds()
    for i in range(200):
        fs.write_atomic(f"tickets/T/f{i % 5}.txt", b"x")
        fs.read_or_none(f"tickets/T/f{i % 5}.txt")
        fs.listdir("tickets/T")
        fs.lexists("tickets/T/missing/deeper")
        with pytest.raises(StoreError):
            fs.read_or_none("../x")
    assert _fds() == before
    fs.close()
    assert _fds() == before - 1

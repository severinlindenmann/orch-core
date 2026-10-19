"""The pinned workspace root (#516): every access below the root goes through one root fd, every component
``O_NOFOLLOW``; a directory swapped for a symlink after the root was opened is refused, never followed."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from orch.store import StoreError
from orch.store.rootfs import RootFS
from tests._fds import open_fds_on, tree_identities


def _fds(root: Path) -> int:
    """The open descriptors that name a file or directory of this test's own tree (its ``tmp_path``), never the
    process's whole table: other threads, xdist and late finalizers open and close fds of their own meanwhile."""
    return open_fds_on(tree_identities(root.parent))


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
    from orch.store.rootfs import CACHED

    fs, root, _ = ws
    for name in CACHED:
        fs.mkdir(name)  # every cacheable top-level fd is open from here on: at most len(CACHED) of them
    before = _fds(root)
    for i in range(200):
        fs.write_atomic(f"tickets/T/f{i % 5}.txt", b"x")
        fs.read_or_none(f"tickets/T/f{i % 5}.txt")
        fs.listdir("tickets/T")
        fs.lexists("tickets/T/missing/deeper")
        with pytest.raises(StoreError):
            fs.read_or_none("../x")
    assert _fds(root) == before
    fs.close()
    assert _fds(root) == before - 1 - len(CACHED)


# -- the automated scan's two classes: path traversal and resource bounds


@pytest.mark.parametrize(
    "bad",
    ["..", "../x", "tickets/..", "tickets/../../x", "./tickets", "tickets/./T", "tickets//T", "tickets/", "/", "a\0b",
     "tickets\\..\\x", "/etc/passwd"],
)  # fmt: skip
def test_every_api_validates_every_component(ws, bad):
    fs, root, _ = ws
    calls = (
        lambda: fs.read_or_none(bad),
        lambda: fs.open(bad, os.O_RDONLY),
        lambda: fs.write_atomic(bad, b"x"),
        lambda: fs.append_durable(bad, b"x"),
        lambda: fs.listdir(bad),
        lambda: fs.lstat(bad),
        lambda: fs.mkdir(bad),
        lambda: fs.unlink(bad),
        lambda: fs.rmdir(bad),
        lambda: fs.rmtree(bad),
        lambda: fs.replace("tickets/T/a.txt", bad),
        lambda: fs.replace(bad, "tickets/T/z.txt"),
    )
    for call in calls:
        with pytest.raises(StoreError) as e:
            call()
        assert e.value.code == "validation.path"
    assert (root / "tickets" / "T" / "a.txt").read_bytes() == b"inside"


def test_rename_and_unlink_never_reach_outside(ws, tmp_path):
    fs, root, outside = ws
    os.symlink(outside, root / "link")
    for call in (
        lambda: fs.replace("tickets/T/a.txt", "link/T/a.txt"),
        lambda: fs.replace("link/T/a.txt", "tickets/T/x.txt"),
        lambda: fs.unlink("link/T/a.txt"),
        lambda: fs.rmtree("link/T"),
    ):
        with pytest.raises(StoreError):
            call()
    fs.unlink("link")  # the link itself goes, never its target
    fs.write_atomic("link", b"x")
    assert (outside / "T" / "a.txt").read_bytes() == b"OUTSIDE"


def test_rm_at_takes_single_names_only(ws):
    from orch.store.rootfs import rm_at

    fs, _, outside = ws
    for bad in ("../outside", "a/b", ".", "..", ""):
        with pytest.raises(OSError):
            rm_at(fs.fd, bad)
    assert (outside / "T" / "a.txt").exists()


def test_paths_and_trees_are_bounded_in_depth(ws):
    from orch.store.rootfs import MAX_DEPTH, MAX_RM_DEPTH

    fs, root, _ = ws
    with pytest.raises(StoreError):
        fs.read_or_none("/".join(["d"] * (MAX_DEPTH + 1)))
    deep = root / "deep"
    p = deep
    for _ in range(MAX_RM_DEPTH + 3):
        p = p / "d"
    p.mkdir(parents=True)
    before = _fds(root)
    with pytest.raises(OSError):
        fs.rmtree("deep")
    fs.rmtree("deep", ignore_errors=True)
    assert _fds(root) == before
    assert deep.exists()  # not removed, and no fd or stack spent on it beyond the cap


def test_a_symlink_cycle_is_refused_not_looped(ws):
    fs, root, _ = ws
    os.symlink("loop", root / "loop")
    os.symlink(".", root / "tickets" / "self")
    assert fs.read_or_none("loop") is None
    with pytest.raises(StoreError):
        fs.read_or_none("tickets/self/self/self/T/a.txt")
    with pytest.raises(StoreError):
        fs.listdir("loop")


def test_no_fd_leaks_on_failure_paths(ws, tmp_path):
    fs, root, outside = ws
    os.symlink(outside, root / "tickets" / "T" / "sub")
    (root / "tickets" / "T" / "file").write_bytes(b"x")
    os.mkfifo(root / "tickets" / "T" / "fifo")
    fs.listdir("tickets")  # the cached top-level fd is open before counting
    before = _fds(root)
    for _ in range(50):
        for call in (
            lambda: fs.read_or_none("tickets/T/sub/x"),
            lambda: fs.write_atomic("tickets/T/sub/x", b"x"),
            lambda: fs.read_or_none("tickets/T/file/x"),
            lambda: fs.read_or_none("tickets/T"),
            lambda: fs.open("tickets/T/nope", os.O_RDONLY),
            lambda: fs.replace("tickets/T/nope", "tickets/T/n2"),
            lambda: fs.mkdir("tickets/T/file/x"),
        ):
            with contextlib.suppress(OSError, StoreError):
                call()
        assert fs.read_or_none("tickets/T/fifo") is None  # a FIFO never blocks a read
    assert _fds(root) == before


def test_a_cached_top_level_fd_is_dropped_when_its_name_changes(ws, tmp_path):
    fs, root, outside = ws
    assert fs.read_or_none("tickets/T/a.txt") == b"inside"  # caches the fd of tickets/
    _swap(root, "tickets", outside, tmp_path)
    # until the next revalidate the cached fd still names the store's own (moved) directory, never the link's target
    fs.write_atomic("tickets/T/b.txt", b"mine")
    assert (tmp_path / "moved-tickets" / "T" / "b.txt").read_bytes() == b"mine"
    fs.revalidate()
    with pytest.raises(StoreError):
        fs.write_atomic("tickets/T/c.txt", b"pwned")
    assert sorted(os.listdir(outside / "T")) == ["a.txt"]


def test_removing_a_cached_directory_forgets_its_fd(ws):
    fs, root, _ = ws
    fs.listdir("tickets")
    fs.rmtree("tickets")
    fs.mkdir("tickets/X")
    assert (root / "tickets" / "X").is_dir()

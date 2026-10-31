"""Ticket work folders (design r3 part C): ``<project>/.orch-work/<KEY>/``, made and removed without following links."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from orch.layout import (
    Layout,
    LayoutError,
    delete_workdir,
    ensure_workdir,
    workdir_files,
    workdir_read,
    workdir_stat,
    workdir_summary,
)

KEY = "DEMO-0001"


@pytest.fixture
def lay(tmp_path: Path) -> Layout:
    (tmp_path / "proj").mkdir()
    return Layout.new_at(tmp_path / "proj")


@pytest.mark.parametrize(
    "bad", ["..", "../x", "DEMO-0001/..", "demo-1", "", "DEMO-1", "DEMO-0000", "A" * 40, "DEMO-0001\n"]
)
def test_a_key_that_is_not_a_ticket_key_never_builds_a_path(lay, bad):
    with pytest.raises(LayoutError):
        lay.workdir(bad)
    with pytest.raises(LayoutError):
        ensure_workdir(lay, bad)


def test_the_folder_is_next_to_the_store_never_inside_it(lay):
    path = lay.workdir(KEY)
    assert path == lay.project / ".orch-work" / KEY
    assert lay.store not in path.parents and path != lay.store


def test_ensure_makes_it_private_with_a_gitignore_and_reports_the_stat(lay):
    assert workdir_stat(lay, KEY) is None  # a read never creates
    assert not (lay.project / ".orch-work").exists()
    st = ensure_workdir(lay, KEY)
    path = lay.workdir(KEY)
    assert (st.st_dev, st.st_ino) == (path.stat().st_dev, path.stat().st_ino)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert (path.parent / ".gitignore").read_text() == "*\n"  # no repository above ever sees it
    again = ensure_workdir(lay, KEY)
    assert again.st_ino == st.st_ino  # idempotent


def test_a_symlinked_work_dir_or_ticket_folder_is_refused(lay, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (lay.project / ".orch-work").symlink_to(outside)
    for fn in (workdir_stat, ensure_workdir, workdir_summary, delete_workdir):
        with pytest.raises(LayoutError):
            fn(lay, KEY)
    assert list(outside.iterdir()) == []  # nothing was made through the link
    (lay.project / ".orch-work").unlink()
    (lay.project / ".orch-work").mkdir(mode=0o700)
    (lay.project / ".orch-work" / KEY).symlink_to(outside)
    for fn in (workdir_stat, ensure_workdir, workdir_summary, delete_workdir):
        with pytest.raises(LayoutError):
            fn(lay, KEY)
    assert list(outside.iterdir()) == []


def test_a_file_where_the_folder_belongs_is_refused(lay):
    (lay.project / ".orch-work").write_text("x")
    with pytest.raises(LayoutError):
        ensure_workdir(lay, KEY)


def test_delete_removes_exactly_the_ticket_folder_and_follows_no_link(lay, tmp_path):
    ensure_workdir(lay, KEY)
    ensure_workdir(lay, "DEMO-0002")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("mine")
    root = lay.workdir(KEY)
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f.txt").write_text("x" * 10)
    (root / "link").symlink_to(victim)  # a link to a folder outside
    (root / "a" / "filelink").symlink_to(victim / "keep.txt")
    assert workdir_summary(lay, KEY) == {"files": 1, "bytes": 10, "truncated": 0}  # links count as nothing
    assert delete_workdir(lay, KEY) is True
    assert not root.exists()
    assert (victim / "keep.txt").read_text() == "mine"  # not followed
    assert lay.workdir("DEMO-0002").is_dir() and (lay.project / ".orch-work" / ".gitignore").exists()  # siblings stay
    assert delete_workdir(lay, KEY) is False  # none left
    assert workdir_summary(lay, KEY) is None


def test_the_project_root_and_store_are_untouched_by_delete(lay):
    (lay.project / "mine.txt").write_text("m")
    lay.store.mkdir()
    ensure_workdir(lay, KEY)
    delete_workdir(lay, KEY)
    assert (lay.project / "mine.txt").read_text() == "m" and lay.store.is_dir()


def test_a_foreign_owner_is_refused(lay, monkeypatch):
    ensure_workdir(lay, KEY)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(lay.project).st_uid + 1)
    with pytest.raises(LayoutError):
        workdir_stat(lay, KEY)


def test_a_deep_folder_within_the_cap_is_summed_and_deleted(lay):
    ensure_workdir(lay, KEY)
    deep = lay.workdir(KEY)
    base = os.getcwd()
    try:
        os.chdir(deep)
        for _ in range(60):  # one component at a time
            os.mkdir("d")
            os.chdir("d")
        Path("f.txt").write_text("x")
    finally:
        os.chdir(base)
    assert workdir_summary(lay, KEY)["files"] == 1
    assert delete_workdir(lay, KEY) is True and not deep.exists()


def test_the_summary_stops_at_its_cap(lay):
    ensure_workdir(lay, KEY)
    for i in range(30):
        (lay.workdir(KEY) / f"f{i}").write_text("x")
    got = workdir_summary(lay, KEY, limit=10)
    assert got["truncated"] == 1 and got["files"] <= 10


def test_a_mount_point_inside_is_refused_not_entered(lay, monkeypatch):
    ensure_workdir(lay, KEY)
    (lay.workdir(KEY) / "sub").mkdir()
    import orch.layout as L

    root_dev = lay.workdir(KEY).stat().st_dev
    calls = {"n": 0}
    real_fstat = os.fstat

    def fake_fstat(fd):
        st = real_fstat(fd)
        calls["n"] += 1
        if calls["n"] == 1:  # the first fstat in delete is the root's: pretend another device for it
            return os.stat_result((*tuple(st)[:2], root_dev + 1, *tuple(st)[3:]))
        return st

    monkeypatch.setattr(L.os, "fstat", fake_fstat)
    with pytest.raises(LayoutError):
        delete_workdir(lay, KEY)
    monkeypatch.undo()
    assert (lay.workdir(KEY) / "sub").is_dir()


def test_cost_is_linear_and_depth_is_capped(lay, monkeypatch):
    import orch.layout as L

    ensure_workdir(lay, KEY)
    base = os.getcwd()
    try:
        os.chdir(lay.workdir(KEY))
        for _ in range(40):
            os.mkdir("d")
            os.chdir("d")
        Path("f").write_text("x")
    finally:
        os.chdir(base)
    opens = []
    real = os.open
    monkeypatch.setattr(L.os, "open", lambda *a, **k: (opens.append(1), real(*a, **k))[1])
    assert workdir_summary(lay, KEY)["files"] == 1
    assert len(opens) < 40 * 3  # ~one open per level plus the walk to the folder, not d squared
    monkeypatch.undo()
    delete_workdir(lay, KEY)
    ensure_workdir(lay, KEY)
    try:
        os.chdir(lay.workdir(KEY))
        for _ in range(L.WORKDIR_DEPTH + 5):
            os.mkdir("d")
            os.chdir("d")
    finally:
        os.chdir(base)
    assert workdir_summary(lay, KEY)["truncated"] == 1
    with pytest.raises(LayoutError):
        delete_workdir(lay, KEY)
    assert lay.workdir(KEY).is_dir()  # the refusal came before anything was deleted


def test_an_entry_added_after_the_scan_is_not_deleted_and_the_delete_refuses(lay, monkeypatch):
    import orch.layout as L

    ensure_workdir(lay, KEY)
    root = lay.workdir(KEY)
    (root / "old.txt").write_text("a")
    monkeypatch.setattr(L, "_after_look", lambda: (root / "new.txt").write_text("b"))
    with pytest.raises(LayoutError):
        delete_workdir(lay, KEY)
    assert (root / "new.txt").exists()


def test_a_directory_swapped_after_the_scan_refuses(lay, monkeypatch, tmp_path):
    import orch.layout as L

    ensure_workdir(lay, KEY)
    root = lay.workdir(KEY)
    (root / "sub").mkdir()
    (root / "sub" / "f").write_text("x")

    def swap():
        (root / "sub" / "f").unlink()
        (root / "sub").rmdir()
        (root / "sub").mkdir()  # another directory under the same name
        (root / "sub" / "g").write_text("y")

    monkeypatch.setattr(L, "_after_look", swap)
    with pytest.raises(LayoutError):
        delete_workdir(lay, KEY)
    assert (root / "sub" / "g").exists()


def test_files_and_read_see_only_plain_files_directly_in_the_folder(lay, tmp_path):
    assert workdir_files(lay, KEY) == []
    ensure_workdir(lay, KEY)
    root = lay.workdir(KEY)
    (root / "b.txt").write_text("bb")
    (root / "a.txt").write_text("a")
    (root / "sub").mkdir()
    (root / "sub" / "c.txt").write_text("c")
    outside = tmp_path / "o.txt"
    outside.write_text("secret")
    (root / "link.txt").symlink_to(outside)
    os.link(outside, root / "hard.txt")
    assert workdir_files(lay, KEY) == [("a.txt", 1), ("b.txt", 2)]
    assert workdir_files(lay, KEY, limit=1) == [("a.txt", 1)]
    assert workdir_read(lay, KEY, "b.txt", 10) == b"bb"
    for bad in ("../o.txt", "sub/c.txt", "sub", "link.txt", "hard.txt", "..", ".", "", "x\0y", "gone.txt"):
        with pytest.raises(LayoutError):
            workdir_read(lay, KEY, bad, 10)
    with pytest.raises(LayoutError):
        workdir_read(lay, KEY, "b.txt", 1)  # over the cap

"""Live worktree diff (focus mode P2-5): the uncommitted changes as files plus unified text, read without git."""

from __future__ import annotations

import os

from orch.gitread import local
from orch.gitread.client import HostGitReader, PinStore, ReaderPool

from .conftest import git_version  # noqa: F401  (fixture module import keeps conftest loaded)


def seeded(make_repo):
    r = make_repo()
    r.write("src/app.py", "one\ntwo\nthree\n")
    r.write("gone.txt", "bye\n")
    r.write("bin.dat", b"\0\1\2")
    r.commit()
    return r


def by_path(got):
    return {f["path"]: f for f in got["files"]}


def test_modified_untracked_deleted_and_binary_are_listed_with_counts(make_repo):
    r = seeded(make_repo)
    r.write("src/app.py", "one\nTWO\nthree\nfour\n")
    r.write("new.txt", "a\nb\n")
    (r.path / "gone.txt").unlink()
    r.write("bin.dat", b"\0\1\2\3")
    got = local.READER.worktree_diff(r.path)
    f = by_path(got)
    assert f["src/app.py"]["status"] == "M" and (f["src/app.py"]["added"], f["src/app.py"]["removed"]) == (2, 1)
    assert f["new.txt"]["status"] == "?" and f["new.txt"]["added"] == 2
    assert f["gone.txt"]["status"] == "D" and f["gone.txt"]["removed"] == 1
    assert f["bin.dat"]["binary"] is True and f["bin.dat"]["added"] == 0
    assert got["head"] == r.sha("HEAD") and got["truncated"] is False
    u = got["unified"]
    assert "diff --git a/src/app.py b/src/app.py" in u and "+TWO" in u and "-two" in u
    assert "--- /dev/null" in u and "+++ b/new.txt" in u and "+++ /dev/null" in u
    assert "bin.dat\n@@" not in u


def test_a_clean_tree_is_empty(make_repo):
    r = seeded(make_repo)
    got = local.READER.worktree_diff(r.path)
    assert got["files"] == [] and got["unified"] == ""


def test_symlink_and_oversize_files_are_listed_without_hunks(make_repo):
    r = seeded(make_repo)
    os.symlink("src/app.py", r.path / "ln")
    r.write("big.txt", "x\n" * (600 * 1024))
    got = local.READER.worktree_diff(r.path)
    f = by_path(got)
    assert f["ln"]["binary"] is True
    assert f["big.txt"].get("too_large") is True and f["big.txt"]["added"] == 0
    assert "big.txt" not in got["unified"]


def test_caps_truncate_text_and_file_count(make_repo):
    r = seeded(make_repo)
    for i in range(10):
        r.write(f"f{i}.txt", "line\n" * 300)
    got = local.READER.worktree_diff(r.path, max_bytes=2048)
    assert got["truncated"] is True and len(got["files"]) == 10 and len(got["unified"]) <= 2048
    got = local.READER.worktree_diff(r.path, max_files=3)
    assert len(got["files"]) == 3 and got["truncated"] is True


def test_the_host_child_answers_the_same_as_direct_mode(make_repo):
    r = seeded(make_repo)
    r.write("src/app.py", "one\nTWO\nthree\n")
    r.write("new.txt", "a\n")
    reader = HostGitReader(ReaderPool(), PinStore(), tree_checks=True)
    reader.sync([r.path])
    got = reader.worktree_diff(r.path, "proj", principal="p")
    want = local.READER.worktree_diff(r.path)
    assert got["files"] == want["files"] and got["unified"] == want["unified"]
    assert got["head"] == r.sha("HEAD")

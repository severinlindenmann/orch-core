"""#565 parity corpus: direct mode's clean check is orch's reader in-process (``orch.gitread.local.READER``), and on
every repository state below it must agree with plain git (clean config, empty ``HOME``) on dirty/clean and on the
list of changed paths. git's answer is ``status --porcelain`` (untracked ``normal``, ``--ignore-submodules=dirty``,
no renames) plus a staged gitlink change (``diff --cached --raw``) plus the flagged files (``ls-files -v``), exactly
what #561's ``dirty`` combined. States the reader cannot read are "refused" (unknown, never clean): a populated
submodule (#561's rule), a split or sparse index.

Paths are compared after one normalisation: git lists an untracked directory once (``d/``), the reader lists the
files in it; a reader path below a directory git lists is counted as that directory.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from orch.gitread.errors import Unavailable
from orch.gitread.local import READER

from .conftest import GIT


def plain_git(r, where: Path | None = None) -> set[str]:
    """The changed paths plain git reports for the working copy ``where`` (default ``r.path``)."""
    cwd = str(where or r.path)

    def run(*args: str) -> bytes:
        return subprocess.run([GIT, "--no-optional-locks", "-C", cwd, *args], capture_output=True, env=r.env,
                              check=True, stdin=subprocess.DEVNULL).stdout  # fmt: skip

    out: set[str] = set()
    status = run("status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=dirty",
                 "--no-renames")  # fmt: skip
    for entry in status.split(b"\0"):
        if entry:
            out.add(entry[3:].decode())
    raw = run("diff", "--cached", "--raw", "-z", "--no-renames", "--no-relative")
    fields = raw.split(b"\0")
    for i in range(0, len(fields) - 1, 2):
        meta = fields[i].lstrip(b":").split()
        if len(meta) >= 2 and b"160000" in (meta[0], meta[1]):
            out.add(fields[i + 1].decode())
    for line in run("ls-files", "-v", "--full-name", "--", ":(top)").decode().splitlines():
        if line[:1] == "S" or line[:1].islower():
            out.add(line[2:])
    return out


def reader(r, where: Path | None = None) -> set[str]:
    got = READER.tree_check(where or r.path, r.env)
    assert not got["more"]
    return {base64.b64decode(c["path_b64"]).decode() for c in got["changed"]}


def agree(r, where: Path | None = None) -> set[str]:
    """Both answers, normalised; asserts they agree and returns the paths."""
    mine = reader(r, where)  # first: the reader never writes the index, git might (it does not: no optional locks)
    theirs = plain_git(r, where)
    dirs = sorted(p for p in theirs if p.endswith("/"))
    norm = {next((d for d in dirs if p.startswith(d)), p) for p in mine}
    assert norm == theirs, (sorted(mine), sorted(theirs))
    lines = READER.dirty(where or r.path, r.env)
    assert (lines == "") == (not theirs), lines  # the dirty/clean answer agrees too
    return theirs


def base(make_repo):
    r = make_repo()
    r.write("a.txt", "one\n")
    r.write("d/b.txt", "bee\n")
    r.write("x.sh", "#!/bin/sh\n", mode=0o644)
    r.write(".gitignore", "*.log\nbuild/\n")
    r.commit("base")
    return r


def test_a_clean_repository_is_clean_for_both(make_repo):
    r = base(make_repo)
    assert agree(r) == set()


@pytest.mark.parametrize(
    "state",
    ["modified", "added_staged", "deleted", "deleted_staged", "mode", "untracked", "untracked_dir", "staged_only",
     "same_size_rewrite", "type_change"],
)  # fmt: skip
def test_work_tree_and_index_changes(make_repo, state):
    r = base(make_repo)
    if state == "modified":
        r.write("a.txt", "two\n")
    elif state == "added_staged":
        r.write("new.txt", "n\n")
        r.git("add", "new.txt")
    elif state == "deleted":
        os.remove(r.path / "d" / "b.txt")
    elif state == "deleted_staged":
        r.git("rm", "-q", "d/b.txt")
    elif state == "mode":
        os.chmod(r.path / "x.sh", 0o755)
    elif state == "untracked":
        r.write("u.txt", "u\n")
    elif state == "untracked_dir":
        r.write("newdir/f1", "1\n")
        r.write("newdir/sub/f2", "2\n")
    elif state == "staged_only":  # staged, then the work tree put back: only the index differs from HEAD
        r.write("a.txt", "staged\n")
        r.git("add", "a.txt")
        r.write("a.txt", "one\n")
    elif state == "same_size_rewrite":  # same size, same second: the stat may match, the bytes do not
        st = os.stat(r.path / "a.txt")
        r.write("a.txt", "ONE\n")
        os.utime(r.path / "a.txt", ns=(st.st_atime_ns, st.st_mtime_ns))
    elif state == "type_change":
        os.remove(r.path / "a.txt")
        os.symlink("d/b.txt", r.path / "a.txt")
    assert agree(r)


def test_a_symlink_target_change(make_repo):
    r = base(make_repo)
    os.symlink("a.txt", r.path / "link")
    r.commit("link")
    assert agree(r) == set()
    os.remove(r.path / "link")
    os.symlink("d/b.txt", r.path / "link")
    assert agree(r) == {"link"}


def test_untracked_versus_ignored_and_a_nested_gitignore(make_repo):
    r = base(make_repo)
    r.write("debug.log", "x\n")  # ignored at the top
    r.write("build/out.o", "x\n")  # ignored directory
    r.write("d/.gitignore", "*.tmp\n!keep.tmp\n")
    r.commit("nested ignore")
    r.write("d/scratch.tmp", "x\n")  # ignored by the nested file
    r.write("d/keep.tmp", "x\n")  # re-included by its negation
    r.write("d/note.md", "x\n")  # untracked
    assert agree(r) == {"d/keep.tmp", "d/note.md"}


def test_info_exclude_and_the_persons_global_ignore(make_repo):
    r = base(make_repo)
    (r.gitdir / "info").mkdir(exist_ok=True)
    (r.gitdir / "info" / "exclude").write_text("*.secret\n")
    r.write("k.secret", "x\n")
    assert agree(r) == set()
    home = Path(r.env["HOME"])
    (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {home / 'gi'}\n")
    (home / "gi").write_text("*.swp\n")
    r.write("a.swp", "x\n")
    r.env["GIT_CONFIG_GLOBAL"] = str(home / ".gitconfig")  # plain git reads the same person file
    try:
        assert agree(r) == set()
    finally:
        r.env["GIT_CONFIG_GLOBAL"] = os.devnull


def _gitlink(r, sha: str = "1" * 40) -> None:
    r.git("update-index", "--add", "--cacheinfo", f"160000,{sha},sub")
    r.git("commit", "-q", "-m", "gitlink")
    (r.path / "sub").mkdir(exist_ok=True)  # unpopulated, as after a clone without --recurse-submodules


@pytest.mark.parametrize("how", ["moved_staged", "replaced", "deleted"])
def test_a_submodule_pointer_moved_replaced_or_deleted(make_repo, how):
    r = base(make_repo)
    _gitlink(r)
    assert agree(r) == set()
    if how == "moved_staged":
        r.git("update-index", "--cacheinfo", f"160000,{'2' * 40},sub")
    elif how == "replaced":
        (r.path / "sub").rmdir()
        r.write("sub", "a file now\n")
    else:
        (r.path / "sub").rmdir()
    assert agree(r) == {"sub"}


def test_a_populated_submodule_is_refused_never_clean(make_repo):
    """#561's rule, kept: the reader does not open a submodule, so a checked-out one is unknown."""
    r = base(make_repo)
    src = make_repo("subsrc")
    src.write("s", "s\n")
    src.commit("s")
    r.git("-c", "protocol.file.allow=always", "submodule", "-q", "add", str(src.path), "sub")
    r.git("commit", "-q", "-m", "sub")
    with pytest.raises(Unavailable, match="submodule"):
        READER.tree_check(r.path, r.env)


def test_a_subdirectory_workspace_reads_the_whole_repository(make_repo):
    r = base(make_repo)
    r.write("a.txt", "outside the subdirectory\n")
    r.git("update-index", "--skip-worktree", "x.sh")  # a flagged file elsewhere
    assert agree(r) == {"a.txt", "x.sh"}
    assert reader(r, r.path / "d") == {"a.txt", "x.sh"}  # from d/: the same, whole repository


def test_packed_objects_and_packed_refs(make_repo):
    r = base(make_repo)
    r.write("a.txt", "two\n")
    r.commit("second")
    r.git("gc", "-q", "--prune=now")
    r.git("pack-refs", "--all")
    assert not any((r.gitdir / "objects").glob("[0-9a-f][0-9a-f]/*"))  # every object packed
    assert (r.gitdir / "packed-refs").exists() and not (r.gitdir / "refs" / "heads" / "main").exists()
    assert agree(r) == set()
    r.write("d/b.txt", "changed\n")
    assert agree(r) == {"d/b.txt"}


def test_a_linked_worktree(make_repo):
    r = base(make_repo)
    wt = r.path.parent / "wt"
    r.git("worktree", "add", "-q", "-b", "side", str(wt))
    assert agree(r, wt) == set()
    (wt / "a.txt").write_text("in the worktree\n")
    (wt / "w.new").write_text("x\n")
    assert agree(r, wt) == {"a.txt", "w.new"}
    assert agree(r) == set()  # the main working copy is untouched


def test_an_unborn_head(make_repo):
    r = make_repo()
    assert agree(r) == set()
    r.write("first.txt", "1\n")
    assert agree(r) == {"first.txt"}
    r.git("add", "first.txt")
    assert agree(r) == {"first.txt"}


def test_a_detached_head(make_repo):
    r = base(make_repo)
    r.git("checkout", "-q", "--detach")
    assert agree(r) == set()
    r.write("a.txt", "detached change\n")
    assert agree(r) == {"a.txt"}


def test_index_v4(make_repo):
    r = base(make_repo)
    r.git("update-index", "--index-version", "4")
    assert (r.gitdir / "index").read_bytes()[4:8] == b"\0\0\0\4"
    assert agree(r) == set()
    r.write("d/b.txt", "v4 change\n")
    r.write("v4.new", "x\n")
    assert agree(r) == {"d/b.txt", "v4.new"}


def test_a_split_index_is_refused(make_repo):
    r = base(make_repo)
    r.git("update-index", "--split-index")
    r.write("a.txt", "two\n")
    assert plain_git(r) == {"a.txt"}  # git reads it
    with pytest.raises(Unavailable):  # its entries name no paths of their own (they are in the shared index)
        READER.tree_check(r.path, r.env)


def test_a_sparse_checkout_is_refused(make_repo):
    r = base(make_repo)
    r.git("sparse-checkout", "set", "--cone", "--sparse-index", "d")
    with pytest.raises(Unavailable):
        READER.tree_check(r.path, r.env)


def test_an_untracked_nested_repository(make_repo):
    r = base(make_repo)
    inner = make_repo("repo/inner")
    inner.write("i", "i\n")
    inner.commit("i")
    assert agree(r) == {"inner/"}


def test_no_repository_at_all(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(Unavailable, match="not a git working copy"):
        READER.tree_check(tmp_path / "plain")
    shutil.rmtree(tmp_path / "plain")

"""``diff`` is a valid unified diff that ``git apply`` accepts and that reproduces the head exactly; on the simple
fixtures it equals ``git diff --no-ext-diff --no-textconv --no-renames`` byte for byte (hunk placement may differ
where git uses its own heuristics) (H3b1). Base/head follow
section 0.8: head is the branch tip, base the merge base with the default branch (``main``, then ``master``), else
the head's first parent, else ``observe.unavailable``."""

from __future__ import annotations

import base64
import os
import subprocess

import pytest

from orch.gitread import local
from orch.gitread.errors import Unavailable

from .conftest import Repo

GIT_DIFF = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color")


def _names(block: bytes, path: str) -> bool:
    """The block is for ``path`` or for something under it (a pathspec names a directory too)."""
    from orch.gitread.patchtext import quote_path

    head = block.split(b"\n", 1)[0]
    exact = b"diff --git " + quote_path(b"a/" + path.encode()) + b" "
    return head.startswith(exact) or head.startswith(b"diff --git a/" + path.encode() + b"/")


def patch_of(result: dict) -> bytes:
    return base64.b64decode(result["patch"])


def build_fixture(r: Repo) -> tuple[str, str]:
    """A ``main`` base and a ``feat`` head covering every kind of change. Returns (base, head)."""
    body = "".join(f"line {i}\n" for i in range(60))
    r.write("text.txt", body)
    r.write("func.c", "int a;\n\nint main(void)\n{\n" + "".join(f"    x({i});\n" for i in range(40)) + "}\n")
    r.write("bin.dat", b"\x00\x01\x02binary\x00" * 50)
    r.write("bin_gone.dat", b"\x00gone")
    r.write("mode.sh", "echo hi\n")
    r.write("mode_and_text.sh", "a\nb\n")
    r.write("empty_stays.txt", "")
    r.write("becomes_empty.txt", "something\n")
    r.write("from_empty.txt", "")
    r.write("empty_deleted.txt", "")
    r.write("deleted.txt", "bye\nbye\n")
    r.write("noeol.txt", "no newline")
    r.write("to_link", "was a file\n")
    r.write("file_to_dir", "x\n")
    r.write("with space.txt", "s\n")
    r.write("café.txt", "accent\n")
    r.write('q"uote.txt', "quoted\n")
    r.write("dir/a.txt", "a\n")
    r.write("dir/sub/b.txt", "b\n")
    r.write("moved_from.txt", "".join(f"moved {i}\n" for i in range(20)))
    os.symlink("text.txt", r.path / "link")
    base = r.commit("base")
    r.git("checkout", "-q", "-b", "feat")
    r.write("text.txt", body.replace("line 5\n", "line five\n").replace("line 40\n", "") + "tail\n")
    r.write("func.c", "int a;\n\nint main(void)\n{\n" + "".join(f"    x({i});\n" for i in range(40) if i != 30) + "}\n")
    r.write("bin.dat", b"\x00\x01\x02binary\x00" * 51)
    (r.path / "bin_gone.dat").unlink()
    r.write("new_bin.png", b"\x89PNG\r\n\x1a\n\x00\x00")
    (r.path / "mode.sh").chmod(0o755)
    r.write("mode_and_text.sh", "a\nB\n", mode=0o755)
    r.write("becomes_empty.txt", "")
    r.write("from_empty.txt", "now text\n")
    (r.path / "empty_deleted.txt").unlink()
    r.write("new_empty.txt", "")
    (r.path / "deleted.txt").unlink()
    r.write("noeol.txt", "no newline, changed")
    (r.path / "to_link").unlink()
    os.symlink("text.txt", r.path / "to_link")
    (r.path / "file_to_dir").unlink()
    r.write("file_to_dir/inner.txt", "inner\n")
    r.write("with space.txt", "s2\n")
    r.write("café.txt", "accent 2\n")
    r.write('q"uote.txt', "quoted 2\n")
    r.write("dir/sub/b.txt", "b2\n")
    r.write("dir/new/c.txt", "c\n")
    (r.path / "link").unlink()
    os.symlink("func.c", r.path / "link")
    os.rename(r.path / "moved_from.txt", r.path / "moved_to.txt")  # renames off: a deletion and an addition
    r.commit("feat")
    sub = r.sha("HEAD~1")  # a gitlink entry pointing at some commit
    r.git("update-index", "--add", "--cacheinfo", f"160000,{sub},vendor/sub")
    r.git("commit", "-q", "-m", "gitlink")
    head = r.sha("HEAD")
    return base, head


SIMPLE = [
    "bin.dat", "bin_gone.dat", "new_bin.png", "mode.sh", "mode_and_text.sh", "deleted.txt", "new_empty.txt",
    "empty_deleted.txt", "becomes_empty.txt", "from_empty.txt", "noeol.txt", "with space.txt", "café.txt",
    'q"uote.txt', "dir/sub/b.txt", "dir/new/c.txt", "to_link", "link", "vendor/sub", "moved_from.txt",
    "moved_to.txt", "file_to_dir",
]  # fmt: skip


def split_patch(patch: bytes) -> list[bytes]:
    """The ``diff --git`` blocks (a content line always starts with a space, ``+``, ``-`` or a backslash)."""
    blocks: list[bytes] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith(b"diff --git "):
            blocks.append(b"")
        blocks[-1] += line
    return blocks


def apply_and_compare(r: Repo, patch: bytes, base: str, head: str, tmp_path) -> None:
    """``git apply --check`` accepts the patch on the base tree, and applying it gives exactly the head tree. A
    binary file only says ``differ`` (no data to apply), so the patch is applied without those blocks and the
    expectation is the head tree with the binary paths left as they were in the base."""
    binary = [b for b in split_patch(patch) if b"\nBinary files " in b]
    text = b"".join(b for b in split_patch(patch) if b not in binary)
    assert len(binary) == 3

    def run(idx: str, *args: str, **kw) -> subprocess.CompletedProcess:
        env = {**r.env, "GIT_INDEX_FILE": str(tmp_path / idx)}
        return subprocess.run(["git", "-C", str(r.path), *args], env=env, capture_output=True, check=True, **kw)

    run("idx", "read-tree", base)
    run("idx", "apply", "--cached", "--unidiff-zero", "--check", "-", input=text)
    run("idx", "apply", "--cached", "--unidiff-zero", "-", input=text)
    run("want", "read-tree", head)
    for path in ("bin.dat", "bin_gone.dat", "new_bin.png"):
        in_base = run("want", "ls-tree", base, path).stdout.split()
        if in_base:
            run("want", "update-index", "--add", "--cacheinfo", f"{in_base[0].decode()},{in_base[2].decode()},{path}")
        else:
            run("want", "update-index", "--force-remove", path)
    assert run("idx", "write-tree").stdout == run("want", "write-tree").stdout


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("packed", [False, True])
def test_diff_is_a_valid_patch_and_equals_git_on_simple_files(make_repo, tmp_path, fmt, packed):
    r = make_repo(object_format=fmt)
    base, head = build_fixture(r)
    if packed:
        r.git("repack", "-adf", "-q", "--depth=10", "--window=50")
        r.git("pack-refs", "--all")
    for ctx in (3, 0, 1, 10):
        got = local.diff(r.path, branch="feat", default_branch=None, context=ctx, max_bytes=1048576)
        assert (got["base_sha"], got["head_sha"]) == (base, head)
        assert got["truncated"] is False
        apply_and_compare(r, patch_of(got), base, head, tmp_path)
        blocks = split_patch(patch_of(got))
        for path in SIMPLE:
            want = r.git(*GIT_DIFF, f"-U{ctx}", base, head, "--", f":(literal){path}", raw=True)
            mine = b"".join(b for b in blocks if b.startswith(b"diff --git ") and _names(b, path))
            assert mine == want, (ctx, path)
    statuses = {f["path"]: f["status"] for f in got["files"]}
    assert statuses["to_link"] == "T" and statuses["link"] == "M" and statuses["deleted.txt"] == "D"
    assert statuses["new_bin.png"] == "A" and statuses["moved_to.txt"] == "A" and statuses["moved_from.txt"] == "D"
    assert statuses["vendor/sub"] == "A"
    numstat = r.git("diff", "--numstat", "--no-renames", base, head).splitlines()
    by_path = {f["path"]: f for f in got["files"]}
    for line in numstat:
        add, dele, path = line.split("\t", 2)
        if path.startswith('"'):
            continue  # a quoted name; checked through the patch text
        f = by_path[path]
        if add == "-":
            assert f["binary"] and (f["add"], f["del"]) == (0, 0)
        else:
            assert (f["add"], f["del"]) == (int(add), int(dele)), path


def test_base_is_the_merge_base_with_main_not_its_tip(make_repo):
    r = make_repo()
    r.write("a.txt", "1\n")
    r.commit()
    r.git("checkout", "-q", "-b", "feat")
    r.write("a.txt", "1\nfeat\n")
    head = r.commit()
    r.git("checkout", "-q", "main")
    r.write("b.txt", "main moved on\n")
    r.commit()
    got = local.diff(r.path, branch="feat", default_branch=None)
    assert got["base_sha"] == r.sha("main~1") and got["head_sha"] == head
    assert patch_of(got) == r.git(*GIT_DIFF, "main...feat", raw=True)  # one hunk: identical to git


def test_master_is_the_fallback_and_a_declared_default_wins(make_repo):
    r = make_repo(branch="master")
    r.write("a.txt", "1\n")
    first = r.commit()
    r.git("checkout", "-q", "-b", "trunk")
    r.write("a.txt", "2\n")
    trunk = r.commit()
    r.git("checkout", "-q", "-b", "feat")
    r.write("a.txt", "3\n")
    r.commit()
    assert local.diff(r.path, branch="feat", default_branch=None)["base_sha"] == first  # no main: master
    assert local.diff(r.path, branch="feat", default_branch="trunk")["base_sha"] == trunk


def test_no_merge_base_takes_the_first_parent_and_a_root_commit_is_unavailable(make_repo):
    r = make_repo()
    r.write("a.txt", "1\n")
    r.commit()
    r.git("checkout", "-q", "--orphan", "lonely")
    r.write("a.txt", "x\n")
    root = r.commit("root")
    with pytest.raises(Unavailable, match="no parent"):
        local.diff(r.path, branch="lonely", default_branch=None)
    r.write("a.txt", "y\n")
    head = r.commit("second")
    got = local.diff(r.path, branch="lonely", default_branch=None)
    assert (got["base_sha"], got["head_sha"]) == (root, head)
    assert patch_of(got) == r.git(*GIT_DIFF, root, head, raw=True)


def test_a_missing_branch_is_unavailable(make_repo):
    r = make_repo()
    r.write("a.txt", "1\n")
    r.commit()
    with pytest.raises(Unavailable, match="does not exist"):
        local.diff(r.path, branch="nope", default_branch=None)


def test_max_bytes_cuts_on_a_line_boundary(make_repo):
    r = make_repo()
    r.write("a.txt", "".join(f"{i}\n" for i in range(2000)))
    r.commit()
    r.git("checkout", "-q", "-b", "feat")
    r.write("a.txt", "".join(f"{i} changed\n" for i in range(2000)))
    r.commit()
    full = r.git(*GIT_DIFF, "main", "feat", raw=True)
    got = local.diff(r.path, branch="feat", default_branch=None, max_bytes=1024)
    cut = patch_of(got)
    assert got["truncated"] is True and len(cut) <= 1024 and cut.endswith(b"\n") and full.startswith(cut)
    assert got["files"][0]["add"] == 2000  # the counts are of the whole diff


def test_opus_16mib_newline_blob_is_too_large_to_diff_and_cheap(make_repo):
    """73 KB on disk, 16 Mi lines: reported "too large to diff" before any line split (it cost 2.2 GiB before)."""
    import resource
    import time

    r = make_repo()
    r.write("small.txt", "1\n")
    r.commit()
    r.git("checkout", "-q", "-b", "feat")
    r.write("nl.txt", b"\n" * (16 << 20))
    r.write("lines.txt", b"\n" * 300_000)  # under 8 MiB, over the line cap
    r.write("small.txt", "2\n")
    r.commit()
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t = time.monotonic()
    got = local.diff(r.path, branch="feat", default_branch=None)
    assert time.monotonic() - t < 5
    grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
    unit = 1 if os.uname().sysname == "Darwin" else 1024  # bytes on macOS, KiB on Linux
    assert grown * unit < 400 << 20
    by = {f["path"]: f for f in got["files"]}
    assert by["nl.txt"]["too_large"] is True and by["nl.txt"]["binary"] is True
    assert by["small.txt"]["too_large"] is False and by["small.txt"]["add"] == 1
    text = patch_of(got)
    assert b"diff --git a/nl.txt b/nl.txt\nToo large to diff: a side is over 8 MiB\n" in text
    assert b"diff --git a/lines.txt b/lines.txt\nToo large to diff: a side has over 200000 lines\n" in text
    assert by["lines.txt"]["too_large"] is True
    assert b"+2\n" in text


def test_base_labels_and_the_commit_list(make_repo):
    r = make_repo()
    r.write("a.txt", "1\n")
    base = r.commit()
    r.git("checkout", "-q", "-b", "feat")
    shas = []
    for i, subject in enumerate(["one", "two \x1b[2J‮evil", "three"]):
        r.write("a.txt", f"{i}\n")
        r.git("add", "-A")
        r.git("commit", "-q", "-m", subject)
        shas.append(r.sha("HEAD"))
    got = local.diff(r.path, branch="feat", default_branch=None)
    assert (got["base_sha"], got["base_source"], got["base_moved"]) == (base, "local_ref", 0)
    assert [c["sha"] for c in got["commits"]] == shas[::-1] and got["commits_more"] == 0
    assert got["commits"][1]["subject"] == "two [2Jevil"  # ESC and the RTL override removed
    # a declared default branch that does not exist: never a silent one-commit diff
    with pytest.raises(Unavailable, match="declared default branch"):
        local.diff(r.path, branch="feat", default_branch="trunk")
    # main deleted, no master: first parent, labelled
    r.git("branch", "-D", "main")
    got = local.diff(r.path, branch="feat", default_branch=None)
    assert (got["base_sha"], got["base_source"]) == (shas[1], "first_parent")
    assert [c["sha"] for c in got["commits"]] == [shas[2]]


def test_the_commit_list_is_capped_at_50(make_repo):
    r = make_repo()
    r.write("a.txt", "0\n")
    r.commit()
    r.git("checkout", "-q", "-b", "feat")
    for i in range(53):
        r.git("commit", "-q", "--allow-empty", "-m", f"c{i}")
    got = local.diff(r.path, branch="feat", default_branch=None)
    assert len(got["commits"]) == 50 and got["commits_more"] == 3
    assert got["commits"][0]["subject"] == "c52"

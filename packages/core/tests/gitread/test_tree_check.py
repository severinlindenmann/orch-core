"""``tree.check`` (H10a): orch's own index and worktree reader, never git (D-P2-19, F2, N3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from orch.gitread import limits, local, treecheck, treejob
from orch.gitread.client import HostGitReader, PinStore, ReaderPool
from orch.gitread.errors import Unavailable

from .conftest import case_sensitive, git_version


def changed(r, **kw):
    got = local.tree_check(r.path, **kw)
    return {c["path"]: c["why"] for c in got["changed"]}, got["more"]


def base(make_repo, **kw):
    r = make_repo(**kw)
    r.write("a.txt", "one\n")
    r.write("d/e/b.txt", "two\n")
    r.write("x.sh", "#!/bin/sh\n", mode=0o755)
    r.write(".gitignore", "*.log\nbuild/\n!keep.log\n")
    os.symlink("a.txt", r.path / "link")
    r.commit()
    return r


def git_status(r) -> set[str]:
    out = r.git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none", raw=True)
    return {e[3:].decode("utf-8", "surrogateescape") for e in out.split(b"\0") if e}


# --- what it sees ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_a_clean_tree_is_clean_and_every_kind_of_change_is_seen(make_repo, fmt):
    r = base(make_repo, object_format=fmt)
    assert changed(r) == ({}, False)
    (r.path / "a.txt").touch()  # stat changed, bytes did not: hashed, still clean
    os.utime(r.path / "a.txt", (1, 1))
    assert changed(r) == ({}, False)
    r.write("a.txt", "ONE\n")  # same size, other bytes
    (r.path / "d/e/b.txt").unlink()
    (r.path / "x.sh").chmod(0o644)
    r.write("new.txt", "n\n")
    r.write("debug.log", "ignored\n")
    r.write("keep.log", "re-included\n")
    r.write("build/out/o.bin", "ignored dir\n")
    os.unlink(r.path / "link")
    os.symlink("d", r.path / "link")
    got, more = changed(r)
    # host mode applies the repository's own ignore rules (#583 round 3, decision B; orch.gitread.ignorepolicy), with
    # the repository's core.ignorecase: the ignored files do not count, as in git
    assert (
        got
        == {
            "a.txt": "modified",
            "d/e/b.txt": "deleted",
            "x.sh": "modified",
            "new.txt": "untracked",
            "keep.log": "untracked",
            "link": "modified",
        }
        and not more
    )
    assert set(got) == git_status(r)


def test_staged_changes_count_even_when_the_worktree_matches_the_index(make_repo):
    r = base(make_repo)
    r.write("a.txt", "staged\n")
    r.write("s.txt", "staged new\n")
    r.git("add", "a.txt", "s.txt")
    r.git("rm", "-q", "--cached", "d/e/b.txt")
    got, _ = changed(r)
    assert got == {"a.txt": "modified", "s.txt": "modified", "d/e/b.txt": "deleted"}


def test_flags_unmerged_and_intent_to_add_are_changes(make_repo):
    r = base(make_repo)
    r.git("update-index", "--skip-worktree", "a.txt")
    r.git("update-index", "--assume-unchanged", "x.sh")
    r.write("ita.txt", "x\n")
    r.git("add", "-N", "ita.txt")
    got, _ = changed(r)
    assert got == {"a.txt": "flag", "x.sh": "flag", "ita.txt": "modified"}


def test_a_merge_conflict_is_a_change(make_repo):
    r = base(make_repo)
    r.git("checkout", "-q", "-b", "other")
    r.write("a.txt", "theirs\n")
    r.commit()
    r.git("checkout", "-q", "main")
    r.write("a.txt", "ours\n")
    r.commit()
    r.git("merge", "-q", "other", check=False)
    assert changed(r)[0].get("a.txt") == "modified"


def test_a_fifo_at_a_tracked_path_is_a_type_change_and_never_blocks(make_repo):
    r = base(make_repo)
    (r.path / "a.txt").unlink()
    os.mkfifo(r.path / "a.txt")  # opening it for reading would block: it is never opened
    os.mkfifo(r.path / "untracked-fifo")
    got, _ = changed(r)
    assert got == {"a.txt": "type", "untracked-fifo": "untracked"}


def test_ignore_rules_follow_gitignore(make_repo):
    """Matching as git does it, core.ignorecase false (case folding: test_ignore_casefold.py)."""
    r = make_repo()
    case_sensitive(r)
    r.write("t.txt", "t\n")
    r.write(".gitignore", "/root-only\n*.tmp\ndocs/**/gen\n**/cache/\n\\#hash\n trailing  \n")
    r.write("sub/.gitignore", "local\n!*.tmp\n")
    r.commit()
    (r.gitdir / "info").mkdir(exist_ok=True)
    (r.gitdir / "info" / "exclude").write_text("excluded\n")
    for p in (
        "root-only",
        "sub/root-only",
        "x.tmp",
        "sub/y.tmp",
        "docs/a/b/gen",
        "docs/gen",
        "z/cache/f",
        "cache",
        "#hash",
        "sub/local",
        "local",
        "excluded",
        "sub/excluded",
        "[x",
    ):
        r.write(p, "x\n")
    import base64

    from orch.gitread.local import READER

    got = {base64.b64decode(c["path_b64"]).decode() for c in READER.tree_check(r.path, r.env)["changed"]}
    assert set(got) == git_status(r)
    assert {"sub/root-only", "sub/y.tmp", "cache", "local", "[x"} <= set(got)
    assert not {"root-only", "x.tmp", "docs/a/b/gen", "docs/gen", "z/cache/f", "#hash", "excluded"} & set(got)


def test_a_nested_repository_is_one_untracked_directory(make_repo):
    r = base(make_repo)
    inner = make_repo("repo/inner")
    inner.write("f", "x\n")
    inner.commit()
    assert changed(r)[0] == {"inner/": "untracked"}


def test_index_versions_3_and_4_and_a_linked_worktree(make_repo):
    r = base(make_repo)
    r.git("update-index", "--index-version", "4")
    assert changed(r) == ({}, False)
    r.write("a.txt", "v4\n")
    assert changed(r)[0] == {"a.txt": "modified"}
    r.git("checkout", "-q", "--", "a.txt")
    r.git("update-index", "--index-version", "3")
    assert changed(r) == ({}, False)
    wt = r.path.parent / "wt"
    r.git("worktree", "add", "-q", str(wt), "-b", "side")
    r.write("../wt/new", "n\n")
    got = local.tree_check(wt, roots=[str(r.path), str(wt)])
    assert [c["path"] for c in got["changed"]] == ["new"]
    assert changed(r) == ({}, False)


def test_more_than_200_changes_are_cut_with_more(make_repo):
    r = base(make_repo)
    for i in range(250):
        r.write(f"u/{i:03}", "x\n")
    got = local.tree_check(r.path)
    assert len(got["changed"]) == 200 and got["more"] is True


# --- escaping (N3, F1 11.3) -----------------------------------------------------------------------------------------


def test_paths_are_escaped_for_display(make_repo):
    r = base(make_repo)
    names = [b"bidi\xe2\x80\xaeexe.txt", b"esc\x1b[31m", b"nl\nx", b"bad\xff", b"tab\tx", "⟨U+0041⟩".encode()]
    want = {
        "bidi⟨U+202E⟩exe.txt",
        "esc⟨U+001B⟩[31m",
        "nl⟨U+000A⟩x",
        "bad⟨0xFF⟩",
        "tab⟨U+0009⟩x",
        "⟨U+27E8⟩U+0041⟩",  # a literal marker cannot be forged: its "⟨" is escaped
    }
    for n in names:
        try:
            (r.path / os.fsdecode(n)).write_bytes(b"x")
        except OSError:  # APFS refuses a name that is not UTF-8 (EILSEQ); escape_path is tested on its own below
            want.discard("bad⟨0xFF⟩")
    got = {c["path"] for c in local.tree_check(r.path)["changed"]}
    assert got == want
    for p in got:
        assert not any(ord(ch) < 0x20 or ord(ch) == 0x7F or 0x202A <= ord(ch) <= 0x202E for ch in p)


def test_escape_path_rules():
    assert treejob.escape_path(b"plain/path.txt") == "plain/path.txt"
    assert treejob.escape_path("café".encode()) == "café"
    assert treejob.escape_path(b"a\x00b\x7f\xc2\x85") == "a⟨U+0000⟩b⟨U+007F⟩⟨U+0085⟩"
    assert treejob.escape_path(b"\xe2\x81\xa6x") == "⟨U+2066⟩x"
    assert treejob.escape_path(b"x" * 5000).endswith("…") and len(treejob.escape_path(b"x" * 5000)) == 4096
    assert treejob.escape_path(b"short", cut=True) == "short…"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"changed": [], "more": "no"},
        {"changed": [{"path_b64": "eA==", "cut": False, "why": "pwned"}], "more": False},
        {"changed": [{"path_b64": "!!", "cut": False, "why": "modified"}], "more": False},
        {"changed": [{"path_b64": "", "cut": False, "why": "modified"}], "more": False},
        {"changed": [{"path_b64": "eA==", "cut": False, "why": "modified", "x": 1}], "more": False},
        {"changed": [{"path_b64": "eA==", "cut": False, "why": "modified"}] * 201, "more": True},
        {"changed": [], "more": True},
        {"changed": [], "more": False, "head": None, "head_ref": "HEAD"},
        {"changed": [], "more": False, "head": "zz", "head_ref": "refs/heads/main"},
    ],
)
def test_a_malformed_child_answer_is_unknown(raw):
    with pytest.raises(Unavailable):
        treejob.result_of(raw)


# --- unknown: fail closed -------------------------------------------------------------------------------------------


def test_a_detached_head_is_unknown(make_repo):
    r = base(make_repo)
    r.git("checkout", "-q", "--detach")
    with pytest.raises(Unavailable):
        local.tree_check(r.path)


def test_a_corrupt_or_split_index_is_unknown(make_repo):
    r = base(make_repo)
    idx = r.gitdir / "index"
    data = bytearray(idx.read_bytes())
    data[40] ^= 1
    idx.write_bytes(bytes(data))
    with pytest.raises(Unavailable, match="checksum"):
        local.tree_check(r.path)
    r.git("read-tree", "HEAD")
    assert changed(r)[0] == {}
    r.git("update-index", "--split-index")
    with pytest.raises(Unavailable, match="link"):
        local.tree_check(r.path)


def test_a_populated_submodule_is_unknown(make_repo):
    sub = make_repo("sub")
    sub.write("f", "x\n")
    sub.commit()
    r = base(make_repo)
    r.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub.path), "mod")
    r.commit()
    with pytest.raises(Unavailable, match="submodule"):
        local.tree_check(r.path)


def test_tier_b_has_no_tree_check_and_the_capability_answers_unknown(make_repo):
    from orch.ops.caps import Capabilities

    r = base(make_repo)
    reader = HostGitReader(ReaderPool(), PinStore(), tree_checks=False)
    reader.sync([r.path])
    with pytest.raises(Unavailable, match="worker"):
        reader.tree_check(r.path, "proj")
    caps = Capabilities.host((str(r.path),), git=reader).with_principal("p")
    assert caps.git.dirty(r.path, "proj") is None
    unpinned = HostGitReader(ReaderPool(), PinStore(), tree_checks=True)
    caps = Capabilities.host((str(r.path),), git=unpinned).with_principal("p")
    assert caps.git.dirty(r.path, "proj") is None  # no pin: unknown, never clean


# --- limits ---------------------------------------------------------------------------------------------------------


def test_limits_make_the_answer_unknown(make_repo, monkeypatch):
    r = base(make_repo)
    for i in range(30):
        r.write(f"t/{i}", "x\n")
    r.commit()
    monkeypatch.setattr(limits, "WALK_ENTRIES", 20)
    with pytest.raises(Unavailable, match="more than 20 entries"):
        local.tree_check(r.path)
    monkeypatch.setattr(limits, "WALK_ENTRIES", 200_000)
    monkeypatch.setattr(limits, "INDEX_ENTRIES", 10)
    with pytest.raises(Unavailable, match="more than 10 entries"):
        local.tree_check(r.path)
    monkeypatch.setattr(limits, "INDEX_ENTRIES", 1_000_000)
    monkeypatch.setattr(limits, "INDEX_MAX", 100)
    with pytest.raises(Unavailable, match="larger than"):
        local.tree_check(r.path)


def test_a_tree_deeper_than_the_walk_allows_is_unknown(make_repo):
    r = base(make_repo)
    r.write("/".join(["d"] * (treecheck.MAX_DEPTH + 2)) + "/f", "x\n")
    with pytest.raises(Unavailable, match="deeper"):
        local.tree_check(r.path)


@pytest.mark.slow
def test_a_huge_worktree_stays_within_the_childs_limits(make_repo):
    """10 000 tracked files and 5 000 untracked ones, through the real child: an answer within the 30 s budget."""
    r = make_repo()
    for i in range(100):
        d = r.path / f"d{i:02}"
        d.mkdir()
        for j in range(100):
            (d / f"f{j:02}").write_bytes(b"x\n")
    r.commit()
    for i in range(5000):
        (r.path / f"d{i % 100:02}" / f"u{i}").write_bytes(b"u")
    reader = HostGitReader(ReaderPool(), PinStore(), tree_checks=True)
    reader.sync([r.path])
    got = reader.tree_check(r.path, "big", principal="p")
    assert len(got["changed"]) == 200 and got["more"] is True


# --- F2: nothing in the repository makes it spawn a process ---------------------------------------------------------


def _booby_trap(r, tmp):
    """filter.*.clean, core.fsmonitor and core.hooksPath that each leave a marker file if anything runs them."""
    marks = {k: tmp / f"ran-{k}" for k in ("clean", "smudge", "fsmonitor", "hook", "textconv")}
    hooks = tmp / "hooks"
    hooks.mkdir()
    for name in ("post-index-change", "pre-commit", "reference-transaction"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch {marks['hook']}\n")
        (hooks / name).chmod(0o755)
    r.write(".gitattributes", "* filter=trap diff=trap\n")
    r.commit()  # committed before the trap is armed: the blobs are the raw bytes
    r.git("config", "filter.trap.clean", f"sh -c 'touch {marks['clean']}; cat'")
    r.git("config", "filter.trap.smudge", f"sh -c 'touch {marks['smudge']}; cat'")
    r.git("config", "filter.trap.required", "true")
    r.git("config", "diff.trap.textconv", f"sh -c 'touch {marks['textconv']}; cat'")
    r.git("config", "core.fsmonitor", f"{tmp}/fsmonitor.sh")
    (tmp / "fsmonitor.sh").write_text(f"#!/bin/sh\ntouch {marks['fsmonitor']}\n")
    (tmp / "fsmonitor.sh").chmod(0o755)
    r.git("config", "core.hooksPath", str(hooks))
    os.utime(r.path / "a.txt", (2, 2))  # the stat differs: the check has to hash the file (where a filter would run)
    return marks


def test_f2_a_booby_trapped_repo_spawns_nothing_in_the_real_child(make_repo, tmp_path):
    r = base(make_repo)
    marks = _booby_trap(r, tmp_path.resolve())
    from orch.host.launcher import spawn_gitread

    spawned = []

    def spawn():
        spawned.append(1)
        return spawn_gitread()

    reader = HostGitReader(ReaderPool(spawn=spawn), PinStore(), tree_checks=True)
    reader.sync([r.path])
    before = len(spawned)
    got = reader.tree_check(r.path, "proj", principal="p")
    assert (got["changed"], got["more"], got["head_ref"]) == ([], False, "refs/heads/main")
    assert got["head"] == r.sha("HEAD")
    r.write("a.txt", "dirty\n")
    assert reader.tree_check(r.path, "proj", principal="p")["changed"] == [{"path": "a.txt", "why": "modified"}]
    assert len(spawned) - before == 2  # one reader child per check, nothing else
    assert not [k for k, m in marks.items() if m.exists()]


AUDIT = textwrap.dedent(
    """
    import json, sys
    events = []
    def hook(event, args):
        if event in ("subprocess.Popen", "os.posix_spawn", "os.system", "os.fork", "os.forkpty") \\
                or event.startswith(("os.exec", "os.spawn")):
            events.append([event, repr(args)[:600]])
    sys.addaudithook(hook)
    from pathlib import Path
    from orch.gitread import local
    from orch.gitread.client import HostGitReader, PinStore, ReaderPool
    repo = sys.argv[1]
    local.tree_check(repo)
    in_process = list(events)
    reader = HostGitReader(ReaderPool(), PinStore(), tree_checks=True)
    reader.sync([repo])
    events.clear()
    reader.tree_check(repo, "proj", principal="p")
    print(json.dumps({"in_process": in_process, "host": events}))
    """
)


def test_f2_no_exec_beyond_the_reader(make_repo, tmp_path):
    """An audit hook in the calling process: the in-process check spawns nothing, the host-side check exactly one
    reader child (the fixed argv); the child's own hook refuses any spawn (test_gitread_profiles)."""
    r = base(make_repo)
    marks = _booby_trap(r, tmp_path.resolve())
    done = subprocess.run([sys.executable, "-c", AUDIT, str(r.path)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got["in_process"] == []
    assert len(got["host"]) == 1 and got["host"][0][0] == "subprocess.Popen"
    assert "'-I', '-m', 'orch.gitread'" in got["host"][0][1]
    assert not [k for k, m in marks.items() if m.exists()]


def test_the_raw_bytes_rule_is_conservative_where_a_filter_would_hide_a_change(make_repo):
    """git with a clean filter that maps both contents to the same blob says clean; orch compares raw bytes."""
    r = base(make_repo)
    r.write(".gitattributes", "a.txt filter=upper\n")
    r.commit()
    r.git("config", "filter.upper.clean", "tr a-z A-Z")
    r.write("a.txt", "ONE\n")
    r.git("add", "a.txt")  # stores "ONE\n" through the filter
    r.commit()
    r.write("a.txt", "one\n")  # git: clean (the filter maps it to the stored blob); orch: modified
    assert changed(r)[0] == {"a.txt": "modified"}


@pytest.mark.skipif(git_version() < (2, 37), reason="git without fsmonitor settings")
def test_index_extensions_written_by_fsmonitor_and_untracked_cache_are_ignored(make_repo):
    r = base(make_repo)
    r.git("config", "core.untrackedCache", "true")
    r.git("update-index", "--untracked-cache")
    r.git("status", "--porcelain")
    assert b"UNTR" in (r.gitdir / "index").read_bytes()
    r.write("u", "x\n")
    assert changed(r)[0] == {"u": "untracked"}  # the cache is never trusted


# --- integrity: every way a dirty tree could pass for clean (scan review) ------------------------------------------


def _forge_entry(r, rel: str, **fields) -> None:
    """Rewrite one v2 index entry's stat fields (the index is agent-writable) and re-seal the checksum."""
    import hashlib
    import struct

    idx = r.gitdir / "index"
    data = bytearray(idx.read_bytes()[:-20])
    assert data[:4] == b"DIRC" and struct.unpack_from(">I", data, 4)[0] == 2
    i = 12
    names = ("ctime_s", "ctime_ns", "mtime_s", "mtime_ns", "dev", "ino", "mode", "uid", "gid", "size")
    while True:
        (flags,) = struct.unpack_from(">H", data, i + 60)
        nlen = flags & 0xFFF
        name = bytes(data[i + 62 : i + 62 + nlen])
        if name == rel.encode():
            vals = list(struct.unpack_from(">10I", data, i))
            for k, v in fields.items():
                vals[names.index(k)] = v & 0xFFFFFFFF
            struct.pack_into(">10I", data, i, *vals)
            break
        i += (62 + nlen + 8) & ~7
    idx.write_bytes(bytes(data) + hashlib.sha1(bytes(data)).digest())


def test_racy_git_a_same_second_same_size_rewrite_is_hashed_not_trusted(make_repo):
    """The index's stat data match the file to the second (nanoseconds zero, as an index written without USE_NSEC
    stores them), size equal, content changed in the second the index was written: hashed, so modified."""
    r = base(make_repo)
    p = r.path / "a.txt"
    p.write_text("ONE\n")  # same size as "one\n"
    os.utime(p, ns=(1_800_000_000 * 10**9, 1_800_000_000 * 10**9))
    st = p.lstat()
    _forge_entry(
        r,
        "a.txt",
        mtime_s=st.st_mtime_ns // 10**9,
        mtime_ns=0,
        ctime_s=st.st_ctime_ns // 10**9,
        ctime_ns=0,
        size=st.st_size,
        ino=st.st_ino,
    )
    # the index was written in the file's own second, later in it than the file: git's (s, ns) rule would trust it
    os.utime(r.gitdir / "index", ns=(st.st_mtime_ns + 500_000_000, st.st_mtime_ns + 500_000_000))
    assert changed(r)[0] == {"a.txt": "modified"}
    p.write_text("one\n")  # the committed bytes again: hashing says clean
    assert changed(r)[0] == {}


def test_a_forged_stat_on_an_old_index_is_hashed_when_the_file_changed_after_it(make_repo):
    """The file's own ctime is after the index was written: its stat data are not trusted, whatever the entry says."""
    r = base(make_repo)
    old = 1_700_000_000 * 10**9
    os.utime(r.gitdir / "index", ns=(old, old))
    p = r.path / "a.txt"
    p.write_text("ONE\n")
    st = p.lstat()
    _forge_entry(r, "a.txt", mtime_s=st.st_mtime_ns // 10**9, mtime_ns=st.st_mtime_ns % 10**9,
                 ctime_s=st.st_ctime_ns // 10**9, ctime_ns=st.st_ctime_ns % 10**9, ino=st.st_ino)  # fmt: skip
    os.utime(r.gitdir / "index", ns=(old, old))
    assert changed(r)[0] == {"a.txt": "modified"}


def test_skip_worktree_and_assume_unchanged_cannot_hide_an_edit(make_repo):
    r = base(make_repo)
    r.git("update-index", "--skip-worktree", "a.txt")
    r.git("update-index", "--assume-unchanged", "x.sh")
    r.write("a.txt", "hidden edit\n")
    r.write("x.sh", "#!/bin/sh\necho hidden\n", mode=0o755)
    assert git_status(r) == set()  # git itself is fooled
    got, _ = changed(r)
    assert got == {"a.txt": "flag", "x.sh": "flag"}  # orch is not: dirty
    from orch.gitread.treejob import render

    assert render(local.tree_check(r.path))  # a non-empty answer is dirty for Git.dirty()


def test_a_tracked_file_matching_an_ignore_pattern_is_still_compared(make_repo):
    r = base(make_repo)
    r.write("build/tracked.txt", "t\n")
    r.write("traced.log", "t\n")
    r.git("add", "-f", "build/tracked.txt", "traced.log")
    r.commit()
    assert changed(r)[0] == {}
    r.write("build/tracked.txt", "edited\n")
    r.write("traced.log", "edited\n")
    r.write("build/untracked.txt", "ignored\n")
    # the ignored untracked file does not count (as in git), the tracked ones compare whatever the rules say
    want = {"build/tracked.txt": "modified", "traced.log": "modified"}
    assert changed(r)[0] == want


def test_sparse_checkout_is_dirty_and_a_sparse_index_unknown(make_repo):
    r = base(make_repo)
    r.git("config", "core.sparseCheckout", "true")  # the old way: no extensions.worktreeConfig
    (r.gitdir / "info").mkdir(exist_ok=True)
    (r.gitdir / "info" / "sparse-checkout").write_text("/a.txt\n")
    r.git("read-tree", "-mu", "HEAD")
    assert not (r.path / "x.sh").exists()
    got, _ = changed(r)
    assert got and set(got.values()) == {"flag"}  # every entry outside it carries skip-worktree: dirty
    r.git("sparse-checkout", "set", "--cone", "--sparse-index", "d")  # writes extensions.worktreeConfig and sdir
    with pytest.raises(Unavailable):
        local.tree_check(r.path)


def test_a_case_only_rename_on_a_case_insensitive_fs_is_dirty(make_repo):
    r = base(make_repo)
    # on a case-insensitive FS (macOS) the two-step rename is git's own; on Linux it is an ordinary rename, which the
    # same answer covers, so this runs on CI too
    os.rename(r.path / "a.txt", r.path / "tmp-name")
    os.rename(r.path / "tmp-name", r.path / "A.txt")
    got, _ = changed(r)
    assert got == {"A.txt": "untracked", "a.txt": "deleted"}


def test_a_submodule_whose_directory_vanished_is_deleted_and_a_staged_gitlink_change_is_seen(make_repo):
    sub = make_repo("sub")
    sub.write("f", "x\n")
    first = sub.commit()
    r = base(make_repo)
    (r.path / "mod").mkdir()
    r.git("update-index", "--add", "--cacheinfo", f"160000,{first},mod")
    r.git("commit", "-q", "-m", "gitlink")
    assert changed(r)[0] == {}  # not checked out: clean, as git sees it
    (r.path / "mod").rmdir()
    assert changed(r)[0] == {"mod": "deleted"}
    (r.path / "mod").mkdir()
    sub.write("f", "y\n")
    second = sub.commit()
    r.git("update-index", "--cacheinfo", f"160000,{second},mod")
    assert changed(r)[0] == {"mod": "modified"}


def test_the_result_names_the_head_it_is_relative_to(make_repo):
    r = base(make_repo)
    got = local.tree_check(r.path)
    assert got["head"] == r.sha("HEAD") and got["head_ref"] == "refs/heads/main"
    r.git("checkout", "-q", "-b", "other")
    assert local.tree_check(r.path)["head_ref"] == "refs/heads/other"
    unborn = make_repo("unborn")
    unborn.write("f", "x\n")
    got = local.tree_check(unborn.path)
    assert got["head"] is None and got["changed"] == [{"path": "f", "why": "untracked"}]


def test_a_tracked_symlink_replaced_by_a_regular_file_is_a_type_change(make_repo):
    r = base(make_repo)
    os.unlink(r.path / "link")
    (r.path / "link").write_text("a.txt")  # the same bytes as the link target string: still another type
    assert changed(r)[0] == {"link": "type"}


def test_a_sparse_directory_entry_is_unknown(make_repo):
    """A mode-040000 entry (a sparse index's directory) is refused even without its sdir extension."""
    import hashlib
    import struct

    r = base(make_repo)
    idx = r.gitdir / "index"
    data = bytearray(idx.read_bytes()[:-20])
    struct.pack_into(">I", data, 12 + 24, 0o040000)  # the first entry's mode
    idx.write_bytes(bytes(data) + hashlib.sha1(bytes(data)).digest())
    with pytest.raises(Unavailable, match="mode 40000"):
        local.tree_check(r.path)


def test_a_repo_name_over_64_characters_is_refused_not_cut(make_repo):
    from orch.ops.caps import Capabilities

    r = base(make_repo)
    reader = HostGitReader(ReaderPool(), PinStore(), tree_checks=True)
    reader.sync([r.path])
    caps = Capabilities.host((str(r.path),), git=reader).with_principal("p")
    assert caps.git.tree_check(r.path, "n" * 64) is not None
    assert caps.git.tree_check(r.path, "n" * 65) is None  # unknown: fails closed


@pytest.mark.slow
def test_a_clean_10k_file_tree_through_the_real_child_within_its_budget(make_repo):
    """The clean path, all of it: 10 000 tracked files compared (stat shortcut, the index written well after them)
    and then 1 000 of them modified (hashed), through the real child, well inside the 30 s wall and 10 s CPU."""
    import time

    r = make_repo()
    for i in range(100):
        d = r.path / f"d{i:02}"
        d.mkdir()
        for j in range(100):
            (d / f"f{j:02}").write_bytes(b"x\n")
    old = time.time() - 10
    for p in r.path.rglob("f*"):
        os.utime(p, (old, old))  # outside the racy second of the index written next
    r.commit()
    reader = HostGitReader(ReaderPool(), PinStore(), tree_checks=True)
    reader.sync([r.path])
    t0 = time.monotonic()
    got = reader.tree_check(r.path, "big", principal="p")
    clean_s = time.monotonic() - t0
    assert got["changed"] == [] and got["more"] is False
    for i in range(10):
        for j in range(100):
            (r.path / f"d{i:02}" / f"f{j:02}").write_bytes(b"y\n")
    t0 = time.monotonic()
    got = reader.tree_check(r.path, "big", principal="p")
    dirty_s = time.monotonic() - t0
    assert len(got["changed"]) == 200 and got["more"] is True
    assert clean_s < 10 and dirty_s < 10, (clean_s, dirty_s)


def test_pin_store_writes_the_file_before_memory(tmp_path, make_repo):
    from orch.gitread.pinning import pin_repo

    r = base(make_repo)
    pins_dir = tmp_path / "state"
    pins_dir.mkdir()
    store = PinStore(pins_dir / "git-pins.json")
    (pins_dir / "git-pins.json.tmp").write_text("leftover")
    os.chmod(pins_dir / "git-pins.json.tmp", 0o666)
    store.update({str(r.path): pin_repo(str(r.path), [str(r.path)])}, [])
    assert oct((pins_dir / "git-pins.json").stat().st_mode & 0o777) == "0o600"
    assert not (pins_dir / "git-pins.json.tmp").exists()
    os.chmod(pins_dir, 0o500)  # the file cannot be written: memory does not change either
    try:
        with pytest.raises(OSError):
            store.update({}, [str(r.path)])
        assert store.get(str(r.path)) is not None
    finally:
        os.chmod(pins_dir, 0o700)

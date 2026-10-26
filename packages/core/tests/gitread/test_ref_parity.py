"""#583: gitread's ref parsing against git's, case by case. The rule: where the reader answers, its answer is git's
(plain git, clean config); where it cannot be sure it reads what git reads, it refuses (``Unavailable``: unknown).
It never gives a different answer. Two differentials were closed (see ``orch.gitread.refs``):

1. a ``packed-refs`` whose header says ``sorted`` but is not: git binary-searches it and can miss an entry the reader
   would find; the reader now refuses such a file;
2. a name that differs from a loose ref only in case or Unicode normalisation: on a case-insensitive or normalising
   file system git opens that file under the other name; the reader said "no such ref", it now refuses.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from orch.gitread.errors import Unavailable
from orch.gitread.local import READER

from .conftest import GIT

REFUSED = "refused"


def git_commit(r, name: str) -> str | None:
    done = subprocess.run([GIT, "-C", str(r.path), "rev-parse", "--verify", "-q", "--end-of-options",
                           f"{name}^{{commit}}"], capture_output=True, env=r.env, check=False)  # fmt: skip
    return done.stdout.decode().strip() if done.returncode == 0 else None


def mine(r, name: str):
    try:
        return READER.commit(r.path, name)
    except Unavailable:
        return REFUSED


def same_or_refused(r, name: str):
    """The reader's answer for ``name``: git's, or a refusal; never another one."""
    got, want = mine(r, name), git_commit(r, name)
    assert got in (want, REFUSED), (name, got, want)
    return got


def repo(make_repo, object_format: str = "sha1"):
    r = make_repo(object_format=object_format)
    r.write("a", "1\n")
    first = r.commit("1")
    r.write("a", "2\n")
    second = r.commit("2")
    return r, first, second


def put(r, rel: str, data: bytes) -> None:
    p = r.gitdir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


# -- loose refs


@pytest.mark.parametrize(
    "shape",
    ["plain", "crlf", "no_newline", "trailing_space", "extra_line", "uppercase", "short", "long"],
)
def test_a_loose_ref_shape_is_read_as_git_reads_it_or_refused(make_repo, shape):
    r, first, _ = repo(make_repo)
    data = {
        "plain": first + "\n",
        "crlf": first + "\r\n",
        "no_newline": first,
        "trailing_space": first + " \n",
        "extra_line": first + "\nsomething else\n",
        "uppercase": first.upper() + "\n",
        "short": first[:39] + "\n",
        "long": first + "0\n",
    }[shape]
    put(r, "refs/heads/x", data.encode())
    got = same_or_refused(r, "refs/heads/x")
    assert got == (first if shape == "plain" else REFUSED)  # only git's own exact form is read


def test_a_sha1_length_ref_in_a_sha256_repository_is_refused(make_repo):
    r, first, _ = repo(make_repo, "sha256")
    assert len(first) == 64
    put(r, "refs/heads/x", (first[:40] + "\n").encode())
    assert same_or_refused(r, "refs/heads/x") == REFUSED
    put(r, "refs/heads/x", (first + "\n").encode())
    assert same_or_refused(r, "refs/heads/x") == first


@pytest.mark.parametrize("content", ["ref: refs/heads/main\n", "ref:refs/heads/main\n", "ref:  refs/heads/main\n",
                                     "ref: refs/heads/y\n"])  # fmt: skip
def test_a_loose_symref_is_refused_however_it_is_spaced_or_nested(make_repo, content):
    r, _, _ = repo(make_repo)
    put(r, "refs/heads/y", b"ref: refs/heads/main\n")
    put(r, "refs/heads/x", content.encode())
    assert git_commit(r, "refs/heads/x") is not None  # positive control: git follows it
    assert mine(r, "refs/heads/x") == REFUSED


def test_a_loose_ref_overrides_a_packed_one_as_in_git(make_repo):
    r, first, second = repo(make_repo)
    r.git("pack-refs", "--all")
    put(r, "refs/heads/main", (first + "\n").encode())  # loose now disagrees with packed
    assert same_or_refused(r, "refs/heads/main") == first == git_commit(r, "refs/heads/main")
    os.remove(r.gitdir / "refs/heads/main")
    assert same_or_refused(r, "refs/heads/main") == second


# -- packed-refs


def packed(r, body: bytes) -> None:
    for p in (r.gitdir / "refs" / "heads").iterdir():
        p.unlink()
    put(r, "packed-refs", body)


@pytest.mark.parametrize("header", [b"", b"# pack-refs with: peeled\n", b"# pack-refs with: peeled fully-peeled\n",
                                    b"# pack-refs with: peeled fully-peeled sorted \n"])  # fmt: skip
def test_packed_refs_headers_and_peeled_lines(make_repo, header):
    r, first, second = repo(make_repo)
    r.git("tag", "-a", "-m", "t", "v1", first)
    tag = r.sha("refs/tags/v1")
    body = (f"{second} refs/heads/main\n{first} refs/heads/old\n{tag} refs/tags/v1\n^{first}\n").encode()
    packed(r, header + body)  # in name order, as git writes it
    for name, want in (("refs/heads/main", second), ("refs/heads/old", first), ("refs/tags/v1", first)):
        assert same_or_refused(r, name) == want


def test_an_unsorted_file_that_says_sorted_is_refused_and_without_the_trait_is_read(make_repo):
    """Differential 1: git binary-searches a ``sorted`` file and can miss an entry out of order."""
    r, first, second = repo(make_repo)
    rows = b"".join(f"{sha} refs/heads/{n}\n".encode() for n, sha in
                    (("zz", second), ("aa", first), ("mm", first), ("main", second)))  # fmt: skip
    packed(r, b"# pack-refs with: peeled fully-peeled sorted \n" + rows)
    for name in ("refs/heads/aa", "refs/heads/zz", "refs/heads/main"):
        assert same_or_refused(r, name) == REFUSED
    packed(r, b"# pack-refs with: peeled fully-peeled \n" + rows)  # no claim: git sorts it itself
    assert same_or_refused(r, "refs/heads/aa") == first and same_or_refused(r, "refs/heads/zz") == second


@pytest.mark.parametrize(
    "body",
    ["dup", "comment", "crlf", "no_newline", "bad_peel", "peel_first", "double_peel", "uppercase", "two_spaces"],
)
def test_a_malformed_packed_refs_is_refused_never_read_differently(make_repo, body):
    r, first, second = repo(make_repo)
    rows = {
        "dup": f"{first} refs/heads/main\n{second} refs/heads/main\n",
        "comment": f"{second} refs/heads/main\n# a comment\n",
        "crlf": f"{second} refs/heads/main\r\n",
        "no_newline": f"{second} refs/heads/main",
        "bad_peel": f"{second} refs/heads/main\n^xyz\n",
        "peel_first": f"^{first}\n{second} refs/heads/main\n",
        "double_peel": f"{second} refs/heads/main\n^{first}\n^{first}\n",
        "uppercase": f"{second.upper()} refs/heads/main\n",
        "two_spaces": f"{second}  refs/heads/main\n",
    }[body]
    packed(r, rows.encode())
    assert same_or_refused(r, "refs/heads/main") == REFUSED


# -- names


@pytest.mark.parametrize("name", ["refs/heads/x.lock", "refs/heads/a..b", "refs/heads/x.", "refs/heads/a@{1}",
                                  "refs/heads/a\x01b", "refs/heads/a\x7fb", "refs/heads/.x", "refs/heads/a//b",
                                  "refs/heads/a b", "refs/heads/a:b", "refs/heads/a~1", "refs/heads/a^",
                                  "refs/heads/a?", "refs/heads/a*", "refs/heads/a[b", "refs/heads/a\\b",
                                  "@"])  # fmt: skip
def test_a_name_git_refuses_is_refused(make_repo, name):
    r, _, _ = repo(make_repo)
    assert mine(r, name) == REFUSED
    check = subprocess.run([GIT, "check-ref-format", name], capture_output=True, env=r.env, check=False)
    assert check.returncode != 0  # positive control: git's own rules refuse it too


@pytest.mark.parametrize("asked", ["refs/heads/MAIN", "refs/heads/Main", "refs/Heads/main"])
def test_a_name_differing_only_in_case_is_refused_never_no_such_ref(make_repo, asked):
    """Differential 2: on a case-insensitive file system git opens refs/heads/main for refs/heads/MAIN."""
    r, _, second = repo(make_repo)
    assert same_or_refused(r, asked) == REFUSED
    assert same_or_refused(r, "refs/heads/main") == second


def test_a_name_differing_only_in_unicode_normalisation_is_refused(make_repo):
    import unicodedata

    r, _, second = repo(make_repo)
    nfc = "refs/heads/übung"
    r.git("branch", "übung")
    assert same_or_refused(r, nfc) == second
    assert same_or_refused(r, unicodedata.normalize("NFD", nfc)) == REFUSED


# -- HEAD


@pytest.mark.parametrize(
    "content,expect",
    [("ref: refs/heads/main\n", "branch"), ("{sha}\n", "detached"), ("ref:refs/heads/main\n", REFUSED),
     ("ref: refs/heads/main\r\n", REFUSED), ("ref: refs/heads/main", REFUSED), ("ref: refs/heads/main \n", REFUSED),
     ("{SHA}\n", REFUSED), ("{sha}", REFUSED), ("ref: refs/heads/MAIN\n", REFUSED), ("ref: refs/tags/v\n", REFUSED)],
)  # fmt: skip
def test_head_as_a_symref_or_a_detached_id(make_repo, content, expect):
    r, first, second = repo(make_repo)
    put(r, "HEAD", content.replace("{sha}", first).replace("{SHA}", first.upper()).encode())
    git_head = subprocess.run([GIT, "-C", str(r.path), "rev-parse", "-q", "--verify", "HEAD"], capture_output=True,
                              env=r.env, check=False).stdout.decode().strip() or None  # fmt: skip
    try:
        got = READER.head(r.path)
    except Unavailable:
        got = REFUSED
    if expect == REFUSED:
        assert got == REFUSED
    else:
        assert got[1] == git_head and got == (("refs/heads/main", second) if expect == "branch" else (None, first))


def test_head_on_a_branch_git_cannot_find_in_a_falsely_sorted_file_is_unknown(make_repo):
    """Opus L2: HEAD -> main, main only in a packed-refs that says sorted and is not: git calls HEAD unborn."""
    r, first, second = repo(make_repo)
    lines = [f"{first} refs/heads/z{i:03d}" for i in range(100)]
    lines.insert(60, f"{second} refs/heads/main")  # out of order among the z refs (Opus's probe 8b)
    packed(r, b"# pack-refs with: peeled fully-peeled sorted \n" + "".join(x + "\n" for x in lines).encode())
    assert git_commit(r, "HEAD") is None  # positive control: git's binary search misses main
    with pytest.raises(Unavailable, match="sorted"):
        READER.head(r.path)
    with pytest.raises(Unavailable):
        READER.tree_check(r.path, r.env)


def test_the_skipped_remote_head_symref_is_never_answered_and_changes_nothing_else(make_repo):
    """Opus L3: git follows a loose refs/remotes/<r>/HEAD (it wins over packed-refs); the reader never follows it,
    so a lookup of it is unknown, never the packed value or "no such ref". HEAD and refs/heads are unaffected."""
    r, first, second = repo(make_repo)
    put(r, "refs/remotes/origin/main", (second + "\n").encode())
    put(r, "packed-refs", f"{first} refs/remotes/origin/HEAD\n".encode())
    put(r, "refs/remotes/origin/HEAD", b"ref: refs/remotes/origin/main\n")
    assert git_commit(r, "refs/remotes/origin/HEAD") == second  # git follows the loose symref
    assert mine(r, "refs/remotes/origin/HEAD") == REFUSED
    assert mine(r, "refs/remotes/origin/head") == REFUSED  # nor by another case
    assert READER.head(r.path) == ("refs/heads/main", second)
    assert same_or_refused(r, "refs/heads/main") == second
    assert same_or_refused(r, "refs/remotes/origin/main") == second


#: Opus delta L2 on #583 (p583d2/apfsfold.out): 22 (stored, looked-up) code point pairs APFS opens as one file, which
#: NFC + casefold kept apart
APFS_PAIRS = [
    (0x0390, (938, 769)),
    (0x0390, (921, 776, 769)),
    (0x03B0, (939, 769)),
    (0x03B0, (933, 776, 769)),
    (0x1FB7, (913, 834, 837)),
    (0x1FB7, (8124, 834)),
    (0x1FC7, (8140, 834)),
    (0x1FC7, (919, 834, 837)),
    (0x1FD2, (921, 776, 768)),
    (0x1FD2, (938, 768)),
    (0x1FD3, (938, 769)),
    (0x1FD3, (921, 776, 769)),
    (0x1FD7, (921, 776, 834)),
    (0x1FD7, (938, 834)),
    (0x1FE2, (939, 768)),
    (0x1FE2, (933, 776, 768)),
    (0x1FE3, (939, 769)),
    (0x1FE3, (933, 776, 769)),
    (0x1FE7, (933, 776, 834)),
    (0x1FE7, (939, 834)),
    (0x1FF7, (8188, 834)),
    (0x1FF7, (937, 834, 837)),
]


@pytest.mark.parametrize(
    "stored,asked", APFS_PAIRS, ids=[f"{s:04x}" + "-".join(f"{c:04x}" for c in a) for s, a in APFS_PAIRS]
)
def test_a_ref_lookup_the_file_system_folds_is_unknown_never_another_answer(make_repo, stored, asked):
    """Round 3, L2: only a byte-exact match against the refs directory is an answer. On a folding file system the
    file system decides what git opens; anything it adds is unknown."""
    r, _, second = repo(make_repo)
    name = "refs/heads/x" + chr(stored)
    put(r, name, (second + "\n").encode())
    asked_name = "refs/heads/x" + "".join(chr(c) for c in asked)
    got = same_or_refused(r, asked_name)
    if git_commit(r, asked_name) is not None:  # the file system opened the loose file under the other name
        assert got == REFUSED


def test_head_through_an_apfs_folded_name_is_unknown_never_the_packed_commit(make_repo):
    """Opus delta probe 5b: packed refs/heads/U+0390 = A, loose refs/heads/U+03AA U+0301 = B, HEAD -> U+0390. git
    opens the loose file on APFS and says B; the reader must never say A."""
    r, first, second = repo(make_repo)
    n1, n2 = "\u0390", "\u03aa\u0301"
    put(
        r,
        "packed-refs",
        f"# pack-refs with: peeled fully-peeled sorted \n{first} refs/heads/main\n{first} refs/heads/{n1}\n".encode(),
    )
    os.remove(r.gitdir / "refs" / "heads" / "main")
    put(r, f"refs/heads/{n2}", (second + "\n").encode())
    put(r, "HEAD", f"ref: refs/heads/{n1}\n".encode())
    git_head = git_commit(r, "HEAD")
    try:
        got = READER.head(r.path)[1]
    except Unavailable:
        got = REFUSED
    assert got in (git_head, REFUSED), (got, git_head)  # never the packed A where git says B

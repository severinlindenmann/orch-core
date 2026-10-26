"""#583 round 3, owner decision B (19 Oct 2026): ignored files are ignored exactly as git ignores them on a
case-insensitive file system (macOS) too, so ``node_modules`` and the like never make a clean check fail.

git matches ignore patterns case-insensitively (``WM_CASEFOLD``, quirks and all) exactly when ``core.ignorecase`` is
true, which ``git init`` writes on macOS. The reader reads the value as git does (the repository's config over the
person's, default false) and, where it cannot tell, matches in both modes and ignores a path only if both do (never
cleaner than git). These tests compare the reader with plain git, with ``core.ignorecase`` true and false, for the
cases the Opus delta found (negations and casefold quirks that gave a false clean), nested ignores, the places the
value can come from, host mode, and a seeded differential fuzz of the matcher itself against ``git check-ignore``.
"""

from __future__ import annotations

import base64
import os
import random
import subprocess
from pathlib import Path

import pytest

from orch.gitread import wildmatch
from orch.gitread.ignorepolicy import Ignores, _parse
from orch.gitread.local import READER

from .conftest import GIT, untracked_by_git


def _mine(r, env=None) -> set[str]:
    got = READER.tree_check(r.path, env if env is not None else r.env)
    assert not got["more"]
    return {base64.b64decode(c["path_b64"]).decode() for c in got["changed"]}


def _host(r) -> set[str]:
    """Host mode's reader (no person configuration): ``orch.gitread.local.tree_check``, as the child answers it."""
    return {base64.b64decode(c["path_b64"]).decode() for c in _host_raw(r)["changed"]}


def _host_raw(r) -> dict:
    from orch.gitread import treecheck
    from orch.gitread.fs import open_pin
    from orch.gitread.pinning import pin_repo
    from orch.gitread.repo import Repo

    pins = pin_repo(os.path.realpath(r.path), None)
    with Repo(pins) as repo:
        worktree = open_pin(pins.worktree)
        try:
            return treecheck.check(repo, worktree)
        finally:
            worktree.close()


def _setup(
    make_repo, gitignore: str, files: list[str], ignorecase: str | None, sub: dict[str, str] | None = None, name="repo"
):
    r = make_repo(name)
    r.write("tracked", "t\n")
    r.commit()
    if ignorecase is None:
        r.git("config", "--unset", "core.ignorecase", check=False)
    else:
        r.git("config", "core.ignorecase", ignorecase)
    r.write(".gitignore", gitignore)
    for rel, text in (sub or {}).items():
        r.write(rel, text)
    for f in files:
        r.write(f, "x\n")
    return r


# (name, .gitignore, files); no two file names differ only in case, so a case-insensitive volume holds them all
CASES = [
    ("negation_case", "*.log\n!Keep.log\n", ["keep.log", "other.log"]),
    ("negation_dir", "build/\n!Build/\n", ["build/x", "other/y"]),
    ("negation_all", "*\n!*.TXT\n!.gitignore\n", ["notes.txt", "q.dat"]),
    ("negation_nested", "out/*\n!out/Keep/\n", ["out/keep/a", "out/zap/b"]),
    ("escaped_upper", "a\\A\n", ["aA", "ab"]),
    ("escaped_lower", "a\\a\n", ["aA", "aa2"]),
    ("class_member", "[A]bc\n", ["Abc", "abd"]),
    ("class_member_lower", "[a]bc\n", ["Abc", "abd"]),
    ("class_negated", "f[!a]\n", ["fA", "fb", "fa2"]),
    ("range", "[b-c]x\n[A-B]y\n", ["Bx", "by", "Cx", "dx"]),
    ("range_reversed_case", "[B-b]z\n", ["Bz", "bz2"]),
    ("posix_upper", "[[:upper:]]one\n", ["Aone", "aone2", "1one"]),
    ("posix_lower", "[[:lower:]]two\n", ["Atwo", "atwo2", "1two"]),
    ("literal_prefix", "Docs/Gen/*.tmp\n", ["docs/gen/a.tmp", "docs/gen/b.TMP"]),
    ("star_literal", "*.Tmp\n", ["a.tmp", "b.TMP", "c.tmpx"]),
    ("anchored", "/Top\n", ["top", "sub/top"]),
    ("dirs", "**/Cache/\n", ["cache/a", "x/CACHE/b", "x/cached/c"]),
    ("plain", "node_modules/\n.venv/\n", ["node_modules/p/a.js", "NODE_MODULES/q.js", ".venv/bin/py"]),
]


@pytest.mark.parametrize("ic", ["true", "false"])
@pytest.mark.parametrize("name,gi,files", CASES, ids=[c[0] for c in CASES])
def test_the_reader_ignores_exactly_what_git_ignores_whatever_core_ignorecase_says(make_repo, name, gi, files, ic):
    r = _setup(make_repo, gi, files, ic)
    theirs = untracked_by_git(r)
    assert _mine(r) == theirs, (name, ic)
    assert _host(r) == theirs, (name, ic, "host mode reads the same repository config")


@pytest.mark.parametrize("name,gi,files", CASES, ids=[c[0] for c in CASES])
def test_when_core_ignorecase_is_unset_it_is_false_as_in_git(make_repo, name, gi, files):
    r = _setup(make_repo, gi, files, None)
    theirs = untracked_by_git(r)
    assert _mine(r) == theirs, name  # the person's config is known (empty): git's default, false
    mine_host = _host(r)  # not known in host mode: both modes, ignored only if both ignore it
    assert theirs <= mine_host, (name, sorted(theirs), sorted(mine_host))


def test_the_probes_of_the_opus_delta_are_shown_with_gits_macos_default(make_repo):
    """``*.log`` / ``!Keep.log`` with ``core.ignorecase=true``: git lists keep.log; a case-sensitive match hid it."""
    r = _setup(make_repo, "*.log\n!Keep.log\n", ["keep.log", "other.log"], "true")
    assert "keep.log" in untracked_by_git(r) and "other.log" not in untracked_by_git(r)
    assert _mine(r) == untracked_by_git(r)
    assert READER.tree_check(r.path, r.env)["ignore_rules"] == "applied"


def test_ignored_files_are_ignored_on_a_case_insensitive_volume_by_default(make_repo):
    """The point of decision B: a clean check does not fail on node_modules, ``core.ignorecase=true`` as on macOS."""
    r = _setup(
        make_repo, "node_modules/\n*.pyc\n.venv/\n", ["node_modules/a/b.js", "x.pyc", ".venv/bin/python"], "true"
    )
    r.commit("ignore rules")
    assert untracked_by_git(r) == set() and _mine(r) == set() and _host(r) == set()
    assert READER.dirty(r.path, r.env) == ""


def test_nested_ignore_files_fold_too(make_repo):
    sub = {"d/.gitignore": "*.O\n!keep.o\n", "d/e/.gitignore": "Gen*\n"}
    files = ["d/a.o", "d/keep.o", "d/e/gen1", "d/e/Gen2", "d/e/other"]
    for ic in ("true", "false"):
        r = _setup(make_repo, "", files, ic, sub, "r" + ic)
        assert _mine(r) == untracked_by_git(r), ic
        assert _host(r) == untracked_by_git(r), ic


def test_the_base_of_a_nested_pattern_is_compared_as_git_does(make_repo):
    """git compares the directory part with ``fspathncmp``; the walk hands the reader the on-disk spelling."""
    r = _setup(make_repo, "", ["Sub/x.tmp", "Sub/y.txt"], "true", {"Sub/.gitignore": "/X.TMP\n"})
    assert _mine(r) == untracked_by_git(r)


# -- where core.ignorecase comes from


def _person_config(r, tmp_path: Path, text: str) -> dict[str, str]:
    cfg = tmp_path / "person.gitconfig"
    cfg.write_text(text)
    return {**r.env, "GIT_CONFIG_GLOBAL": str(cfg)}


def test_the_persons_ignorecase_applies_when_the_repository_sets_none(make_repo, tmp_path):
    r = _setup(make_repo, "*.log\n!Keep.log\n", ["keep.log", "other.log"], None)
    for val in ("true", "false"):
        r.env = _person_config(r, tmp_path, f"[core]\n\tignorecase = {val}\n")
        assert _mine(r) == untracked_by_git(r), val
        assert READER.tree_check(r.path, r.env)["ignore_rules"] == "applied"


def test_the_repositorys_ignorecase_wins_over_the_persons(make_repo, tmp_path):
    r = _setup(make_repo, "*.log\n!Keep.log\n", ["keep.log", "other.log"], "false")
    r.env = _person_config(r, tmp_path, "[core]\n\tignorecase = true\n")
    assert _mine(r) == untracked_by_git(r)
    r.git("config", "core.ignorecase", "true")
    r.env = _person_config(r, tmp_path, "[core]\n\tignorecase = false\n")
    assert _mine(r) == untracked_by_git(r)


@pytest.mark.parametrize("gitconfig", ["[include]\n\tpath = elsewhere\n", "[core]\n\tignorecase = banana\n"])
def test_a_repository_value_that_cannot_be_followed_matches_in_both_modes(make_repo, gitconfig):
    """An include may set ``core.ignorecase`` anywhere, and git dies on a value that is not a boolean: neither mode is
    known, so a path is ignored only if both modes ignore it, never cleaner than git whichever it is."""
    r = _setup(make_repo, "*.log\n!Keep.log\nbuild/\n", ["keep.log", "other.log", "build/x"], "true")
    with open(r.gitdir / "config", "a") as f:
        f.write(gitconfig)
    got = READER.tree_check(r.path, r.env)
    mine = {base64.b64decode(c["path_b64"]).decode() for c in got["changed"]}
    for ic in ("true", "false"):
        r.git("config", "--replace-all", "core.ignorecase", ic, check=False)
        if "include" in gitconfig:  # git itself refuses a value that is not a boolean
            theirs = untracked_by_git(r)
            assert theirs <= mine, (ic, sorted(theirs), sorted(mine))
    assert "keep.log" in mine and "other.log" not in mine and "build/x" not in mine


def test_host_mode_applies_the_repository_rules_with_the_repositorys_ignorecase(make_repo):
    r = _setup(make_repo, "*.log\n!Keep.log\n", ["keep.log", "other.log"], "true")
    assert _host(r) == untracked_by_git(r)
    assert _host_raw(r)["ignore_rules"] == "applied"


# -- parsing: git's add_patterns_from_buffer


def test_a_bom_trailing_spaces_and_escapes_are_read_as_git_reads_them(make_repo):
    cases = [
        (b"\xef\xbb\xbf!keep.log\n", ["keep.log"]),  # the BOM is skipped
        (b"a\\\\ \n", ["a\\", "a\\ "]),  # an escaped backslash, then a trailing space that is trimmed
        (b"b\\ \n", ["b ", "b"]),  # a protected trailing space
        (b"c   \n", ["c", "c "]),
        (b"d \\\n", ["d", "d "]),  # a lone trailing backslash
        (b"e\r\n", ["e"]),
        (b"x\\ y\n", ["x y"]),
    ]
    for ic in ("true", "false"):
        for n, (text, files) in enumerate(cases):
            r = _setup(make_repo, "*.log\n", files, ic, None, f"r{ic}{n}")
            (r.path / ".gitignore").write_bytes(b"*.log\n" + text)
            assert _mine(r) == untracked_by_git(r), (ic, text)


@pytest.mark.parametrize(
    "content",
    [
        b"!keep.log\n\xc3\xa9*\n!\xc3\xa9x\n",
        b"!keep.log\n\x01\n",
        b"!keep.log\n\xff\xfe\n",
        b"!keep.log\n\x65\xcc\x81x\n",
    ],
)
def test_an_uncertain_line_drops_its_file_and_every_lower_source(make_repo, content):
    """A negation with non-ASCII bytes, a control byte, bytes that are not UTF-8, text that is not NFC: dropping only
    that file would drop a negation and let info/exclude hide keep.log (cleaner than git)."""
    r = _setup(make_repo, "", ["keep.log", "other.log"], "false")
    (r.gitdir / "info").mkdir(exist_ok=True)
    (r.gitdir / "info" / "exclude").write_text("*.log\n")
    (r.path / ".gitignore").write_bytes(content)
    assert {"keep.log", "other.log"} <= _mine(r)


def test_a_non_ascii_positive_pattern_in_nfc_applies(make_repo):
    r = _setup(make_repo, "café*\n# Über comment\n", ["café.txt", "cafe.txt"], "false")
    assert _mine(r) == untracked_by_git(r)


# -- the matcher against git, pattern by pattern

ATOMS = [b"a", b"b", b"A", b"B", b"/", b"*", b"**", b"?", b"[ab]", b"[!a]", b"[^a]", b"[]a]", b"[a-c]", b"[A-C]",
         b"[b-B]", b"[A]", b"[!A]", b"\\A", b"\\a", b"[[:lower:]]", b"[[:upper:]]", b"[[:alpha:]]", b"[[:digit:]]",
         b"[[:punct:]]", b"\\*", b"\\!", b"\\#", b"\\ ", b" ", b"[", b"]", b"-", b"!", b"#", b"\\", b".", b"x",
         b"**/", b"/**", b"[a-]", b"[-a]", b"[\\]]", b"[!]]", b"[:", b"[[:", b"\\[", b"\t"]  # fmt: skip
PATH_BYTES = [b"a", b"b", b"A", b"B", b"x", b"/", b"!", b"#", b" ", b"*", b"[", b"]", b"-", b"\\", b".", b":", b"1",
              b"c", b"C", b"?"]  # fmt: skip


def _rand_pattern(rng: random.Random) -> bytes:
    s = b"".join(rng.choice(ATOMS) for _ in range(rng.randint(1, 5)))
    if rng.random() < 0.2:
        s = b"!" + s
    if rng.random() < 0.15:
        s = b"/" + s
    if rng.random() < 0.15:
        s += b"/"
    return s


def _rand_path(rng: random.Random, pat: bytes) -> bytes:
    out = bytearray()
    for ch in pat.lstrip(b"!/"):
        c = bytes([ch])
        if c in b"*?[]":
            out += b"".join(rng.choice(PATH_BYTES) for _ in range(rng.randint(0, 2)))
        elif c != b"\\":
            out += c
    out = bytearray(ch ^ 0x20 if (65 <= (ch | 0x20) <= 90 and rng.random() < 0.4) else ch for ch in out)
    parts = [x for x in bytes(out).split(b"/") if x and x not in (b".", b"..")]
    return b"/".join(parts) if not (parts and parts[0].startswith(b":")) else b""  # ":" starts a pathspec


@pytest.mark.parametrize("ic", [False, True])
def test_the_matcher_agrees_with_git_check_ignore_on_random_patterns(tmp_path, ic):
    """Seeded differential fuzz: gitread's ignore matching against ``git check-ignore --no-index`` with
    ``core.ignorecase`` set to ``ic``. A path the reader ignores and git does not is a false clean (none allowed); one
    git ignores and the reader does not is a mismatch (none either: the matcher is exact)."""
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}  # fmt: skip
    repo = tmp_path / "fz"
    subprocess.run([GIT, "init", "-q", str(repo)], env=env, check=True)
    subprocess.run([GIT, "-C", str(repo), "config", "core.ignorecase", "true" if ic else "false"], env=env, check=True)
    rng = random.Random(583 + ic)
    total = bad = 0
    folds = (ic,)
    for _ in range(120):
        pats = [_rand_pattern(rng) for _ in range(rng.randint(1, 3))]
        gi = b"\n".join(pats) + b"\n"
        paths = sorted({p for pat in pats for _ in range(6) if (p := _rand_path(rng, pat))})
        if not paths:
            continue
        (repo / ".gitignore").write_bytes(gi)
        got = subprocess.run([GIT, "-C", str(repo), "check-ignore", "--no-index", "-z", "--stdin"],
                             input=b"\0".join(paths) + b"\0", capture_output=True, env=env).stdout  # fmt: skip
        theirs = {x for x in got.split(b"\0") if x}
        parsed = _parse(gi, b"", None, folds)
        assert parsed is not None, gi  # the corpus holds no uncertain line (ASCII, no control bytes but tab)
        for p in paths:
            ig = Ignores(None, folds)
            ig.levels.append(parsed)
            parts = p.split(b"/")
            mine = any(ig.ignored(b"/".join(parts[:k]), True) for k in range(1, len(parts))) or ig.ignored(p, False)
            total += 1
            if mine != (p in theirs):
                bad += 1
                print(ic, gi, p, mine)
    assert total > 500 and bad == 0


def test_a_class_with_a_casefold_keeps_the_linear_compile():
    """M2 stays linear with folding: the CPU time of the compile grows in step with the class (measured in CPU time,
    as a ratio over 1x/2x/4x, so machine load does not matter and a quadratic compile still fails)."""
    from .conftest import assert_linear

    def compile_class(n):
        assert wildmatch.compile_glob(b"[" + b"aB" * (n << 17) + b"]", None, True) is not None

    assert_linear(compile_class, sizes=(1, 2, 4))


# -- Opus delta L1: NFD names under core.precomposeunicode


@pytest.mark.parametrize("precompose", ["true", "false"])
@pytest.mark.parametrize(
    "pattern,files",
    [("cafe*\n", ["café.txt"]), ("e?*\n", ["éx"]), ("cafe*/\n", ["café/a.txt"]),
     ("*\n!*.keep\n", ["é.keep"])],
)  # fmt: skip
def test_a_non_nfc_name_is_never_ignored_so_never_cleaner_than_git(make_repo, pattern, files, precompose):
    """git on macOS composes an NFD name to NFC before matching; the reader matches the bytes it lists. A path with a
    non-ASCII component that is not NFC is always untracked here: never cleaner than git whichever way git goes."""
    r = _setup(make_repo, pattern, files, "false")
    r.git("config", "core.precomposeunicode", precompose)
    import unicodedata

    def nfc(names):
        return {unicodedata.normalize("NFC", n) for n in names}

    theirs, mine = nfc(untracked_by_git(r)), nfc(_mine(r))
    assert theirs <= mine, (sorted(theirs), sorted(mine))  # never cleaner than git
    assert nfc(files) <= mine  # and the reader always counts them

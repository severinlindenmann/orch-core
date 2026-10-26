"""#583 (Opus M1, L1, M2, L5 on #565): the reader's ignore rules against git's, file by file.

* **Globs** are git's ``wildmatch`` without backtracking (:mod:`orch.gitread.wildmatch`): each pattern below, with its
  files, gives the same untracked list as plain ``git status --untracked-files=all`` (clean config), including the
  constructs the old regex got wrong (an unclosed ``[``, ``[[:class:]]``) and the one that made it hang.
* **The person's global ignore file** fails toward "nothing ignored": an ``include``/``includeIf`` or an empty
  ``core.excludesFile`` never makes the reader fall back to git's default file, so it never calls clean what git
  shows as untracked.
* **A budget** ends a direct-mode tree check that runs too long as unknown.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orch.gitread import local
from orch.gitread.errors import Unavailable
from orch.gitread.local import READER
from orch.gitread.wildmatch import compile_glob

from .conftest import GIT, assert_linear, cpu_budget

CASES = [
    ("[", ["[", "x"]),
    ("a[b", ["a[b", "ab"]),
    ("f[[:alpha:]]", ["f:]", "fx", "f1"]),
    ("x[[:digit:]]", ["xd]", "x1", "xa"]),
    ("[[:upper:]]*", ["Abc", "abc"]),
    ("[[:foo:]]x", ["fx", "[x"]),
    ("y[[:alpha:]", ["y[", "ya"]),
    ("[!a]b", ["ab", "cb"]),
    ("[^a]c", ["ac", "cc"]),
    ("[]]x", ["]x", "ax"]),
    ("[a-c]z", ["bz", "dz"]),
    ("[z-a]q", ["zq", "aq"]),
    ("[a-]w", ["-w", "aw", "bw"]),
    ("a[\\]]b", ["a]b", "a\\b"]),
    ("?v", ["av", "vv"]),
    ("*.log", ["a.log", "d/b.log", "c.txt"]),
    ("**/foo", ["foo", "d/foo", "d/e/foo", "foox"]),
    ("d/**/z", ["d/z", "d/e/z", "d/e/f/z", "z"]),
    ("d/**", ["d/x", "d/e/y"]),
    ("a**b", ["ab", "axxb"]),
    ("\\!bang", ["!bang", "bang"]),
    ("\\#hash", ["#hash"]),
    ("*a*a*a*a*a*a*a*ab", ["a" * 60, "a" * 20 + "b"]),
]


def _status(r) -> set[str]:
    out = subprocess.run([GIT, "--no-optional-locks", "-C", str(r.path), "status", "--porcelain=v1", "-z",
                          "--untracked-files=all"], capture_output=True, env=r.env, check=True).stdout  # fmt: skip
    return {e[3:].decode() for e in out.split(b"\0") if e}


def _mine(r) -> set[str]:
    import base64

    got = READER.tree_check(r.path, r.env)
    return {base64.b64decode(c["path_b64"]).decode() for c in got["changed"]}


@pytest.mark.parametrize("pattern,files", CASES, ids=[c[0] for c in CASES])
def test_an_ignore_pattern_ignores_exactly_what_git_ignores(make_repo, pattern, files):
    r = make_repo()
    r.git("config", "core.ignoreCase", "false")  # git matches case-sensitively; the policy can be sure (round 3)
    r.write("d/keep.txt", "k\n")
    r.commit("tracked")
    r.write(".gitignore", pattern + "\n")
    r.commit("ignore")
    for f in files:
        r.write(f, "x\n")
    with cpu_budget(5):  # no backtracking: the "*a*a*...*ab" line took 21 s before (M1)
        mine = _mine(r)
    theirs = _status(r)
    assert mine == theirs, (pattern, sorted(mine), sorted(theirs))


def test_the_matcher_is_linear_on_the_old_redos_line():
    glob = compile_glob(b"*a" * 12 + b"b")
    assert_linear(lambda n: glob.match(b"a" * n), sizes=(1000, 2000, 4000))  # backtracking would be n**12


def test_a_tree_check_past_its_budget_is_unknown_never_clean(make_repo, monkeypatch):
    """M1 / L5: direct mode has no child wall clock; the reader's own budget ends a long check as unknown."""
    r = make_repo()
    r.git("config", "core.ignorecase", "false")
    r.write(".gitignore", "".join(f"p{i}*q\n" for i in range(3000)))
    r.commit("many patterns")
    for i in range(300):
        r.write(f"u/f{i}", "x\n")
    assert READER.dirty(r.path, r.env)  # within the budget: an answer
    monkeypatch.setattr(local, "SECONDS", 0.0)
    with pytest.raises(Unavailable, match="too long"):
        READER.dirty(r.path, r.env)


# -- the person's global ignore file (M2)


def _person(r, home: Path, gitconfig: str | None, xdg_config: str | None = None, extra: dict | None = None):
    (home / ".config" / "git").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "git" / "ignore").write_text("*.def\n")  # git's default global ignore file
    (home / ".gi_other").write_text("*.oth\n")
    (home / ".gc2").write_text("[core]\n\texcludesFile = ~/.gi_other\n")
    for name, text in ((".gitconfig", gitconfig), (".config/git/config", xdg_config)):
        p = home / name
        if text is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(text)
    for k, v in (extra or {}).items():
        (home / k).write_text(v)


PERSON = [
    ("none", None, None, {"z.def"}, set()),  # the default file applies: z.def ignored by both
    ("include", "[include]\n\tpath = ~/.gc2\n", None, {"z.def"}, None),
    ("includeIf", '[includeIf "gitdir:/"]\n\tpath = ~/.gc2\n', None, {"z.def"}, None),
    ("empty value", None, "[core]\n\texcludesFile =\n", {"z.def"}, None),
    ("set directly", "[core]\n\texcludesFile = ~/.gi_other\n", None, {"z.def", "z.oth"}, {"z.def"}),
    ("unparsable", "[core\n\texcludesFile = ~/.gi_other\n", None, {"z.def"}, None),
]


@pytest.mark.parametrize("label,gitconfig,xdg,files,want", PERSON, ids=[p[0] for p in PERSON])
def test_the_persons_ignore_never_hides_what_git_shows(make_repo, label, gitconfig, xdg, files, want):
    """M2: git follows includes and treats an empty value as "no file"; the reader cannot follow includes, so it
    ignores nothing then. It may be dirtier than git, never cleaner."""
    r = make_repo()
    r.git("config", "core.ignorecase", "false")
    r.write("t", "t\n")
    r.commit("t")
    home = Path(r.env["HOME"])
    _person(r, home, gitconfig, xdg)
    for f in files:
        r.write(f, "x\n")
    env = {**r.env}
    env.pop("GIT_CONFIG_GLOBAL")  # git reads ~/.gitconfig and the XDG config, as for the person
    r.env = env
    try:
        mine = _mine(r)
        theirs = None if label == "unparsable" else _status(r)  # git itself refuses a config it cannot parse
    finally:
        r.env = {**env, "GIT_CONFIG_GLOBAL": os.devnull}
    assert theirs is None or theirs <= mine, (label, theirs, mine)  # never cleaner than git
    assert mine == (want if want is not None else files)  # an include or an empty value: nothing of the person's

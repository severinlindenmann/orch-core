"""#583 round 3 (orch-07, structural; owner decision B of 19 Oct 2026): *removal never depends on ignore rules; a status
check applies a rule only as git would, and where it cannot tell which way git matches it applies the rule only if git
would in either case.* :mod:`orch.gitread.ignorepolicy` is the one place that decides; these tests pin its decisions,
its bounds, and that no other code applies a pattern. The case-folding itself is in ``test_ignore_casefold.py``.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path

import pytest

from orch.gitread import ignorepolicy
from orch.gitread.errors import Unavailable
from orch.gitread.ignorepolicy import BYTES_MAX
from orch.gitread.local import READER, person_ignore

from .conftest import case_sensitive, cpu_budget

SRC = Path(ignorepolicy.__file__).parents[1]
USES = re.compile(r"\bcompile_glob\b|\bparse_ignore\b|ignorepolicy\._parse|\bIgnores\(|[\"']info/exclude[\"']")


def test_no_other_code_parses_or_applies_an_ignore_pattern():
    """The single entry point: ``_parse``, ``compile_glob`` and ``Pattern(`` appear only in ignorepolicy (and the
    matcher's own module); treecheck asks the policy for every stack it walks with."""
    # checkout.py matches .gitattributes patterns (#584: which blobs a filter attribute selects), never ignore rules
    allowed = {SRC / "gitread" / "ignorepolicy.py", SRC / "gitread" / "wildmatch.py", SRC / "gitread" / "checkout.py"}
    offenders = []
    for p in SRC.rglob("*.py"):
        if p in allowed:
            continue
        text = p.read_text()
        if USES.search(text):
            offenders.append(str(p.relative_to(SRC)))
    assert offenders == []


def _paths(raw) -> set[str]:
    return {base64.b64decode(c["path_b64"]).decode() for c in raw["changed"]}


def test_the_file_system_does_not_decide_core_ignorecase_does(make_repo, tmp_path):
    """Decision B: whether the volume folds case is not what git goes by; ``core.ignorecase`` is. With it false (set
    here whatever the volume), ``*.log`` ignores ``x.log`` and a ``!X.LOG`` negation does not apply, exactly as git."""
    r = make_repo()
    r.write(".gitignore", "*.log\n!X.LOG\n")
    r.commit()
    r.git("config", "core.ignorecase", "false")
    r.write("x.log", "x\n")
    assert "x.log" not in _paths(READER.tree_check(r.path, r.env))
    r.git("config", "core.ignorecase", "true")
    assert "x.log" in _paths(READER.tree_check(r.path, r.env))  # git folds: the negation re-includes it


def test_a_sure_gitignore_below_an_unsure_one_still_applies(make_repo):
    r = make_repo()
    case_sensitive(r)
    r.write("d/t", "t\n")
    r.commit()
    (r.path / ".gitignore").write_bytes(b"*.tmp\n!\xc3\xa9\n")  # a non-ASCII negation: unsure, dropped (only dirtier)
    r.write("d/.gitignore", "*.o\n")
    r.write("d/a.o", "x\n")
    r.write("d/b.tmp", "x\n")
    got = _paths(READER.tree_check(r.path, r.env))
    assert "d/b.tmp" in got and "d/a.o" not in got


def test_a_repository_core_excludesfile_drops_the_persons_file(make_repo):
    """Opus delta L1: git's excludesFile is then the repository's; the person's (lowest) is dropped, never guessed."""
    r = make_repo()
    case_sensitive(r)
    r.write("t", "t\n")
    r.commit()
    home = Path(r.env["HOME"])
    (home / ".config" / "git").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "git" / "ignore").write_text("*.def\n")
    r.write("z.def", "x\n")
    assert "z.def" not in _paths(READER.tree_check(r.path, r.env))  # the default file applies
    r.git("config", "core.excludesFile", str(home / "other"))
    assert "z.def" in _paths(READER.tree_check(r.path, r.env))
    (home / ".config" / "git" / "ignore").unlink()


@pytest.mark.parametrize(
    "extra,known",
    [({}, True), ({"XDG_CONFIG_HOME": "rel"}, False), ({"GIT_CONFIG_NOSYSTEM": "0"}, False),
     ({"GIT_CONFIG_NOSYSTEM": "true"}, False), ({"GIT_CONFIG_SYSTEM": "/etc/x"}, False),
     ({"GIT_CONFIG_PARAMETERS": "'core.ignorecase'='false'"}, False), ({"GIT_CONFIG_COUNT": "0"}, False),
     ({"HOME": "rel"}, False)],
)  # fmt: skip
def test_the_persons_environment_is_known_only_in_the_plain_cases(tmp_path, extra, known):
    """Opus delta L4: any environment that changes where git reads its configuration from is "not known"."""
    env = {"HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1", **extra}
    assert person_ignore(env).known is known


def test_ignorecase_in_the_persons_config_is_read_with_its_last_value(tmp_path):
    env = {"HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}
    assert person_ignore(env).ignorecase is None  # set nowhere: git's default is the policy's to apply
    (tmp_path / ".gitconfig").write_text("[core]\n\tignorecase = true\n")
    got = person_ignore(env)
    assert got.known and got.ignorecase is True
    (tmp_path / ".gitconfig").write_text("[core]\n\tignorecase = true\n\tignorecase = no\n")
    assert person_ignore(env).ignorecase is False
    (tmp_path / ".gitconfig").write_text("[core]\n\tignorecase = perhaps\n")
    assert not person_ignore(env).known  # git would die on it: not certain


def test_hard_linked_large_gitignores_end_quickly_as_unknown(make_repo):
    """Opus delta M2: 8 untracked directories, each with the same 1 MiB .gitignore as a hard link. git: 0.04 s; the
    reader took 67 s. The ignore bytes of one check are capped at BYTES_MAX: unknown at once."""
    r = make_repo()
    case_sensitive(r)
    r.write("t", "t\n")
    r.commit()
    first = r.path / "d0" / ".gitignore"
    first.parent.mkdir()
    first.write_bytes(b"[" + b"a" * (1 << 20) + b"]\n")
    for i in range(1, 8):
        (r.path / f"d{i}").mkdir()
        os.link(first, r.path / f"d{i}" / ".gitignore")
    with cpu_budget(5), pytest.raises(Unavailable, match="larger than"):
        READER.tree_check(r.path, r.env)


def test_a_class_heavy_gitignore_under_the_cap_compiles_in_linear_time(make_repo):
    r = make_repo()
    case_sensitive(r)
    r.write("t", "t\n")
    r.commit()
    lines = b"".join(b"[" + b"ab" * 1500 + b"]x\n" for _ in range(BYTES_MAX // 3100))
    r.write(".gitignore", lines)
    with cpu_budget(6):  # quadratic compile of this input cost minutes
        READER.tree_check(r.path, r.env)


def test_environment_passed_config_makes_the_repository_ignorecase_not_final(make_repo):
    """#724: GIT_CONFIG_COUNT/GIT_CONFIG_PARAMETERS outrank the repository's config in git, so the repository's
    ``core.ignorecase = false`` is not final: both modes run and x.log (re-included by ``!X.LOG`` folding) shows."""
    r = make_repo()
    r.write(".gitignore", "*.log\n!X.LOG\n")
    r.commit()
    r.git("config", "core.ignorecase", "false")
    r.write("x.log", "x\n")
    assert "x.log" not in _paths(READER.tree_check(r.path, r.env))  # known person config: the repo's value is final
    for extra in ({"GIT_CONFIG_COUNT": "0"}, {"GIT_CONFIG_PARAMETERS": "'core.ignorecase'='true'"}):
        assert person_ignore({"HOME": r.env["HOME"], **extra}).env_config is True
        assert "x.log" in _paths(READER.tree_check(r.path, {**r.env, **extra}))
    assert person_ignore({"HOME": r.env["HOME"], "GIT_CONFIG_SYSTEM": "/x"}).env_config is False

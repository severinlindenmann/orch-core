"""#583 round 3 (orch-07, structural): *removal never depends on ignore rules; ignore rules apply only when gitread is
sure it matches git, otherwise none.* :mod:`orch.gitread.ignorepolicy` is the one place that decides; these tests pin
its decisions, its bounds, and that no other code applies a pattern.
"""

from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path

import pytest

from orch.gitread import ignorepolicy
from orch.gitread.errors import Unavailable
from orch.gitread.ignorepolicy import BYTES_MAX
from orch.gitread.local import READER, person_ignore

from .conftest import case_sensitive

SRC = Path(ignorepolicy.__file__).parents[1]


def test_no_other_code_parses_or_applies_an_ignore_pattern():
    """The single entry point: ``_parse``, ``compile_glob`` and ``Pattern(`` appear only in ignorepolicy (and the
    matcher's own module); treecheck asks the policy for every stack it walks with."""
    allowed = {SRC / "gitread" / "ignorepolicy.py", SRC / "gitread" / "wildmatch.py"}
    offenders = []
    for p in SRC.rglob("*.py"):
        if p in allowed:
            continue
        text = p.read_text()
        if re.search(r"\bcompile_glob\b|\b_parse\(|\bPattern\(|parse_ignore|\.gitignore\"\)|info/exclude\"\)", text):
            offenders.append(str(p.relative_to(SRC)))
    assert offenders == []


def _paths(raw) -> set[str]:
    return {base64.b64decode(c["path_b64"]).decode() for c in raw["changed"]}


def test_the_file_system_probe_is_the_real_one(make_repo, tmp_path):
    r = make_repo()
    (tmp_path / "probe").write_text("x")
    folds = (tmp_path / "PROBE").exists()
    from orch.gitread.fs import open_walk

    d, _ = open_walk(os.path.realpath(r.path))
    try:
        assert ignorepolicy._case_insensitive(d) is folds
    finally:
        d.close()
    r.write(".gitignore", "*.log\n")
    r.commit()
    r.git("config", "core.ignorecase", "false")
    r.write("x.log", "x\n")
    got = READER.tree_check(r.path, r.env)
    assert ("x.log" in _paths(got)) is folds  # a case-insensitive file system: no rule applies (dirtier)


def test_the_probes_keep_log_case_is_shown(make_repo, sure_ignores):
    """Opus delta M1: info/exclude ``*.log`` / ``!Keep.log`` with git's default ``ignorecase=true`` hides keep.log in
    a case-sensitive match while git shows it; the policy applies no rule then."""
    r = make_repo()
    r.write("t", "t\n")
    r.commit()
    r.git("config", "core.ignorecase", "true")
    (r.gitdir / "info").mkdir(exist_ok=True)
    (r.gitdir / "info" / "exclude").write_text("*.log\n!Keep.log\n")
    r.write("keep.log", "mine\n")
    r.write("other.log", "x\n")
    got = READER.tree_check(r.path, r.env)
    assert {"keep.log", "other.log"} <= _paths(got) and got["ignore_rules"].startswith("none")


@pytest.mark.parametrize("content", [b"\xef\xbb\xbf!keep.log\n", b"!keep.log\na\\ \n", b"!keep.log\n\xc3\xa9*\n",
                                     b"!keep.log\nx\\y\n", b"!keep.log\n\x01\n"])  # fmt: skip
def test_an_unsure_gitignore_drops_itself_and_every_lower_source(make_repo, sure_ignores, content):
    """A BOM, an unproven backslash, non-ASCII or a control byte: the file is not certain. Dropping it alone would
    drop its negation and let info/exclude hide keep.log (cleaner than git); everything lower goes with it."""
    r = make_repo()
    case_sensitive(r)
    r.write("t", "t\n")
    r.commit()
    (r.gitdir / "info").mkdir(exist_ok=True)
    (r.gitdir / "info" / "exclude").write_text("*.log\n")
    (r.path / ".gitignore").write_bytes(content)
    r.write("keep.log", "mine\n")
    r.write("other.log", "x\n")
    assert {"keep.log", "other.log", ".gitignore"} <= _paths(READER.tree_check(r.path, r.env))


def test_a_sure_gitignore_below_an_unsure_one_still_applies(make_repo, sure_ignores):
    r = make_repo()
    case_sensitive(r)
    r.write("d/t", "t\n")
    r.commit()
    (r.path / ".gitignore").write_bytes(b"\xef\xbb\xbf*.tmp\n")  # unsure: dropped (only dirtier)
    r.write("d/.gitignore", "*.o\n")
    r.write("d/a.o", "x\n")
    r.write("d/b.tmp", "x\n")
    got = _paths(READER.tree_check(r.path, r.env))
    assert "d/b.tmp" in got and "d/a.o" not in got


def test_a_repository_core_excludesfile_drops_the_persons_file(make_repo, sure_ignores):
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


def test_ignorecase_in_the_persons_config_means_no_rule(tmp_path):
    (tmp_path / ".gitconfig").write_text("[core]\n\tignorecase = true\n")
    got = person_ignore({"HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"})
    assert got.known and got.ignorecase


def test_hard_linked_large_gitignores_end_quickly_as_unknown(make_repo, sure_ignores):
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
    started = time.monotonic()
    with pytest.raises(Unavailable, match="larger than"):
        READER.tree_check(r.path, r.env)
    assert time.monotonic() - started < 2


def test_a_class_heavy_gitignore_under_the_cap_compiles_in_linear_time(make_repo, sure_ignores):
    r = make_repo()
    case_sensitive(r)
    r.write("t", "t\n")
    r.commit()
    lines = b"".join(b"[" + b"ab" * 1500 + b"]x\n" for _ in range(BYTES_MAX // 3100))
    r.write(".gitignore", lines)
    started = time.monotonic()
    READER.tree_check(r.path, r.env)
    assert time.monotonic() - started < 3

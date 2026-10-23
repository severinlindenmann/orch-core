"""[rev818] adversarial: the cwd default of `orch host convenience on|off` and the error writer's `fix`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orch.host import convenience
from orch.host.cli import _cwd_workspace
from tests.host import human_helpers
from tests.host.lifecycle_helpers import UID
from tests.host.test_click_cli import run

lab, served = human_helpers.lab, human_helpers.served

WA, WB = "a" * 26, "b" * 26


def _entries(a: Path, b: Path) -> list[dict]:
    return [
        {"workspace_id": WA, "prefix": "AAA", "root": str(a)},
        {"workspace_id": WB, "prefix": "BBB", "root": str(b)},
    ]


def test_pwd_env_does_not_steer_the_choice(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    monkeypatch.chdir(b)
    monkeypatch.setenv("PWD", str(a))  # an env var never names the workspace; a mismatch refuses
    assert _cwd_workspace(_entries(a, b)) is None
    monkeypatch.setenv("PWD", os.getcwd())
    assert _cwd_workspace(_entries(a, b)) == WB


def test_nested_registered_root_innermost_wins_and_a_fake_dot_orch_is_ignored(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = a / "sub" / "b"
    b.mkdir(parents=True)
    (a / "sub" / ".orch").mkdir()  # a planted .orch folder is not a registered root
    monkeypatch.chdir(a / "sub")
    monkeypatch.setenv("PWD", os.getcwd())
    assert _cwd_workspace(_entries(a, b)) == WA
    monkeypatch.chdir(b)
    monkeypatch.setenv("PWD", os.getcwd())
    assert _cwd_workspace(_entries(a, b)) == WB


def test_a_symlink_inside_workspace_a_selects_workspace_b(tmp_path, monkeypatch):
    """Finding: a link planted in A's tree (an agent can write files there) points at B; running the command 'in A'
    flips B. The physical cwd is all the process sees; the output names B."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (a / "docs").symlink_to(b, target_is_directory=True)
    monkeypatch.chdir(a / "docs")
    monkeypatch.setenv("PWD", str(a / "docs"))  # the shell's logical directory is the link
    assert _cwd_workspace(_entries(a, b)) is None  # fixed: logical and physical differ, so no default


def test_bad_roots_never_match_everything(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _cwd_workspace([{"workspace_id": WA, "root": None}, {"workspace_id": WB}]) is None
    # an empty or relative root (refused by Entry.from_doc, but this reader uses the raw JSON) resolves to the cwd
    assert _cwd_workspace([{"workspace_id": WA, "root": ""}, {"workspace_id": WB, "root": "rel/path"}]) is None


def test_an_agent_in_the_workspace_folder_is_refused(served, monkeypatch):  # noqa: F811
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    monkeypatch.chdir(served.service.entry.root)
    monkeypatch.setenv("PWD", os.getcwd())
    code, out, _ = run(
        "host", "convenience", "off", "--state", str(served.lab.state), env={"HOME": "/nonexistent", "ORCH_GRANT": "g"}
    )
    assert code == 3 and "human_only" in out
    assert convenience.enabled(served.lab.state, served.wid, UID)


@pytest.mark.parametrize("switch", ["on;id", "$(id)", "show --x"])
def test_fix_argv_takes_only_the_parser_choices(switch, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        run("host", "convenience", switch, "--state", str(tmp_path))

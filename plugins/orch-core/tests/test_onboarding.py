import io
import json
import shutil
import subprocess

import pytest

from orch.cli import run
from orch.onboarding import (
    OPEN_ITEM_CODES,
    SETUP_HINT,
    dismiss,
    is_dismissed,
    load_state,
    open_setup_items,
    outside_workspace_hint,
    state_path,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_state_file_is_tolerant(tmp_path):
    assert load_state() == {"dismissed_repos": [], "dismissed_items": {}}
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text("{broken", encoding="utf-8")
    assert load_state() == {"dismissed_repos": [], "dismissed_items": {}}


@needs_git
def test_hint_only_in_undismissed_git_repos(tmp_path):
    assert outside_workspace_hint(tmp_path) is None  # not a git repo
    repo = _repo(tmp_path / "repo")
    assert outside_workspace_hint(repo) == SETUP_HINT
    dismiss(repo)
    assert is_dismissed(repo) and outside_workspace_hint(repo) is None
    assert json.loads(state_path().read_text(encoding="utf-8"))["dismissed_repos"] == [str(repo.resolve())]


def test_open_setup_items_and_item_dismissal(ws_root, ws):
    codes = [c.code for c in open_setup_items(ws)]
    assert "repos" in codes and "uv" not in codes and "terminal-cli" not in codes
    dismiss(ws.root, "repos")
    assert "repos" not in [c.code for c in open_setup_items(ws)]


def test_load_state_tolerates_wrong_shapes(tmp_path):
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps({"dismissed_repos": 5, "dismissed_items": ["a"]}), encoding="utf-8")
    assert load_state() == {"dismissed_repos": [], "dismissed_items": {}}


def test_session_start_survives_malformed_state_shapes(ws_root, ws, monkeypatch, capsys):
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps({"dismissed_repos": 5, "dismissed_items": ["a"]}), encoding="utf-8")
    assert _session_start(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "# orch workspace" in out
    assert "Tickets claimed by this session" in out


def test_state_path_falls_back_to_home_config(monkeypatch, tmp_path):
    monkeypatch.delenv("ORCH_STATE_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))  # ignored on purpose
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert state_path() == tmp_path / ".config" / "orch" / "onboarding.json"


def test_state_path_honours_orch_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    assert state_path() == tmp_path / "s" / "onboarding.json"


@needs_git
def test_dismiss_from_bash_tool_holds_for_the_plugin_hook(tmp_path, monkeypatch, capsys):
    """The Bash tool has no CLAUDE_PLUGIN_DATA, the SessionStart hook has: both must share one state file."""
    repo = _repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    assert run(["setup", "--dismiss"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    assert _session_start(monkeypatch) == 0
    assert capsys.readouterr().out.strip() == ""


def test_dismiss_item_keys_by_workspace_root_from_subdir(ws_root, ws, monkeypatch):
    sub = ws_root / "sub" / "dir"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert run(["setup", "--dismiss", "repos"]) == 0
    assert "repos" not in [c.code for c in open_setup_items(ws)]


@needs_git
def test_dismiss_item_from_nested_git_repo_inside_workspace(ws_root, ws, monkeypatch, capsys):
    nested = _repo(ws_root / "nested-repo")
    monkeypatch.chdir(nested)
    assert run(["setup", "--dismiss", "repos"]) == 0
    assert "repos" not in [c.code for c in open_setup_items(ws)]
    monkeypatch.chdir(ws_root)
    assert _session_start(monkeypatch) == 0
    assert "git.repos" not in capsys.readouterr().out


@needs_git
def test_setup_dismiss_item_outside_workspace_errors(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    rc = run(["setup", "--dismiss", "repos"])
    assert rc != 0
    assert capsys.readouterr().err.strip() != ""


def test_setup_dismiss_unknown_item_errors_without_writing(ws_root, ws):
    assert not state_path().exists()
    rc = run(["setup", "--dismiss", "bogus-code"])
    assert rc != 0
    assert not state_path().exists()


def test_setup_dismiss_unknown_item_lists_valid_codes(ws_root, ws, capsys):
    run(["setup", "--dismiss", "bogus-code"])
    err = capsys.readouterr().err
    assert "bogus-code" in err
    for code in OPEN_ITEM_CODES:
        assert code in err


def test_setup_item_without_dismiss_errors(ws_root, ws, capsys):
    rc = run(["setup", "repos"])
    assert rc != 0
    assert "--dismiss" in capsys.readouterr().err


def _session_start(monkeypatch, payload="{}"):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    return run(["hook", "session-start"])


@needs_git
def test_session_start_outside_workspace_prints_hint_once_dismissed_never(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    assert _session_start(monkeypatch) == 0
    assert capsys.readouterr().out.strip() == SETUP_HINT
    assert run(["setup", "--dismiss"]) == 0
    capsys.readouterr()
    assert _session_start(monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_session_start_outside_git_prints_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert _session_start(monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_session_start_in_workspace_lists_open_setup(ws_root, ws, monkeypatch, capsys):
    assert _session_start(monkeypatch) == 0
    out = capsys.readouterr().out
    assert "Setup still open:" in out and "git.repos" in out and "- repos:" in out
    assert run(["setup", "--dismiss", "repos"]) == 0
    capsys.readouterr()
    assert _session_start(monkeypatch) == 0
    assert "git.repos" not in capsys.readouterr().out


def test_setup_without_dismiss_points_to_the_skill(ws_root, ws, capsys):
    assert run(["setup"]) == 0
    out = capsys.readouterr().out
    assert "orch-setup" in out and "/orch-core:setup" in out

"""#170: per-repo commit check mode (enforce | warn | off) and allowed message patterns."""
import shutil
import subprocess

import pytest

from orch.cli import run
from orch.config.load import validate_schema
from orch.core.check import run_checks
from orch.hooks.commit_msg import check_message, commit_check_mode, commit_skip_problems, repo_name_for
from orch.hooks.install import hook_state, install_hooks
from orch.onboarding import doctor

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _msg(tmp_path, text):
    f = tmp_path / "COMMIT_EDITMSG"
    f.write_text(text, encoding="utf-8")
    return str(f)


# -- defaults and modes -----------------------------------------------------------------------------------------

def test_default_mode_is_enforce(configure):
    ws = configure(git={"repos": {"Work": {}}})
    assert commit_check_mode(ws.config, "Work") == "enforce"
    assert commit_check_mode(ws.config, "unlisted") == "enforce"
    assert commit_check_mode(ws.config, None) == "enforce"


@pytest.mark.parametrize("mode", ["enforce", "warn", "off"])
def test_mode_is_read_per_repo(configure, mode):
    ws = configure(git={"repos": {"Work": {"commit_check": mode}, "Other": {}}})
    assert commit_check_mode(ws.config, "Work") == mode
    assert commit_check_mode(ws.config, "Other") == "enforce"


def test_unknown_mode_falls_back_to_enforce(configure):
    ws = configure(git={"repos": {"Work": {"commit_check": "lenient"}}})
    assert commit_check_mode(ws.config, "Work") == "enforce"


# -- allowed message patterns -----------------------------------------------------------------------------------

def test_pattern_lets_tool_commit_through_but_not_others(configure):
    ws = configure(git={"repos": {"Work": {"commit_skip": ["^vault backup:"]}}})
    assert check_message(ws, "vault backup: 2026-10-07 10:00:00", repo="Work") == []
    assert any("subject must match" in p for p in check_message(ws, "update", repo="Work"))
    assert any("subject must match" in p for p in check_message(ws, "update vault backup: x", repo="Work"))


def test_pattern_is_per_repo(configure):
    ws = configure(git={"repos": {"Work": {"commit_skip": ["^vault backup:"]}, "Code": {}}})
    assert check_message(ws, "vault backup: x", repo="Code")
    assert check_message(ws, "vault backup: x")  # no repo known: nothing is relaxed


def test_global_pattern_applies_to_every_repo(configure):
    ws = configure(commit={"skip": ["^chore\\(deps\\):"]}, git={"repos": {"A": {}, "B": {}}})
    assert check_message(ws, "chore(deps): bump x", repo="A") == []
    assert check_message(ws, "chore(deps): bump x", repo="B") == []
    assert check_message(ws, "chore(deps): bump x") == []


def test_pattern_does_not_waive_attribution_rule(configure):
    ws = configure(git={"repos": {"Work": {"commit_skip": ["^vault backup:"]}}})
    problems = check_message(ws, "vault backup: x\n\nCo-Authored-By: Claude <noreply@anthropic.com>", repo="Work")
    assert any("attribution" in p for p in problems) and not any("subject" in p for p in problems)


def test_invalid_pattern_is_ignored_when_checking_a_message(configure):
    ws = configure(git={"repos": {"Work": {"commit_skip": ["(unclosed"]}}})
    assert check_message(ws, "update", repo="Work")


def test_agent_commits_get_the_full_format_despite_patterns(configure, monkeypatch):
    ws = configure(git={"repos": {"Work": {"commit_skip": ["^vault backup:"]}}})
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    assert any("subject must match" in p for p in check_message(ws, "vault backup: x", repo="Work"))


# -- config validation ------------------------------------------------------------------------------------------

def test_schema_accepts_the_new_keys(configure):
    ws = configure(commit={"skip": ["^x"]}, git={"repos": {"Work": {"commit_check": "warn", "commit_skip": ["^y"]}}})
    assert validate_schema(ws.config) == []


@pytest.mark.parametrize("repo", [{"commit_check": "lenient"}, {"commit_check": True}, {"commit_skip": "^x"},
                                  {"commit_skip": [3]}, {"commit_skip": [""]}])
def test_schema_rejects_bad_repo_values(configure, repo):
    ws = configure(git={"repos": {"Work": repo}})
    errors = validate_schema(ws.config)
    assert errors and all("git/repos/Work/commit_" in e for e in errors)


def test_schema_rejects_bad_global_skip(configure):
    assert validate_schema(configure(commit={"skip": "^x"}).config)


def test_commit_skip_problems_names_the_bad_pattern(configure):
    ws = configure(commit={"skip": ["(bad"]}, git={"repos": {"Work": {"commit_skip": ["ok", "[x"]}}})
    problems = commit_skip_problems(ws.config)
    assert len(problems) == 2
    assert "commit.skip[0]" in problems[0] and "'(bad'" in problems[0]
    assert "git.repos.Work.commit_skip[1]" in problems[1]


def test_orch_check_reports_an_invalid_pattern_as_error(configure):
    ws = configure(git={"repos": {"Work": {"commit_skip": ["(bad"]}}})
    found = [f for f in run_checks(ws, emit_events=False) if f.code == "commit-skip"]
    assert len(found) == 1 and found[0].level == "error" and "Work" in found[0].message
    assert not [f for f in run_checks(configure(), emit_events=False) if f.code == "commit-skip"]


# -- the hook command -------------------------------------------------------------------------------------------

@needs_git
def test_repo_name_is_found_from_cwd(configure, ws_root):
    repo = _init(ws_root / "vault")
    (repo / "notes").mkdir()
    ws = configure(git={"repos": {"Work": {"path": "vault"}}})
    assert repo_name_for(ws, repo) == "Work"
    assert repo_name_for(ws, repo / "notes") == "Work"
    assert repo_name_for(ws, ws_root) is None


@needs_git
def test_hook_off_accepts_anything_from_a_human(configure, ws_root, monkeypatch, tmp_path, capsys):
    repo = _init(ws_root / "Work")
    ws = configure(git={"repos": {"Work": {"commit_check": "off"}}})
    monkeypatch.setenv("ORCH_HOME", str(ws.home))
    monkeypatch.chdir(repo)
    assert run(["hook", "commit-msg", _msg(tmp_path, "update\n")]) == 0
    assert capsys.readouterr().err == ""


@needs_git
def test_hook_warn_prints_problems_but_exits_zero(configure, ws_root, monkeypatch, tmp_path, capsys):
    repo = _init(ws_root / "Work")
    ws = configure(git={"repos": {"Work": {"commit_check": "warn"}}})
    monkeypatch.setenv("ORCH_HOME", str(ws.home))
    monkeypatch.chdir(repo)
    assert run(["hook", "commit-msg", _msg(tmp_path, "update\n")]) == 0
    err = capsys.readouterr().err
    assert "warning" in err and "subject must match" in err and "rejected" not in err


@needs_git
def test_hook_enforce_is_the_default_and_rejects(configure, ws_root, monkeypatch, tmp_path, capsys):
    repo = _init(ws_root / "Work")
    ws = configure(git={"repos": {"Work": {}}})
    monkeypatch.setenv("ORCH_HOME", str(ws.home))
    monkeypatch.chdir(repo)
    assert run(["hook", "commit-msg", _msg(tmp_path, "update\n")]) == 1
    assert "orch: commit rejected" in capsys.readouterr().err


@needs_git
@pytest.mark.parametrize("mode", ["off", "warn"])
def test_hook_enforces_for_agents_in_relaxed_repos(configure, ws_root, monkeypatch, tmp_path, capsys, mode):
    repo = _init(ws_root / "Work")
    ws = configure(git={"repos": {"Work": {"commit_check": mode}}})
    monkeypatch.setenv("ORCH_HOME", str(ws.home))
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    monkeypatch.chdir(repo)
    assert run(["hook", "commit-msg", _msg(tmp_path, "update\n")]) == 1
    assert "orch: commit rejected" in capsys.readouterr().err


@needs_git
def test_hook_pattern_end_to_end(configure, ws_root, monkeypatch, tmp_path):
    repo = _init(ws_root / "Work")
    ws = configure(git={"repos": {"Work": {"commit_skip": ["^vault backup:"]}}})
    monkeypatch.setenv("ORCH_HOME", str(ws.home))
    monkeypatch.chdir(repo)
    assert run(["hook", "commit-msg", _msg(tmp_path, "vault backup: 2026-10-07\n")]) == 0
    assert run(["hook", "commit-msg", _msg(tmp_path, "update\n")]) == 1


# -- install and doctor -----------------------------------------------------------------------------------------

@needs_git
def test_install_skips_a_repo_whose_check_is_off(configure, ws_root):
    _init(ws_root / "Work")
    _init(ws_root / "Code")
    ws = configure(git={"repos": {"Work": {"commit_check": "off"}, "Code": {}}})
    got = {p.name: a for p, a in install_hooks(ws)}
    assert got["Code"] == "installed"
    assert got["Work"].startswith("skipped: commit_check is off")
    assert hook_state(ws_root / "Work") == "missing"
    explicit = {p.name: a for p, a in install_hooks(ws, [ws_root / "Work"])}
    assert explicit["Work"].startswith("skipped: commit_check is off")


@needs_git
def test_warn_repo_still_gets_the_hook(configure, ws_root):
    _init(ws_root / "Work")
    ws = configure(git={"repos": {"Work": {"commit_check": "warn"}}})
    assert {p.name: a for p, a in install_hooks(ws)}["Work"] == "installed"


@needs_git
def test_doctor_treats_off_as_intentional(configure, ws_root):
    _init(ws_root / "Work")
    configure(git={"repos": {"Work": {"commit_check": "off"}}})
    checks = {c.code: c for c in doctor(ws_root)}
    assert checks["hooks"].ok is True and "Work" in checks["hooks"].message and "off" in checks["hooks"].message
    _init(ws_root / "Code")
    ws = configure(git={"repos": {"Work": {"commit_check": "off"}, "Code": {}}})
    checks = {c.code: c for c in doctor(ws_root)}
    assert checks["hooks"].ok is False
    assert checks["hooks"].message.startswith("no orch commit-message check in: Code")  # Work is not missing
    install_hooks(ws)
    assert {c.code: c.ok for c in doctor(ws_root)}["hooks"] is True

"""#161: a workspace root that is a plain folder of git repos, and a hook install that changes no tracked file."""
import os
import shlex
import shutil
import subprocess

import pytest

from orch.cli import run
from orch.hooks.install import UNTRACKED_DIR, hook_state, install_hooks, uninstall_untracked
from orch.onboarding import doctor
from conftest import init_repo

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="sh not installed")
pytestmark = needs_git


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, env=env)


def _repo(parent, name):
    repo = parent / name
    repo.mkdir(parents=True)
    init_repo(repo)
    return repo


def _checks(root):
    return {c.code: c for c in doctor(root)}


def _fake_orch(tmp_path, exit_code=0):
    """An `orch` on PATH that logs its arguments; the wrappers call it by name."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "orch-calls"
    (bin_dir / "orch").write_text(f"#!/bin/sh\necho \"$@\" >> {shlex.quote(str(log))}\nexit {exit_code}\n",
                                  encoding="utf-8")
    (bin_dir / "orch").chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}, log


# -- doctor: plain workspace root -------------------------------------------------------------------------------

def test_git_check_reads_local_only_for_a_plain_root_of_repos(configure, ws_root):
    _repo(ws_root, "hub")
    _repo(ws_root, "libs/meta")
    configure(git={"repos": {"hub": {}, "meta": {"path": "libs/meta"}}})
    checks = _checks(ws_root)
    assert checks["git"].ok is True
    assert "local only" in checks["git"].message and "2 git repo(s)" in checks["git"].message
    assert "orch doctor --init-git" in checks["git"].message
    assert checks["records"].ok is True and "local only" in checks["records"].message


def test_git_check_still_fails_when_a_configured_repo_is_not_a_repo(configure, ws_root):
    _repo(ws_root, "hub")
    (ws_root / "plain").mkdir()
    configure(git={"repos": {"hub": {}, "plain": {}}})
    assert _checks(ws_root)["git"].ok is False


def test_git_check_still_fails_for_a_repo_outside_the_root(configure, ws_root, tmp_path):
    _repo(tmp_path, "elsewhere")
    configure(git={"repos": {"x": {"path": str(tmp_path / "elsewhere")}}})
    assert _checks(ws_root)["git"].ok is False


def test_init_git_makes_a_local_only_repo_that_ignores_the_repos(configure, ws_root, capsys):
    _repo(ws_root, "hub")
    _repo(ws_root, "libs/meta")
    configure(git={"repos": {"hub": {}, "meta": {"path": "libs/meta"}}})
    (ws_root / ".gitignore").write_text("my-secret.json\n", encoding="utf-8")
    assert run(["doctor", "--init-git"]) == 0
    out = capsys.readouterr().out
    assert "initialized" in out and "no remote" in out
    assert (ws_root / ".git").is_dir()
    ignore = (ws_root / ".gitignore").read_text(encoding="utf-8")
    for line in ("/hub/", "/libs/meta/", "/.claude/worktrees/", "my-secret.json"):
        assert line in ignore.splitlines()
    assert _git(ws_root, "remote").stdout == ""
    assert _git(ws_root, "rev-parse", "--verify", "-q", "HEAD").returncode != 0  # nothing committed
    status = _git(ws_root, "status", "--porcelain", "--untracked-files=all").stdout
    assert "hub/" not in status and "libs/" not in status
    assert "managed by" in (ws_root / "orchestrator" / ".gitignore").read_text(encoding="utf-8")
    assert _checks(ws_root)["git"].message.startswith("git repository")
    assert run(["doctor", "--init-git"]) != 0  # already a repo: refused, nothing rewritten
    assert (ws_root / ".gitignore").read_text(encoding="utf-8") == ignore


def test_init_git_refuses_without_subfolder_repos(configure, ws_root, capsys):
    configure(git={"repos": {}})
    assert run(["doctor", "--init-git"]) != 0
    assert not (ws_root / ".git").exists()


def test_stage_records_is_skipped_and_explained_in_a_plain_root(configure, ws_root):
    repo = _repo(ws_root, "hub")
    ws = configure(git={"repos": {"hub": {}}})
    got = dict((p.name, a) for p, a in install_hooks(ws, stage_records=True))["hub"]
    assert got.startswith("installed") and "pre-commit skipped: the workspace root" in got and "local only" in got
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


def test_doctor_warns_when_hooks_path_folder_is_missing(configure, ws_root, tmp_path):
    repo = _repo(ws_root, "hub")
    gone = tmp_path / "gone-hooks"
    _git(repo, "config", "core.hooksPath", str(gone))
    configure(git={"repos": {"hub": {}}})
    check = _checks(ws_root)["hooks-path"]
    assert check.ok is False and "hub" in check.message and str(gone) in check.message
    gone.mkdir()
    assert "hooks-path" not in _checks(ws_root)


# -- hooks install --untracked ----------------------------------------------------------------------------------

def _repo_with_tracked_hooks(ws_root, marker):
    """A repo whose committed .githooks/commit-msg is its core.hooksPath, as in the issue."""
    repo = _repo(ws_root, "hub")
    hooks = repo / ".githooks"
    hooks.mkdir()
    (hooks / "commit-msg").write_text(f"#!/bin/sh\necho \"$1\" >> {shlex.quote(str(marker))}\n", encoding="utf-8")
    (hooks / "commit-msg").chmod(0o755)
    (hooks / "pre-commit").write_text(f"#!/bin/sh\necho pre >> {shlex.quote(str(marker))}\n", encoding="utf-8")
    (hooks / "pre-commit").chmod(0o755)
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "hooks").returncode == 0
    _git(repo, "config", "core.hooksPath", ".githooks")
    return repo


@needs_sh
def test_untracked_install_changes_no_tracked_file_and_keeps_the_repos_hooks(configure, ws_root, tmp_path):
    marker = tmp_path / "their-hooks"
    repo = _repo_with_tracked_hooks(ws_root, marker)
    ws = configure(git={"repos": {"hub": {}}})
    got = install_hooks(ws, untracked=True)[0][1]
    assert got.startswith("installed (untracked") and "no tracked file changed" in got
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout == ""
    folder = (repo / ".git" / UNTRACKED_DIR).resolve()
    assert _git(repo, "config", "--local", "core.hooksPath").stdout.strip() == str(folder)
    assert hook_state(repo) == "installed"
    assert install_hooks(ws, untracked=True)[0][1] == "unchanged"
    assert install_hooks(ws)[0][1] == "unchanged"  # a plain re-run keeps the untracked mode
    assert _git(repo, "status", "--porcelain").stdout == ""

    env, log = _fake_orch(tmp_path)
    assert _git(repo, "commit", "-q", "--allow-empty", "-m", "L-0001 x", env=env).returncode == 0
    assert "hook commit-msg" in log.read_text(encoding="utf-8")
    ran = marker.read_text(encoding="utf-8").splitlines()
    assert ran[-2] == "pre" and ran[-1].endswith("COMMIT_EDITMSG")  # their hooks, unchanged and in order

    env, _ = _fake_orch(tmp_path, exit_code=1)
    before = marker.read_text(encoding="utf-8")
    assert _git(repo, "commit", "-q", "--allow-empty", "-m", "nope", env=env).returncode != 0
    assert marker.read_text(encoding="utf-8") == before + "pre\n"  # rejected before their commit-msg ran


def test_untracked_install_without_a_hooks_path_runs_dot_git_hooks(configure, ws_root):
    repo = _repo(ws_root, "hub")
    ws = configure(git={"repos": {"hub": {}}})
    got = install_hooks(ws, untracked=True)[0][1]
    assert got.startswith("installed") and "the hook from .git/hooks" in got
    text = (repo / ".git" / UNTRACKED_DIR / "post-checkout").read_text(encoding="utf-8")
    assert "orig=''" in text and "git rev-parse --git-common-dir" in text and "ORCH_HOME" not in text
    assert _git(repo, "config", "--local", "--get", "orch.hooksPathBefore").stdout.strip() == ""


def test_untracked_install_names_a_missing_previous_hooks_path(configure, ws_root, tmp_path):
    repo = _repo(ws_root, "hub")
    _git(repo, "config", "core.hooksPath", str(tmp_path / "gone"))
    ws = configure(git={"repos": {"hub": {}}})
    assert "does not exist" in install_hooks(ws, untracked=True)[0][1]


def test_uninstall_puts_the_old_hooks_path_back(configure, ws_root, tmp_path):
    repo = _repo_with_tracked_hooks(ws_root, tmp_path / "m")
    ws = configure(git={"repos": {"hub": {}}})
    install_hooks(ws, untracked=True, stage_records=False)
    assert uninstall_untracked(repo).startswith("removed")
    assert _git(repo, "config", "--local", "core.hooksPath").stdout.strip() == ".githooks"
    assert not (repo / ".git" / UNTRACKED_DIR).exists()
    assert _git(repo, "config", "--local", "--get-regexp", "^orch\\.").stdout == ""
    assert uninstall_untracked(repo).startswith("skipped")

    plain = _repo(ws_root, "plain")
    install_hooks(configure(git={"repos": {"plain": {}}}), untracked=True)
    assert uninstall_untracked(plain) == "removed (core.hooksPath unset again)"
    assert _git(plain, "config", "--local", "--get", "core.hooksPath").returncode == 1


def test_uninstall_is_refused_under_an_agent(configure, ws_root, monkeypatch):
    repo = _repo(ws_root, "hub")
    ws = configure(git={"repos": {"hub": {}}})
    install_hooks(ws, untracked=True)
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    assert run(["hooks", "uninstall"]) != 0
    assert hook_state(repo) == "installed"

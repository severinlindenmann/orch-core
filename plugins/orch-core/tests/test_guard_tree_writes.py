"""Writes to files that run code in the human's sessions are denied to agents (M2); git working-tree commands are
not guarded (fix round 2 ruling)."""
import pytest

from orch.hooks.guard import evaluate


def bash(cmd, cwd=None):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, **({"cwd": str(cwd)} if cwd else {})}


@pytest.mark.parametrize("cmd", ["git stash pop", "git merge --abort", "git restore --staged x.py", "git reset --hard"])
def test_git_working_tree_commands_are_not_guarded(ws, cmd):
    """Ruling (fix round 2): no tree-rewrite denial; ledger-backed proceed points and orch check catch the effect."""
    assert evaluate(ws, bash(cmd, ws.root)).allow, cmd


@pytest.mark.parametrize("cmd", [
    "echo 'export X=1' >> ~/.zshrc", "printf x > ~/.bashrc", "tee -a ~/.profile <<< x", "echo x > .envrc",
    "cp hook .git/hooks/pre-commit", "chmod +x .git/hooks/post-checkout && echo x > .git/hooks/post-checkout",
])
def test_startup_files_and_git_hooks_writes_denied(ws, cmd):
    d = evaluate(ws, bash(cmd))
    assert not d.allow and "startup" in d.reason, cmd


@pytest.mark.parametrize("cmd", ["cat ~/.zshrc", "grep PATH ~/.bashrc", "ls .git/hooks"])
def test_reading_startup_files_allowed(ws, cmd):
    assert evaluate(ws, bash(cmd)).allow, cmd


@pytest.mark.parametrize("path", ["~/.zshrc", ".envrc", ".git/hooks/pre-push", "/home/u/.bash_profile"])
def test_file_tools_on_startup_files_denied(ws, path):
    d = evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}})
    assert not d.allow and "startup" in d.reason

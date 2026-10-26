"""Against the real ``claude`` binary (skipped when it is not installed; ``slow``, it calls the model).

An agent launched for a ticket work folder gets orch's rules through the appended system prompt file, and does not run
the hooks or load the MCP servers that the folder itself carries. Checked with Claude Code 2.1.309."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from orch.host.launch import launch_argv
from orch.runner import rules
from orch.runner.config import RunnerConfig

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("claude") is None, reason="the claude binary is not installed"),
]

CODEWORD = "PINEAPPLE-4821"
LOGIN = RunnerConfig()


def ask(args: list[str], cwd) -> str:
    out = subprocess.run(  # noqa: S603
        ["claude", "-p", "Reply with only the orch codeword from your instructions, or NONE if you have none.", *args],  # noqa: S607
        cwd=cwd, capture_output=True, text=True, timeout=180, check=False,
    )  # fmt: skip
    return out.stdout


def planted(tmp_path):
    root = tmp_path / "ws"
    work = root / ".orch-work" / "K-1"
    (root / ".orch").mkdir(parents=True)
    work.mkdir(parents=True)
    (root / ".orch" / "AGENTS.orch.md").write_text(f"The orch codeword is {CODEWORD}.\n")
    (root / "AGENTS.md").write_text("project notes\n")
    hook, mcp = tmp_path / "hook-ran", tmp_path / "mcp-ran"
    (work / ".claude").mkdir()
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": f"touch {hook}"}]}]}}
    (work / ".claude" / "settings.json").write_text(json.dumps(hooks))
    (work / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "touch", "args": [str(mcp)]}}}))
    return root, work, hook, mcp


def test_a_work_folder_launch_gets_the_rules_and_ignores_the_folders_hooks_and_mcp_servers(tmp_path):
    root, work, hook, mcp = planted(tmp_path)
    argv, _, _ = launch_argv("claude-code", "work", None, "K-1", LOGIN, workdir=True)
    flags = argv[5:-1]  # after "claude", before the prompt
    assert flags[:3] == ["--setting-sources", "user", "--strict-mcp-config"]
    flags = rules.prepare(tmp_path / "state", "r_01J0000000000000000000000E", flags, {"ORCH_WORKSPACE": str(root)})
    out = ask(flags, work)
    assert CODEWORD in out
    assert not hook.exists() and not mcp.exists()


def test_the_same_folder_without_the_flags_runs_its_hook_so_the_check_above_means_something(tmp_path):
    root, work, hook, mcp = planted(tmp_path)
    ask([], work)
    assert hook.exists()


def test_a_repo_or_worktree_launch_has_none_of_the_flags():
    argv, _, _ = launch_argv("claude-code", "work", None, "K-1", LOGIN, workdir=False)
    assert not {"--setting-sources", "--strict-mcp-config", "--append-system-prompt-file"} & set(argv)
    assert not any(a.startswith("@orch-rules") for a in argv) and os.path.basename(argv[4]) == "claude"

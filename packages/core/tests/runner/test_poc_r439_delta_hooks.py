"""Regression for the Opus delta PoC (#439 D1, 939672d1..f93467c5): git >= 2.54 config-based hooks
(``hook.<name>.command`` + ``hook.<name>.event``) in the repo's OWN config run despite ``-c core.hooksPath=/dev/null``.
The gate's allow-list refuses the ``hook`` section (it is not on it), so the hook never runs. Each test asserts the
safe outcome (refusal, no execution)."""

from __future__ import annotations

import subprocess

import pytest
from test_wtgit_confine import _git, world  # noqa: F401

from orch import gitsafe
from orch.runner import wtgit
from orch.runner.supervise import SpawnError


def _supports_config_hooks() -> bool:
    out = subprocess.run(["git", "help", "-c"], capture_output=True, text=True, check=False).stdout
    return "hook.<friendly-name>.command" in out


pytestmark = pytest.mark.skipif(not _supports_config_hooks(), reason="git without config-based hooks")


def test_worktree_add_refuses_a_repo_local_config_hook(world):  # noqa: F811
    root, repo, own, args = world
    marker = root / "hooked"
    _git(repo, "config", "hook.evil.command", f"touch {marker}")
    _git(repo, "config", "hook.evil.event", "post-checkout")
    a = args("worktree.add")
    a["base"] = _git(repo, "rev-parse", "HEAD")
    try:
        wtgit.run_git(a, own)
    except SpawnError:
        pass
    assert not marker.exists(), "repo-local hook.<name>.command ran on worktree add (core.hooksPath override bypassed)"


def test_gate_refuses_hook_section(world):  # noqa: F811
    root, repo, own, args = world
    _git(repo, "config", "hook.evil.command", "true")
    _git(repo, "config", "hook.evil.event", "post-checkout")
    with pytest.raises(gitsafe.Unsafe):
        gitsafe.gate(str(repo), own)

"""Fixtures of the Landing git tests: real git repositories in temp dirs, with no user or system config."""

from __future__ import annotations

import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

from orch.land import key as _key  # noqa: E402
from tests.gitread.conftest import make_repo  # noqa: F401  (a fixture, used by name)


def dev_git() -> tuple[str, str]:
    """The test machine's own git and exec path (a developer Mac's is not root-owned: the real pin refuses it)."""
    git = shutil.which("git") or "/usr/bin/git"
    out = subprocess.run([git, "--exec-path"], capture_output=True, text=True, check=True).stdout.strip()
    return git, out


@pytest.fixture(autouse=True)
def _dev_pinned_git(monkeypatch):
    """Landing tests run on the machine's git: :func:`orch.land.key.pinned_git` is the test seam (its real resolution
    is :func:`orch.land.key.resolve_git`, tested on its own)."""
    monkeypatch.setattr(_key, "pinned_git", dev_git)

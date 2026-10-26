"""scripts/dev/deploy-edge-host.sh refuses unsafe states before it builds or touches any host (D12)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "dev" / "deploy-edge-host.sh"


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def clone(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "scripts" / "dev").mkdir(parents=True)
    shutil.copy(SCRIPT, work / "scripts" / "dev" / "deploy-edge-host.sh")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "origin", "develop")
    return work


def _run(work: Path, **env):
    import os

    return subprocess.run(
        ["bash", str(work / "scripts" / "dev" / "deploy-edge-host.sh")],
        cwd=work,
        capture_output=True,
        text=True,
        env={**os.environ, "ORCH_DEPLOY_DRY_RUN": "1", "ORCH_DEPLOY_HOST": "invalid.invalid", **env},
    )


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_dirty_tree_is_refused(clone):
    (clone / "x.txt").write_text("x")
    r = _run(clone)
    assert r.returncode == 1
    assert "working tree is dirty" in r.stderr


def test_unpushed_head_is_refused(clone):
    (clone / "y.txt").write_text("y")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "local only")
    r = _run(clone)
    assert r.returncode == 1
    assert "is not on any origin branch" in r.stderr


def test_pushed_clean_head_passes_the_checks(clone):
    # no plugins/orch-core in the fixture: the build step (after the checks) is what stops it
    r = _run(clone)
    assert "dirty" not in r.stderr
    assert "not on any origin branch" not in r.stderr

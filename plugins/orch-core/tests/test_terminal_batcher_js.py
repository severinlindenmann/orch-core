"""Runs tests/js/terminal_batcher.js (plain node) against static/terminal.js: the remote key batcher."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_terminal_key_batcher():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "terminal_batcher.js"),
                        str(ROOT / "src" / "orch" / "dashboard" / "static" / "terminal.js")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().endswith("ok"), r.stdout + r.stderr  # a promise that never settles exits 0 silently

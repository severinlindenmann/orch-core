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


def test_only_a_button_asks_for_a_size_explicitly():
    """The sizer is tested in node; this pins its wiring: the page opening and every resize, rotation and keyboard
    event call it with no argument (read-only through a host), only the view and zoom buttons say it is explicit."""
    src = (ROOT / "src" / "orch" / "dashboard" / "static" / "terminal.js").read_text()
    assert src.count("sizeTmux(true)") == 2 and src.count("sizeTmux()") == 2
    assert "layout(); sizeTmux(); }, 150" in src
    assert "sizeTmux(); // fit the session" in src
    assert "remote: Boolean(host.remote)," in src  # the sizer is read-only exactly when a host serves the page

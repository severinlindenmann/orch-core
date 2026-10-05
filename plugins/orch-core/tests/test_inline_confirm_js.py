"""Runs tests/js/inline_confirm.js (plain node, a tiny fake DOM, no packages) against static/app.js: the inline
confirm never confirms on a held key or a double click, Esc and the timeout revert. Skipped where node is missing."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_inline_confirm_js():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "inline_confirm.js"),
                        str(ROOT / "src" / "orch" / "dashboard" / "static" / "app.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "inline confirm ok" in r.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_charter_confirm_label_follows_restored_fields():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "charter_confirm.js"),
                        str(ROOT / "src" / "orch" / "dashboard" / "static" / "app.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "charter confirm ok" in r.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_factory_forms_follow_the_mode_and_send_once():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "factory_forms.js"),
                        str(ROOT / "src" / "orch" / "dashboard" / "static" / "app.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "factory forms ok" in r.stdout

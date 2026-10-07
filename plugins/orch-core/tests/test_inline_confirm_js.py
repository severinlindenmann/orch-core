"""The confirm forms' helpers in static/app.js (plain node, a tiny fake DOM, no packages): the epic and New ticket forms
keep their shown fields in step. The inline two-step confirm these tests once drove is gone: every confirmation is the
one dialog in confirm.js (tests/test_confirm_dialog_js.py), where Cancel has focus first, so a held Enter or a double
press cancels instead of confirming. Skipped where node is missing."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_no_form_arms_in_place_any_more():
    templates = ROOT / "src" / "orch" / "dashboard" / "templates"
    for path in templates.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        if path.name != "design.html":
            assert not re.search(r"data-inline-confirm=|data-dialog=|data-charter-confirm=", text), path.name
    js = (ROOT / "src" / "orch" / "dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "data-inline-confirm" not in js and "dialogConfirmed" not in js and "form[data-armed]" not in js


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

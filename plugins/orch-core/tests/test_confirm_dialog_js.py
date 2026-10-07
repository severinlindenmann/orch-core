"""The one confirm dialog (static/confirm.js): tests/js/confirm_dialog.js runs it with plain node and a small fake DOM
(no packages). The first submit never goes out, Cancel/Esc/backdrop send nothing, the typed words and a required
reason are filled only after the person confirms. Skipped where node is missing."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "orch" / "dashboard" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_confirm_dialog_js():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "confirm_dialog.js"), str(STATIC / "confirm.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "confirm dialog ok" in r.stdout


def test_no_browser_popups_anywhere():
    """window.confirm/alert/prompt are never used: every confirmation is the dialog."""
    import re
    for path in [*STATIC.glob("*.js"), *(ROOT / "src" / "orch" / "dashboard" / "templates").glob("*.html")]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\(", text), path.name


def test_layout_loads_the_dialog_before_app_js():
    """confirm.js runs in <head>, before app.js: its capture listener sees a submit first, and html.confirm-js hides
    the no-JS fallbacks from the first paint."""
    layout = (ROOT / "src" / "orch" / "dashboard" / "templates" / "layout.html").read_text(encoding="utf-8")
    assert layout.index("static_url('confirm.js')") < layout.index("static_url('app.js')")
    assert "confirm.js') }}\" defer" not in layout
    assert "html.confirm-js .nojs-only" in (STATIC / "confirm.css").read_text(encoding="utf-8")


def test_every_dialog_names_a_short_specific_action():
    """Each form that asks names its action in a few words (never OK or Confirm); the care tone sits on outward or
    hard-to-undo actions; Grant once answers at once."""
    import re
    templates = ROOT / "src" / "orch" / "dashboard" / "templates"
    seen = 0
    for path in templates.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<form\b[^>]*data-confirm-title=[^>]*>", text):
            seen += 1
            ok = re.search(r'data-confirm-ok="((?:[^"{]|\{\{.*?\}\})*)"', tag)
            assert ok, (path.name, tag[:120])
            for label in re.findall(r"'([^']+)'", ok.group(1)) or [ok.group(1)]:
                words = re.sub(r"\{\{.*?\}\}", "X", label).split()
                assert 1 <= len(words) <= 5 and label.lower() not in ("ok", "confirm"), (path.name, label)
    assert seen >= 20
    permits = (templates / "_permits.html").read_text(encoding="utf-8")
    assert '{% if scope == "epic" %} data-confirm-title="Grant this command for the whole epic?"' in permits
    assert 'data-confirm-ok="Grant for this epic" data-confirm-tone="care"' in permits
    assert 'data-confirm-title="Add this command to the Dark profile?"' in permits
    skip = permits.split('data-confirm-title="Close {{ eid }} without releasing?"', 1)[1].split("</form>", 1)[0]
    assert 'data-confirm-field="skip_release"' in skip and 'data-confirm-tone="care"' in skip
    assert '<label class="nojs-only">Why close it without releasing <input name="skip_release" required' in skip
    run = (templates / "factory_run.html").read_text(encoding="utf-8")
    assert 'data-confirm-title="Stop the run?"' in run and 'data-confirm-ok="Stop the run"' in run
    assert "'Release to production' if s.name == 'production'" in run

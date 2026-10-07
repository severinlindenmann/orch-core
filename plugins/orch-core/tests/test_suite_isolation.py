"""Keepers for the suite's own isolation (conftest points every config dir at a temp dir)."""
from pathlib import Path

FORBIDDEN = "monkeypatch" + ".undo()"  # split so this file does not match itself


def test_no_test_calls_monkeypatch_undo():
    """undo() drops conftest's isolated ORCH_STATE_DIR, XDG_CONFIG_HOME and CLAUDE_CONFIG_DIR too, so the rest of
    the test would read and write the developer's real ~/.config/orch. Use `with monkeypatch.context() as m:`."""
    tests = Path(__file__).parent
    hits = [f"{p.name}:{n}" for p in sorted(tests.rglob("*.py"))
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if FORBIDDEN in line and not line.lstrip().startswith("#") and "never " + FORBIDDEN not in line]
    assert hits == [], f"use monkeypatch.context() instead: {hits}"

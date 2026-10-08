"""A terminal tile's screen is inline spans (one per colour run). In a flex or grid container every span becomes its
own line, which showed a pane's footer one phrase per line (found on a live Terminals page). Keep it a plain block."""
import re
from pathlib import Path

CSS = Path(__file__).parent.parent / "src" / "orch" / "dashboard" / "static" / "app.css"


def test_a_tile_screen_is_a_plain_block():
    rules = re.findall(r"\.term-(?:tail|screen)\b[^{]*\{([^}]*)\}", CSS.read_text(encoding="utf-8"))
    assert rules, "the tile screen rules are gone: update this test"
    for body in rules:
        assert not re.search(r"display:\s*(?:inline-)?(?:flex|grid)", body), body

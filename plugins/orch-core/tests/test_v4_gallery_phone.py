"""M: the /design gallery shows the v4 core primitives, and the phone width follows Phone4 (journey as five segments,
Your move as a list of rows, the artifacts grid)."""
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

CSS = Path(__file__).resolve().parents[1] / "src" / "orch" / "dashboard" / "static" / "app.css"


def test_gallery_shows_the_v4_primitives(dash):
    html = dash.get("/design?theme=light").text
    prim = html.split('id="primitives"', 1)[1].split('id="states"', 1)[0]
    for marker in ('class="card ym-card"', 'class="col lane lane-rail"', 'class="lane-rail rail-done"',
                   'class="journey"', 'class="ev-tile ev-tile-missing"', 'class="card art-panel"'):
        assert marker in prim, marker
    for title in ("Your move card (Board strip)", "Rail lane and Done rail", "Journey bar", "Evidence tiles",
                  "Artifacts panel"):
        assert f"<h3>{title}</h3>" in prim


def test_phone_width_follows_phone4():
    css = CSS.read_text(encoding="utf-8")
    phone = css[css.index("Phone (Phone4): Your move"):]
    assert re.search(r"\.ym-card \.ym-key, \.ym-card \.ym-title, \.ym-card \.ym-body \{ display: none; \}", phone)
    assert re.search(r"\.ym-phone \{ display: inline-flex;[^}]*min-height: var\(--tap\)", phone)
    journey = css[css.index("Phone (Phone4): the journey as five segments"):]
    assert ".journey-caption { display: block; }" in journey
    assert re.search(r"\.art-grid \{ display: grid; grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)", css)


def test_no_new_colours_in_the_v4_css():
    css = CSS.read_text(encoding="utf-8")
    v4 = css[css.index("/* ---------- M (v4): Your move strip"):]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", v4)  # tokens only, never the drafts' inline hex

"""F: status icons are inline SVG from one sprite per page (design-system spec §3.0), never font glyphs, and every
icon is followed by words."""
import re

import pytest

pytest.importorskip("fastapi")

ROLES = ("ok", "info", "you", "warn", "err", "neu")
GLYPHS = "✓◐●▲✕○"


@pytest.mark.parametrize("url", ["/", "/board", "/board?view=list", "/activity", "/reports", "/workspace", "/new"])
def test_every_page_inlines_the_sprite_once(dash, url):
    html = dash.get(url).text
    assert html.count('<svg class="sprite"') == 1
    for role in ROLES:
        assert f'<symbol id="i-{role}"' in html
    assert html.index('<svg class="sprite"') < html.index('<main')


def test_status_chips_use_the_sprite_and_keep_their_words(dash, put):
    put("testing", sections={"Verification": "ok"}); put("open"); put("in-progress")
    for url in ("/", "/board", "/board?view=list"):
        html = dash.get(url).text
        chips = re.findall(r'<span class="chip chip-(ok|info|you|warn|err|neu)">(.*?)</span>', html, re.S)
        assert chips, url
        for role, inner in chips:
            assert inner.startswith(f'<svg class="i" aria-hidden="true"><use href="#i-{role}"/></svg> '), (url, inner)
            assert not any(g in inner for g in GLYPHS), (url, inner)
            assert re.sub(r"<[^>]+>", "", inner).strip(), (url, inner)  # words after the icon


def test_widget_chip_never_gets_the_you_icon():
    from orch.dashboard.views import TEMPLATES
    w = TEMPLATES.env.get_template("_widgets.html").module
    assert '#i-neu' in str(w.chip("you", "x")) and '#i-warn' in str(w.chip("warn", "x"))

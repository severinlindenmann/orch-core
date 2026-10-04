"""F: page states per the design system: stale is a quiet line with a warn icon (the data keeps full contrast), the
page says how old it is once live updates have been gone a minute, and inbox zero draws its check as an icon."""
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

STATIC = Path(__file__).resolve().parents[1] / "src" / "orch" / "dashboard" / "static"


def test_stale_line_is_quiet_and_never_dims_the_page(dash):
    html = dash.get("/").text
    assert '<div class="stale-banner" role="status"><svg class="i" aria-hidden="true"><use href="#i-warn"/></svg>' in html
    assert '<p class="page-age" role="status" hidden>' in html
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"body\.stale \.stale-banner, \.page-age:not\(\[hidden\]\) \{([^}]*)\}", css).group(1)
    assert "var(--muted)" in rule and "background" not in rule
    assert not re.search(r"body\.stale (main|\.content)[^{]*\{[^}]*opacity", css)  # data is never dimmed


def test_page_age_appears_after_a_minute_without_the_stream():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'source.addEventListener("error"' in js and "Date.now() - lostAt > 60000" in js
    assert 'querySelector(".page-age-n")' in js


def test_inbox_zero_check_is_an_icon(dash):
    html = dash.get("/").text
    if "inbox-zero" in html:
        assert '<span class="iz-mark"><svg class="i" aria-hidden="true"><use href="#i-ok"/></svg></span>' in html


def test_page_age_restarts_with_an_in_place_swap():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    swap = js[js.index("const swapIn = "):js.index("function go(href)")]
    assert "renderedAt = Date.now();" in swap
    assert "Date.now() - renderedAt" in js

"""F: the shell's navigation per the design-system Navigation component: a skip link first, counts with an
aria-label, a pink count that passes 4.5:1, a secondary New ticket, one footer block with a 32 px theme control,
and below 900 px a top bar whose Menu opens a <dialog> drawer (tests/js/menu_drawer.js drives early.js)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "orch" / "dashboard" / "static"


def test_skip_link_is_the_first_focusable_element(dash):
    html = dash.get("/board").text
    body = html[html.index("<body>") + len("<body>"):].lstrip()
    assert body.startswith('<a class="skip-link" href="#main">Skip to content</a>')
    assert '<main class="content" id="main" tabindex="-1">' in html


def test_counts_carry_an_aria_label(dash, put, monkeypatch):
    from orch.dashboard import views
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 3)
    put("testing", sections={"Verification": "ok"})
    put("testing", sections={"Verification": "ok"})
    html = dash.get("/board").text
    nav = html[html.index('<nav class="menu"'):html.index("</nav>")]
    assert '<span class="badge badge-hot" aria-label="2 decisions wait on you">2</span>' in nav
    assert '<span class="badge badge-warn" aria-label="3 setup items open">3</span>' in nav
    assert not re.search(r'<span class="badge[^"]*">', nav)  # every count is named


def test_pink_count_uses_the_you_chip_pair():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    rule = re.search(r"^\.badge-hot \{([^}]*)\}", css, re.M).group(1)
    assert "var(--you-bg)" in rule and "var(--you-fg)" in rule and "#" not in rule


def test_new_ticket_is_secondary_and_shows_where_you_are(dash):
    html = dash.get("/new").text
    assert re.search(r'<a class="btn menu-new" href="/new" aria-current="page">', html)
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.menu \.menu-new\[aria-current=page\] \{[^}]*--mint", css)


def test_unselected_items_are_muted_and_selected_is_bold_text(dash):
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.menu a\.item \{[^}]*color: var\(--muted\)", css)
    assert re.search(r"\.menu a\.item\[aria-current=page\] \{[^}]*color: var\(--text\); font-weight: 700", css)
    assert re.search(r"\.menu a\.item \{[^}]*min-height: var\(--control-h\)", css)  # 40, 44 on coarse pointers


def test_footer_is_one_block_with_a_compact_theme_switch(dash):
    html = dash.get("/").text
    ws_block = html[html.index('<div class="menu-ws">'):html.index("</form>", html.index('<div class="menu-ws">'))]
    assert 'class="theme-switch"' in ws_block and "menu-ws-name" in ws_block
    for value in ("light", "dark", "system"):
        assert f'<use href="#i-{value}"/>' in ws_block
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.theme-switch button \{[^}]*min-height: var\(--s-8\)", css)  # 32 px


def test_phone_top_bar_has_the_page_title_and_a_drawer(dash):
    html = dash.get("/activity").text
    assert '<span class="menu-page" aria-hidden="true">Activity</span>' in html
    assert re.search(r'<dialog class="menu-drawer overlay" aria-label="Menu">', html)
    assert 'aria-label="Close menu"' in html
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.menu-drawer \{[^}]*width: min\(var\(--aside-w\), 85vw\)", css)  # 320 px
    assert "@keyframes drawer-in" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_menu_drawer_traps_focus_and_returns_it():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "menu_drawer.js"), str(STATIC / "early.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "menu drawer ok" in r.stdout


def test_menu_has_no_wordmark_logo():
    """The menu shows the Mission Control icon and title only; no third-party wordmark or its theme rules."""
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".menu-brand .logo" not in css
    menu = (STATIC.parent / "templates" / "_menu.html").read_text(encoding="utf-8")
    assert 'class="logo' not in menu and "menu-icon" in menu


def test_setup_count_links_to_the_setup_tab(dash, monkeypatch):
    from orch.dashboard import views
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 2)
    assert '<a class="item" href="/workspace?tab=setup">' in dash.get("/").text
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    assert '<a class="item" href="/workspace">' in dash.get("/").text

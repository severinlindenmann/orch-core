import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

PAGES = ["/", "/board", "/board?view=list", "/new", "/workspace", "/activity", "/reports"]
TEMPLATES = Path(__file__).parents[1] / "src" / "orch" / "dashboard" / "templates"
STATIC = Path(__file__).parents[1] / "src" / "orch" / "dashboard" / "static"


@pytest.mark.parametrize("url", PAGES)
def test_every_page_renders_on_empty_workspace(dash, url):
    r = dash.get(url)
    assert r.status_code == 200
    assert "Mission Control" in r.text


@pytest.mark.parametrize("url,nav", [("/", "Today"), ("/board", "Board"), ("/workspace", "Workspace")])
def test_menu_marks_current_page(dash, url, nav):
    html = dash.get(url).text
    current = re.findall(r'<a[^>]*aria-current="page"[^>]*>(.*?)</a>', html, re.S)
    assert len(current) == 1 and nav in current[0]


def test_ticket_page_marks_board_current(dash, put):
    tid = put("open", title="Marked")
    html = dash.get(f"/t/{tid}").text
    current = re.findall(r'<a[^>]*aria-current="page"[^>]*>(.*?)</a>', html, re.S)
    assert len(current) == 1 and "Board" in current[0]


def test_old_board_filters_redirect(dash):
    r = dash.get("/?q=backup&type=bug", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/board?q=backup&type=bug"


@pytest.mark.parametrize("cookie,expected", [("dark", "dark"), ("light", "light"), ("<script>", "system"), (None, "system")])
def test_theme_from_cookie(dash, cookie, expected):
    if cookie is not None:
        dash.cookies.set("orch_theme", cookie)
    html = dash.get("/").text
    assert f'<html lang="en" data-theme="{expected}"' in html
    assert "<script>" not in html.split("<body", 1)[0].replace('<script src="/static/app.js" defer></script>', "")


def test_theme_config_default(configure, ws):
    from fastapi.testclient import TestClient
    from orch.core.workspace import Workspace
    from orch.dashboard.app import create_app
    configure(dashboard={"theme": "dark"})
    c = TestClient(create_app(Workspace.open(ws.root), "tok"))
    assert 'data-theme="dark"' in c.get("/?token=tok").text


def test_post_theme_sets_and_clears_cookie(dash):
    r = dash.post("/theme", data={"theme": "dark"}, headers={"referer": "http://testserver/board"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/board"
    assert "orch_theme=dark" in r.headers["set-cookie"] and "samesite=strict" in r.headers["set-cookie"].lower()
    r = dash.post("/theme", data={"theme": "system"}, follow_redirects=False)
    assert "orch_theme=system" in r.headers["set-cookie"] and "Max-Age=0" not in r.headers["set-cookie"]


def test_post_theme_keeps_referer_query(dash):
    r = dash.post("/theme", data={"theme": "light"}, headers={"referer": "http://testserver/board?q=x&type=bug"},
                  follow_redirects=False)
    assert r.headers["location"] == "/board?q=x&type=bug"


def test_post_theme_ignores_foreign_referer(dash):
    r = dash.post("/theme", data={"theme": "light"}, headers={"referer": "http://evil.example/board?q=x"},
                  follow_redirects=False)
    assert r.headers["location"] == "/"


@pytest.mark.parametrize("cookie,expected", [("system", "system"), (None, "dark"), ("light", "light")])
def test_cookie_overrides_config_default(configure, ws, cookie, expected):
    from fastapi.testclient import TestClient
    from orch.core.workspace import Workspace
    from orch.dashboard.app import create_app
    configure(dashboard={"theme": "dark"})
    c = TestClient(create_app(Workspace.open(ws.root), "tok"))
    c.get("/?token=tok")
    if cookie is not None:
        c.cookies.set("orch_theme", cookie)
    html = c.get("/").text
    assert f'data-theme="{expected}"' in html
    pressed = re.findall(r'value="(\w+)" aria-pressed="true"', html)
    assert pressed == [expected]


def test_focus_token_in_every_theme_block():
    tokens = (STATIC / "tokens.css").read_text(encoding="utf-8")
    light = re.search(r"\n\[data-theme=light\] \{([^}]*)\}", tokens).group(1)
    dark = re.search(r"\n\[data-theme=dark\] \{([^}]*)\}", tokens).group(1)
    system = re.search(r":root:not\(\[data-theme=light\]\), \[data-theme=system\] \{([^}]*)\}", tokens).group(1)
    assert "--focus: #15171A" in light and "--focus: #00CC99" in dark and "--focus: #00CC99" in system
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r":focus-visible \{[^}]*outline: 2px solid var\(--focus\)", css)
    assert "solid var(--mint)" not in css


def test_only_explicit_primary_buttons_are_mint():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert "button:not([class])" not in css


def test_locked_page_follows_os_theme(ws):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    assert '<html lang="en" data-theme="system">' in TestClient(create_app(ws, "tok")).get("/").text


def test_post_theme_rejects_unknown_value(dash):
    r = dash.post("/theme", data={"theme": "neon"}, follow_redirects=False)
    assert r.status_code == 303 and "orch_theme" not in r.headers.get("set-cookie", "")


def test_brand_none_hides_logo(configure, ws):
    from fastapi.testclient import TestClient
    from orch.core.workspace import Workspace
    from orch.dashboard.app import create_app
    configure(dashboard={"brand": "none"})
    html = TestClient(create_app(Workspace.open(ws.root), "tok")).get("/?token=tok").text
    # brand: none shows the plain (peak-less) icon and favicon (spec §8) and no wordmark logo.
    assert "/static/brand/mc-icon-plain.svg" in html and 'class="logo' not in html and "Mission Control" in html


def test_brand_default_is_none(dash):
    html = dash.get("/").text
    assert "/static/brand/mc-icon-plain.svg" in html and "/static/brand/mc-favicon-plain.svg" in html


def test_customer_name_is_escaped(configure, ws):
    from fastapi.testclient import TestClient
    from orch.core.workspace import Workspace
    from orch.dashboard.app import create_app
    configure(customer='<b>acme&"')
    html = TestClient(create_app(Workspace.open(ws.root), "tok")).get("/?token=tok").text
    assert "<b>acme" not in html and "&lt;b&gt;acme&amp;" in html


def test_no_external_urls_in_templates_and_static():
    pattern = re.compile(r"https?://(?!testserver)", re.I)
    for path in [*TEMPLATES.glob("*.html"), STATIC / "app.css", STATIC / "app.js"]:
        assert not pattern.search(path.read_text(encoding="utf-8")), path


def test_every_template_extends_layout():
    for path in TEMPLATES.glob("*.html"):
        # preview.html is `orch addon preview`'s standalone file: no menu, no script, no server (#251)
        if path.name in ("layout.html", "preview.html") or path.name.startswith("_"):
            continue
        assert path.read_text(encoding="utf-8").lstrip().startswith('{% extends "layout.html" %}'), path


def test_fonts_and_logos_are_served(dash):
    css = dash.get("/static/app.css").text
    for font in re.findall(r"url\(['\"]?(/static/fonts/[^)'\"]+)", css):
        assert dash.get(font).status_code == 200
    assert dash.get("/static/brand/mc-icon.svg").status_code == 200


def test_early_script_marks_js_before_render(dash):
    html = dash.get("/").text
    head = html[:html.index("</head>")]
    assert re.search(r'<script src="/static/early\.js\?v=[0-9a-f]+"></script>', head)  # not deferred: runs before the body renders
    assert head.index("/static/early.js") < head.index("/static/app.css")
    early = dash.get("/static/early.js")
    assert early.status_code == 200
    assert early.text.startswith("//") and 'document.documentElement.classList.add("js");' in early.text
    # the phone-menu toggle lives with the class, so a failed app.js never leaves the menu hidden
    assert "menu.classList.toggle(\"expanded\")" in early.text
    assert 'toggle("expanded")' not in (STATIC / "app.js").read_text(encoding="utf-8")


def test_phone_menu_collapses_by_css_not_after_load():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "html.js .menu-more:not(.expanded) > .menu-body { display: none; }" in css
    assert "menu.open =" not in js  # no post-load collapse (that was the flash)
    # without JS the <details> stays open in the HTML
    assert '<details class="menu-more" open>' in (TEMPLATES / "_menu.html").read_text(encoding="utf-8")


def test_shell_background_fills_the_sidebar_column():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    shell = re.search(r"^\.shell \{[^}]*\}", css, re.M).group(0)
    assert "linear-gradient" in shell and "248px" in shell


def test_figtree_licence_first_line_is_clean():
    first = (STATIC / "fonts" / "OFL-Figtree.txt").read_text(encoding="utf-8").splitlines()[0]
    assert first == "Copyright 2022 The Figtree Project Authors (https://github.com/erikdkennedy/figtree)"

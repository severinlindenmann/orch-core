import re

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from orch.dashboard.app import create_app
from orch.dashboard.markdown import render_markdown


def test_locked_without_token(ws):
    c = TestClient(create_app(ws, "tok"))
    for url in ("/", "/?token=wrong", "/?token=t%C3%B6k"):
        r = c.get(url)
        assert r.status_code == 401 and "Locked" in r.text
    assert c.get("/static/app.css").status_code == 200


@pytest.mark.parametrize("method,url", [
    ("GET", "/events"), ("GET", "/a/x/y"), ("GET", "/addons/x"), ("POST", "/t/L-0001/comment"),
])
def test_every_route_is_locked_without_token(ws, put, method, url):
    from orch.core import store
    tid = put("open")
    assert tid == "L-0001"
    before = store.resolve(ws, tid).path.read_text(encoding="utf-8")
    r = TestClient(create_app(ws, "tok")).request(method, url, data={"text": "sneaky"} if method == "POST" else None)
    assert r.status_code == 401 and "Locked" in r.text
    assert store.resolve(ws, tid).path.read_text(encoding="utf-8") == before


def test_login_redirect_keeps_other_query_params(ws):
    c = TestClient(create_app(ws, "tok"))
    r = c.get("/?q=backup&token=tok&type=bug", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?q=backup&type=bug"


def test_token_sets_cookie_and_drops_query(ws):
    c = TestClient(create_app(ws, "tok"))
    r = c.get("/?token=tok", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "orch_token=tok" in cookie and "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()


def test_cross_origin_post_refused(dash, ws, put):
    from orch.core import store
    tid = put("open")
    before = store.resolve(ws, tid).path.read_text(encoding="utf-8")
    r = dash.post(f"/t/{tid}/comment", data={"text": "from evil"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert store.resolve(ws, tid).path.read_text(encoding="utf-8") == before
    assert dash.post(f"/t/{tid}/comment", data={"text": "from us"}, headers={"origin": "http://testserver"}).status_code == 200


def test_board_shows_columns_and_needs(dash, put):
    put("open", title="Backup nightly config")
    put("testing", title="Check export")
    r = dash.get("/board")
    assert r.status_code == 200
    assert "Backup nightly config" in r.text and 'id="col-in-progress"' in r.text
    # M: the Your move strip on top holds every ticket whose move is the human's (the testing one here)
    needs = r.text.split('id="your-move"', 1)[1].split('<div class="flow"', 1)[0]
    assert "Check export" in needs and "Backup nightly config" not in needs
    assert '<span class="chip chip-you"><svg class="i" aria-hidden="true"><use href="#i-you"/></svg> Verdict</span>' in r.text  # the testing card says what it needs
    assert '<span class="badge badge-hot" aria-label="1 decision waits on you">1</span>' in r.text  # the Decisions badge in the menu


def test_board_filters(dash, put):
    put("open", title="Alpha", sections={"Ask": "needle"})
    put("open", title="Beta")
    r = dash.get("/board?q=needle")
    assert "Alpha" in r.text and "Beta" not in r.text
    assert "Alpha" not in dash.get("/board?type=bug").text


def test_done_is_collapsed(dash, put):
    put("done", title="Old work")
    assert "Old work" not in dash.get("/board").text
    assert "Old work" in dash.get("/board?show_done=1").text


def test_show_done_link_keeps_filters(dash, put):
    put("done", title="Old work", labels=["ops"])
    body = dash.get("/board?q=old&type=feature&priority=normal&label=ops").text
    assert 'href="?q=old&amp;type=feature&amp;priority=normal&amp;label=ops&amp;show_done=1"' in body


def test_markdown_is_safe():
    html = render_markdown("<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n- [ ] todo\n- [x] done\n\n"
                           "![s](../../artifacts/L-0001/s.png)")
    assert "<script>" not in html and 'href="javascript' not in html
    # a ticket-relative image path is a link to the file; only a linked artifact of the page's ticket shows inline
    assert "☐ todo" in html and "☑ done" in html and 'href="/a/L-0001/s.png"' in html and "<img" not in html


def test_board_calls_needs_you_once(dash, put, monkeypatch):
    from orch.core import query
    put("testing", title="Check export")
    calls = []
    original = query.needs_you

    def counting(ws, **kw):
        calls.append(1)
        return original(ws, **kw)

    monkeypatch.setattr(query, "needs_you", counting)
    assert dash.get("/board").status_code == 200
    assert len(calls) == 1


def test_serve_wires_uvicorn(ws_root, monkeypatch, capsys):
    import uvicorn
    from orch import actor
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    from orch.cli import run
    seen = {}

    def fake_run(self, sockets=None):  # the bound socket is handed over, not host and port
        seen.update(host=self.config.host, port=sockets[0].getsockname()[1])
        sockets[0].close()

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    assert run(["serve", "--no-open", "--port", "9999"]) == 0
    assert seen == {"host": "127.0.0.1", "port": 9999}
    assert "http://127.0.0.1:9999/?token=" in capsys.readouterr().out


@pytest.mark.parametrize("meta", [
    {"external": 5},
    {"external": "JIRA-1"},
    {"external": {"key": "JIRA-1"}},
    {"claim": "someone"},
    {"claim": ["x"]},
    {"labels": 5},
    {"labels": "ops"},
])
def test_board_survives_odd_frontmatter(dash, put, meta):
    put("open", title="Odd one", **meta)
    put("open", title="Normal one", labels=["ops"])
    for url in ("/board", "/board?label=ops"):
        r = dash.get(url)
        assert r.status_code == 200 and "Normal one" in r.text


def test_layout_css_fits_small_screens(dash):
    css = dash.get("/static/app.css").text
    for rule in (".grid-2 { grid-template-columns: minmax(0, 1fr); }", "overflow-wrap: anywhere",
                 "table { display: block; overflow-x: auto; max-width: 100%; }", "input[type=file] { max-width: 100%; }",
                 "flex-wrap: wrap"):
        assert rule in css, rule
    assert ".page-head {" in css and "flex-wrap: wrap" in css.split(".page-head {", 1)[1].split("}", 1)[0]
    phone = css.split("@media (max-width: 900px) {", 1)[1]
    menu_bar = phone.split(".menu {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap" in menu_bar and "flex-direction: row" in menu_bar


def test_board_card_shows_open_blockers(dash, put):
    first = put("in-progress", title="Blocker")
    middle = put("open", title="Middle", blocked_by=[first])
    last = put("open", title="Last", blocked_by=[middle])
    done_blocker = put("done", title="Finished")
    free = put("open", title="Free", blocked_by=[done_blocker])
    html = dash.get("/board").text

    def card(tid):
        return re.search(rf'<a class="card tcard" href="/t/{tid}".*?</a>', html, re.S).group(0)

    badge = '<span class="chip chip-warn"><svg class="i" aria-hidden="true"><use href="#i-warn"/></svg> Blocked by {}</span>'  # the move chip
    assert badge.format(first) in card(middle)
    assert badge.format(middle) in card(last)
    assert "Blocked by" not in card(free)  # a done blocker no longer blocks
    assert "Blocked by" not in card(first)


def test_board_search_is_trimmed(dash, hops):
    t = hops.new("Retry the load")
    assert t.id in dash.get("/board?view=list&q=%20retry%20").text


def test_unknown_status_filter_says_so(dash, hops):
    hops.new("Some ticket")
    html = dash.get("/board?view=list&status=nope").text
    assert "Unknown status filter" in html and "showing all tickets" in html
    assert "Unknown status filter" not in dash.get("/board?view=list&status=done").text

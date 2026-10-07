import re
import subprocess
from datetime import datetime, timezone

import pytest

from addon_fixtures import loaded
from orch.addons import cache
from orch.addons.api import PendingDecision, Snapshot
from orch.addons.loader import AddonRegistry
from orch.addons.widgets import KV, Badge, Card, Countdown, MenuStatus, Link, Search, Table, Text, Tile, Action, QR

XSS = "<script>alert(1)</script>"
OVER = {"capabilities": ["provider", "page", "settings", "panel", "decisions"],
        "slots": ["today.summary", "today.from_addons", "ticket.code", "board.external"],
        "menu": {"title": "Demo status", "icon": "status"},
        "actions": [{"id": "rerun", "label": "Rerun failed", "confirm": "Rerun the failed checks?"}]}
ORIGIN = {"origin": "http://testserver"}


class Demo:
    def __init__(self):
        self.providers, self.resolved, self.acted = [], [], []
        self.mode = "ok"

    def widgets(self, slot, view):
        if self.mode == "raise":
            raise RuntimeError("render boom")
        if self.mode == "html":
            return ["<b>raw</b>"]
        if self.mode == "run":
            self.ctx.run(["git", "status"])  # the addon's own ProviderContext: refused during a render
        if self.mode == "empty":
            return []
        if self.mode == "qr":
            return [QR("hello", "cap")]
        if self.mode == "params":
            if slot == "today.summary":
                return []
            seen = Text("params:" + "|".join(f"{k}={v}" for k, v in sorted(view.params.items())))
            return [Search("q", view.params.get("q", ""), "Filter repos"), seen] if slot.startswith("page.") else [seen]
        if slot == "today.summary":
            return [Tile("Checks failing", None, "err", href="/addons/demo/")]
        rows = tuple((i["label"], Badge(i["role"], i["text"])) for s in view.snapshots() for i in s.items)
        body = (KV(rows),) if rows else (Text("No data yet"),)
        if slot == "ticket.code" and view.ticket is not None:
            body = (Text(f"PR for {view.ticket.id}"),)
        return [Card(f"Status {XSS}", body + (Table(("Repo", "Link"), (("a/b", Link(XSS, "https://example.com")),)),
                                               Action("rerun", "Rerun failed", "a/b#1")))]

    chip = None

    def menu_badge(self, view):
        if self.chip == "raise":
            raise RuntimeError("badge boom")
        return self.chip

    def decisions(self, view):
        return [PendingDecision("phone/1", "Answer from phone", "ISO 8601", ticket="L-0001"),
                PendingDecision("phone/2", "Old answer", stale=True)]

    def resolve(self, decision_id, choice, ctx):
        self.resolved.append((decision_id, choice, type(ctx).__name__))
        return "Applied"

    def act(self, action_id, target, ctx):
        self.acted.append((action_id, target))
        return "Rerun started"


@pytest.fixture
def demo(ws):
    obj = Demo()
    la = loaded(ws, obj, **OVER)
    obj.ctx = la.ctx.provider_context()
    ws._addons = AddonRegistry(ws, {"demo": la})
    return obj


@pytest.fixture
def client(ws, demo, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)  # doctor shells out to git; not an addon
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _banner(html):
    """The text of the addon health banner element (ruling F8: asserts look inside it, not the whole page)."""
    start = html.index('<p class="addon-banner')
    end = html.index("</div>", start) if html.rfind('<div class="callout', 0, start) > html.rfind("</div>", 0, start) else html.index("</p>", start)
    return html[start:end]


def _snap(health="ok", items=None, **kw):
    items = items if items is not None else ({"id": "b", "label": "Branch", "role": "info", "text": "main"},)
    return Snapshot("fake", "harness", datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc), health=health, items=items, **kw)


def test_menu_lists_the_addon_page(client):
    html = client.get("/").text
    assert "menu-addons" in html and 'href="/addons/demo/"' in html and "Demo status" in html


@pytest.mark.parametrize("role", ["ok", "warn", "err", "neu"])
def test_menu_badge_renders_a_chip_with_its_role_and_tooltip(client, demo, role):
    demo.chip = Badge(role, "39 %", title="5-hour 30 % · week 39 %")
    for url in ("/", "/addons/demo/"):
        html = client.get(url).text
        assert f'<span class="chip chip-{role}" title="5-hour 30 % · week 39 %" aria-label="Demo status: 5-hour 30 % · week 39 %">39 %</span>' in html


@pytest.mark.parametrize("chip", [None, "raise", "39 %", Badge("you", "x"), Badge("ok", " ")])
def test_menu_badge_none_or_broken_is_no_chip_and_the_page_still_renders(client, demo, chip):
    demo.chip = chip
    r = client.get("/addons/demo/")
    assert r.status_code == 200 and 'class="chip chip-' not in r.text.split('class="menu-addons"')[1].split("</nav>")[0]


def _menu(html):
    return html.split('class="menu-addons"')[1].split("</div>")[0]


def test_menu_status_renders_chip_and_one_status_line(client, demo):
    from datetime import timedelta
    until = (datetime.now(timezone.utc) + timedelta(hours=3, minutes=5, seconds=30)).isoformat()
    demo.chip = MenuStatus(Badge("warn", "75 %"), (Text("5 h"), Badge("ok", "35 %"), Text("· resets"), Countdown(until)))
    for url in ("/", "/addons/demo/"):
        m = _menu(client.get(url).text)
        assert 'class="chip chip-warn"' in m and ">75 %</span>" in m
        assert '<span class="menu-line">' in m and '<span class="ml-ok">35 %</span>' in m and "· resets" in m
        assert f'data-until="{until}"' in m and "3h05" in m


def test_countdown_text_future_past_and_bad_until():
    from orch.addons.widgets import countdown_text
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    assert countdown_text("2026-10-04T15:05:30Z", "reset", now) == "3h05"
    assert countdown_text("2026-10-04T12:12:30+00:00", "reset", now) == "12 min"
    assert countdown_text("2026-10-04T11:59:00Z", "done!", now) == "done!"
    assert countdown_text("nonsense", "reset", now) is None and countdown_text("2026-10-04T15:00:00", "r", now) is None


def test_menu_status_drops_bad_parts_and_plain_badge_still_works(client, demo):
    demo.chip = MenuStatus(None, (Text("ok"), "raw", Text(" "), Countdown("nope"), Badge("you", "x"), Countdown("2020-01-01T00:00:00Z", "gone")))
    m = _menu(client.get("/addons/demo/").text)
    assert 'class="chip' not in m and "ok" in m and "gone" in m and "nope" not in m and "raw" not in m and "ml-you" not in m
    demo.chip = Badge("ok", "5 %")
    assert 'chip chip-ok' in _menu(client.get("/").text) and "menu-line" not in _menu(client.get("/").text)
    demo.chip = "raise"
    assert client.get("/addons/demo/").status_code == 200


def test_addon_page_renders_widgets_escaped_with_core_csp(client, ws):
    from orch.dashboard.app import PAGE_CSP
    cache.write_snapshot(ws, "demo", _snap())
    r = client.get("/addons/demo/")
    assert r.status_code == 200 and r.headers["content-security-policy"] == PAGE_CSP
    assert XSS not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "Branch" in r.text and "main" in r.text and "<h1>Demo status</h1>" in r.text
    assert 'action="/addons/demo/actions/rerun"' in r.text and 'data-confirm-title="Rerun the failed checks?"' in r.text
    assert 'action="/addons/demo/refresh"' in r.text


def test_qr_widget_renders_as_an_inline_svg(client, demo):
    demo.mode = "qr"
    html = client.get("/addons/demo/").text
    assert "<svg" in html and 'role="img"' in html and 'aria-label="cap"' in html
    start = html.index("<svg")
    end = html.index("</svg>", start)
    assert "hello" not in html[start:end]


def test_never_fetched_banner(client):
    html = client.get("/addons/demo/").text
    assert "Not fetched yet" in _banner(html)


def test_stale_page_keeps_rows_and_shows_banner(client, ws):
    cache.write_snapshot(ws, "demo", _snap("stale", message="fetch failed: offline"))
    html = client.get("/addons/demo/").text
    banner = _banner(html)
    assert "Last updated" in banner and "fetch failed: offline" in banner and 'href="#i-warn"' in banner
    assert "main" in html[html.index("addon-page"):]  # the previous rows are still listed


@pytest.mark.parametrize("health, text", [("offline", "Offline"), ("auth_required", "Login needed"),
                                           ("error", "Fetch failed"), ("rate_limited", "Rate limited · resets")])
def test_banner_per_health(client, ws, health, text):
    cache.write_snapshot(ws, "demo", _snap(health, retry_after=datetime(2026, 10, 2, 9, 5, tzinfo=timezone.utc)
                                          if health == "rate_limited" else None))
    assert text in _banner(client.get("/addons/demo/").text)


def test_unknown_page_is_404(client):
    assert client.get("/addons/nope/").status_code == 404
    assert client.get("/addons/demo", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("mode, needle", [("raise", "could not render"), ("html", "returned an invalid widget"),
                                          ("run", "could not render")])
def test_bad_addon_output_never_breaks_the_page(client, demo, ws, mode, needle):
    demo.mode = mode
    r = client.get("/addons/demo/")
    assert r.status_code == 200 and needle in r.text and "<b>raw</b>" not in r.text
    log = (ws.state_dir / "addon-errors.log").read_text(encoding="utf-8")
    if mode == "run":
        assert "while a page renders" in log


def test_no_subprocess_or_socket_during_any_render(client, ws, put, monkeypatch):
    import socket
    tid = put("open")
    cache.write_snapshot(ws, "demo", _snap())

    def refuse(*a, **k):
        raise AssertionError("subprocess or socket during a page render")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    from orch.dashboard import routes_workspace
    monkeypatch.setattr(routes_workspace, "_checks", lambda ws, **kw: ([], None))  # core's own doctor shells out to git
    for url in ("/", "/board", "/board?view=list", "/board?view=external", f"/t/{tid}", "/addons/demo/", "/activity",
                "/reports", "/workspace"):
        assert client.get(url).status_code == 200, url


def test_today_tiles_and_from_addons_are_not_needs_you(client, ws, put):
    from orch.core import query
    put("backlog")  # the summary strip shows only when the workspace has tickets
    html = client.get("/").text
    assert "Checks failing" in html and "unknown" in html  # None is unknown, not 0
    assert "From addons" in html and "Answer from phone" in html
    n = len(query.needs_you(ws))  # addon items never add to the needs-you count
    assert (f"<title>({n}) " in html) if n else ("<title>(" not in html)
    assert 'name="id" value="phone/1"' in html
    stale_form = html[html.index('value="phone/2"'):]
    assert "disabled" in stale_form[:400]


def test_ticket_code_slot(client, put):
    tid = put("open")
    html = client.get(f"/t/{tid}").text
    # core links this workspace's ticket keys in widget text (design-system spec §3.0)
    assert f'PR for <a class="lnk key" href="/t/{tid}">{tid}</a>' in html


def test_board_external_tab_only_with_a_declaring_addon(client, ws):
    html = client.get("/board").text
    assert 'href="/board?view=external"' in html
    assert "Status &lt;script&gt;" in client.get("/board?view=external").text
    ws._addons = AddonRegistry(ws, {})
    assert 'view=external' not in client.get("/board").text


def test_posts_need_origin(client, demo):
    for url, data in (("/addons/demo/refresh", {}), ("/addons/demo/actions/rerun", {"target": "a/b#1"}),
                      ("/addons/demo/decisions", {"id": "phone/1", "choice": "apply"})):
        assert client.post(url, data=data, follow_redirects=False).status_code == 403, url
    assert demo.acted == [] and demo.resolved == []


def test_refresh_post_asks_the_scheduler(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client.app.state.scheduler, "request_refresh", lambda name=None: calls.append(name) or 1)
    r = client.post("/addons/demo/refresh", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and calls == ["demo"] and "Refresh+started" in r.headers["location"]


def test_action_runs_as_human_post_and_is_logged(client, demo, ws):
    from orch.core.events import read_events
    r = client.post("/addons/demo/actions/rerun", data={"target": "a/b#1"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and demo.acted == [("rerun", "a/b#1")]
    e = read_events(ws)[-1]
    assert e.kind == "addon.action" and e.actor == "human:you" and e.via == "dashboard"
    assert e.data == {"addon": "demo", "action": "rerun", "target": "a/b#1"}


def test_undeclared_action_is_refused(client, demo):
    r = client.post("/addons/demo/actions/deploy", data={"target": "x"}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"] and demo.acted == []


def test_resolve_gets_a_provider_context_and_refuses_stale_or_unknown(client, demo, ws):
    from orch.core.events import read_events
    ok = client.post("/addons/demo/decisions", data={"id": "phone/1", "choice": "apply"}, headers=ORIGIN, follow_redirects=False)
    assert ok.status_code == 303 and demo.resolved == [("phone/1", "apply", "ProviderContext")]
    e = read_events(ws)[-1]
    assert e.kind == "addon.decision" and e.actor == "human:you" and e.data["intent"] == "none"
    for data in ({"id": "phone/2", "choice": "apply"}, {"id": "phone/1", "choice": "approve"}, {"id": "gone", "choice": "apply"}):
        r = client.post("/addons/demo/decisions", data=data, headers=ORIGIN, follow_redirects=False)
        assert "err=" in r.headers["location"], data
    assert len(demo.resolved) == 1
    assert client.post("/addons/demo/decisions", data={"id": "phone/2", "choice": "ignore"}, headers=ORIGIN,
                       follow_redirects=False).status_code == 303
    assert demo.resolved[-1][:2] == ("phone/2", "ignore")


def test_refresh_post_runs_no_provider_code(client, demo):
    """Ruling: the Refresh POST only queues work for the scheduler; scopes() and fetch() never run in the request."""
    calls = []

    class Spy:
        id, kind, interval_s = "spy", "status", 60

        def scopes(self, ctx):
            calls.append("scopes")
            return ["default"]

        def fetch(self, ctx, scope, previous):
            calls.append("fetch")
            raise AssertionError("fetch during a POST")
    demo.providers = [Spy()]
    r = client.post("/addons/demo/refresh", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and "Refresh+started" in r.headers["location"]
    assert calls == [] and "demo" in client.app.state.scheduler._refresh


def test_page_and_board_external_get_cleaned_params(client, demo, put):
    """Controller addition: SlotView.params is the cleaned GET query (at most 10 keys, values cut to 200
    characters, without core's msg/err/token), only for page.<name> and board.external."""
    demo.mode = "params"
    many = "&".join(f"k{i:02d}=v" for i in range(15))
    html = client.get(f"/addons/demo/?q={XSS}&msg=hi&err=no&long={'x' * 300}&{many}").text
    seen = html[html.index("params:"):].split("<", 1)[0]
    keys = [kv.split("=", 1)[0] for kv in seen.removeprefix("params:").split("|")]
    assert len(keys) == 10 and "q" in keys and "long" in keys and not {"msg", "err", "token"} & set(keys)
    assert "long=" + "x" * 200 + "|" in seen and "x" * 201 not in seen
    assert XSS not in html and 'value="&lt;script&gt;alert(1)&lt;/script&gt;"' in html  # Search echoes it escaped
    board = client.get("/board?view=external&q=abc").text
    assert "params:q=abc<" in board  # the board's own view= key is not the addon's
    tid = put("open")
    assert "params:<" in client.get(f"/t/{tid}?q=abc").text  # every other slot sees no params


def test_slot_view_params_only_on_page_and_board_external(ws, demo):
    from orch.addons.runtime import SlotView
    la = ws.addons.get("demo")
    for slot, expected in (("page.demo", {"q": "x"}), ("board.external", {"q": "x"}), ("ticket.code", {}),
                           ("today.summary", {}), ("today.from_addons", {})):
        assert SlotView(ws, la, slot, params={"q": "x"}).params == expected, slot


def test_clean_params_limits():
    from starlette.datastructures import QueryParams
    from orch.addons.runtime import clean_params
    q = QueryParams([("token", "t"), ("msg", "m"), ("err", "e"), ("view", "external"), ("a", "1"), ("a", "2"),
                     ("b", "y" * 500)] + [(f"k{i}", "v") for i in range(20)])
    out = clean_params(q, drop=("view",))
    assert len(out) == 10 and out["a"] == "1" and out["b"] == "y" * 200
    assert not {"token", "msg", "err", "view"} & set(out)
    assert clean_params(None) == {} and clean_params(QueryParams("")) == {}


def test_search_widget_renders_a_get_form_on_the_page(client, demo):
    demo.mode = "params"
    html = client.get("/addons/demo/?q=main").text
    form = html[html.index('class="filters addon-search"') - 40:]
    form = form[:form.index("</form>")]
    assert 'method="get"' in form and 'action="/addons/demo/"' in form and 'name="q"' in form
    assert 'value="main"' in form and 'placeholder="Filter repos"' in form


def test_search_widget_is_only_allowed_on_page_slots(ws):
    from orch.addons.widgets import widget_problems
    la = loaded(ws, Demo(), **OVER)
    m = la.manifest
    assert widget_problems(Search("q", "", "Find"), slot="page.demo", manifest=m) == []
    assert widget_problems(Card("x", (Search("q"),)), slot="page.demo", manifest=m) == []
    for slot in ("ticket.code", "board.external", "today.from_addons"):
        assert any("only allowed on the addon's page" in p for p in widget_problems(Search("q"), slot=slot, manifest=m))
    assert any("Search is only allowed" in p
               for p in widget_problems(Card("x", (Search("q"),)), slot="ticket.code", manifest=m))
    for bad in (Search("Bad Name"), Search("q", value="v" * 201), Search("q", placeholder=None), Search(None)):
        assert widget_problems(bad, slot="page.demo", manifest=m), bad
    assert widget_problems(Search("q"), slot="today.summary", manifest=m) == ["Search: today.summary takes only Tile widgets"]


def test_search_in_another_slot_is_dropped_not_rendered(client, demo, ws, put, monkeypatch):
    def widgets(slot, view):
        return [Search("q", "x", "Find")] if slot == "ticket.code" else []
    monkeypatch.setattr(demo, "widgets", widgets)
    tid = put("open")
    html = client.get(f"/t/{tid}").text
    assert "addon-search" not in html and "returned an invalid widget" in html


class Quiet:
    """An addon without a provider that shows nothing, and counts the decisions() calls it gets."""

    def __init__(self):
        self.asked = 0

    def widgets(self, slot, view):
        return []

    def decisions(self, view):
        self.asked += 1
        return []


QUIET = {"capabilities": ["page", "panel", "settings", "decisions"], "slots": ["board.external", "ticket.code"],
         "menu": {"title": "Quiet", "icon": "box"}}


def test_page_failure_is_a_page_with_an_error_not_a_404(client, monkeypatch):
    runtime = client.app.state.addons
    monkeypatch.setattr(runtime, "_banner", lambda la: (_ for _ in ()).throw(RuntimeError("cache boom")))
    r = client.get("/addons/demo/")
    assert r.status_code == 200 and "could not render" in r.text
    assert client.get("/addons/nope/").status_code == 404


def test_one_failing_addon_does_not_hide_the_others_in_a_slot(ws, demo, put, monkeypatch):
    from orch.addons.runtime import AddonRuntime
    quiet = Quiet()
    ws._addons.addons["quiet"] = loaded(ws, quiet, name="quiet", **QUIET)
    quiet.widgets = lambda slot, view: [Text("from quiet")]
    runtime = AddonRuntime(ws)
    real = runtime._banner
    monkeypatch.setattr(runtime, "_banner", lambda la: (_ for _ in ()).throw(RuntimeError("x")) if la.name == "demo" else real(la))
    groups = runtime.slot("ticket.code", ticket=None)
    assert [g.addon for g in groups] == ["quiet"]


def test_slot_view_reads_settings_only_when_asked(ws, demo, monkeypatch):
    from orch.addons import userfiles
    from orch.addons.runtime import SlotView
    calls = []
    real = userfiles.workspace_addons
    monkeypatch.setattr(userfiles, "workspace_addons", lambda root: calls.append(root) or real(root))
    view = SlotView(ws, ws._addons.get("demo"), "page.demo")
    assert calls == []
    assert view.settings == {"greeting": "Hello"} and len(calls) == 1


def test_find_decision_asks_only_the_named_addon(ws, demo):
    from orch.addons.runtime import AddonRuntime
    quiet = Quiet()
    ws._addons.addons["quiet"] = loaded(ws, quiet, name="quiet", **QUIET)
    la, d = AddonRuntime(ws).find_decision("demo", "phone/1")
    assert la.name == "demo" and d.id == "phone/1" and quiet.asked == 0


def test_external_board_has_an_empty_state(ws, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    ws._addons = AddonRegistry(ws, {"quiet": loaded(ws, Quiet(), name="quiet", **QUIET)})
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    assert "Nothing from outside tools yet" in c.get("/board?view=external").text


# ---- F: the core health line and states (design-system States) ----

def test_fresh_data_is_a_muted_updated_line_with_a_time(client, ws):
    cache.write_snapshot(ws, "demo", _snap())
    banner = _banner(client.get("/addons/demo/").text)
    assert banner.startswith('<p class="addon-banner addon-health-ok" data-health="ok">Updated <time datetime="2026-10-02T09:00:00')
    assert "chip" not in banner  # freshness is never an ok chip


def test_login_needed_is_a_callout_with_the_command_to_copy(client, ws):
    cache.write_snapshot(ws, "demo", _snap("auth_required", message="login needed: run gh auth login"))
    html = client.get("/addons/demo/").text
    callout = html[html.index('<div class="callout callout-warn addon-health"'):]
    callout = callout[:callout.index("</div>")]
    assert 'data-health="auth_required"' in callout and "Login needed" in callout
    assert '<code>gh auth login</code> <button type="button" class="btn copy-fix" data-copy="gh auth login">Copy</button>' in callout
    assert "main" in html[html.index("addon-page"):]  # the data stays


def test_login_command_is_only_taken_from_a_run_phrase():
    from orch.dashboard.views import login_command
    assert login_command("login needed: run gh auth login") == "gh auth login"
    assert login_command("login needed: run `databricks auth login -p int`.") == "databricks auth login -p int"
    assert login_command("token expired") == "" and login_command("") == ""


def test_rate_limited_refresh_says_when_in_visible_text(client, ws):
    cache.write_snapshot(ws, "demo", _snap("rate_limited", retry_after=datetime(2026, 10, 2, 9, 5, tzinfo=timezone.utc)))
    html = client.get("/addons/demo/").text
    form = html[html.index('action="/addons/demo/refresh"'):]
    form = form[:form.index("</form>")]
    assert 'data-busy="Refreshing…"' in form and " disabled>" in form and re.search(r"Refresh \(after \d\d:\d\d\)</button>", form)
    assert "title=" not in form  # never a tooltip-only reason


def test_partial_data_says_so_next_to_the_health_line(client, ws):
    cache.write_snapshot(ws, "demo", _snap(complete=False))
    assert "Showing part of the data" in _banner(client.get("/addons/demo/").text)


def test_first_fetch_shows_skeleton_lines_without_a_spinner(client, demo, ws):
    demo.mode = "empty"
    html = client.get("/addons/demo/").text  # never fetched, nothing to draw yet
    page = html[html.index('<div class="addon-page" id="addon-page">'):]  # id: the live search region (D)
    assert '<section class="card skeleton-block" aria-busy="true" aria-label="Loading Demo status">' in page
    assert "spinner" not in html
    cache.write_snapshot(ws, "demo", _snap())
    assert "skeleton-block" not in client.get("/addons/demo/").text  # fetched, still empty: the plain empty text


def test_login_still_shows_when_another_scope_is_worse(client, ws):
    """The page's health is the worst scope's; a login-needed scope under a failed one still gets its callout, so an
    addon never has to draw its own login help."""
    cache.write_snapshot(ws, "demo", _snap("error", message="boom"))
    cache.write_snapshot(ws, "demo", Snapshot("fake", "other", datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc),
                                              health="auth_required", message="login needed: run gh auth login"))
    html = client.get("/addons/demo/").text
    assert 'data-health="error"' in html and 'data-health="auth_required"' in html
    assert 'data-copy="gh auth login"' in html
    from orch.addons.runtime import banner_for
    assert banner_for(cache.read_snapshots(ws, "demo")).login == "login needed: run gh auth login"


def test_menu_marks_a_broken_addon_with_a_named_red_dot(client, ws):
    nav = lambda html: html[html.index('<nav class="menu"'):html.index("</nav>")]  # noqa: E731
    assert "nav-dot" not in nav(client.get("/").text)
    cache.write_snapshot(ws, "demo", _snap("error", message="boom"))
    html = nav(client.get("/").text)
    item = html[html.index('href="/addons/demo/"'):]
    item = item[:item.index("</a>")]
    assert '<span class="nav-dot" role="img" aria-label="Demo status: last fetch failed"></span>' in item
    assert "✕" not in item and "badge" not in item



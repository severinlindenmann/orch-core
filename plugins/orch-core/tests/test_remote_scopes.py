"""Request origin, route scopes and the request-scoped actor (the authorization core of the remote bridge)."""
import asyncio
import inspect
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from starlette.requests import Request  # noqa: E402

from orch.core.events import Actor, read_events  # noqa: E402
from orch.dashboard import reach, remote_gate, terminals  # noqa: E402
from orch.dashboard.app import create_app, dashboard_routes, router_modules  # noqa: E402
from orch.dashboard.reach import LOCAL_HUMAN, RemoteOrigin, Scope, request_actor  # noqa: E402
from orch.remote import bridge  # noqa: E402
from orch.remote import store as phones  # noqa: E402

DASHBOARD = Path(reach.__file__).parent


def origin(scope=Scope.TYPE, fresh=False, label="Pixel 8", device="dev_abc123"):
    return RemoteOrigin(device, scope, label, fresh)


@pytest.fixture
def app(ws):
    return create_app(ws, "tok")


DROP = object()


def call(app, method, path, *, remote=None, query=b"", body=b"", headers=(), raw=None, extra=None):
    """One ASGI request straight into the app (no client library normalising the path); (status, headers, body)."""
    hdrs = [(b"host", b"127.0.0.1:8765"), (b"cookie", b"orch_token=tok"), (b"origin", b"http://127.0.0.1:8765"),
            *[(k.lower().encode(), v.encode()) for k, v in headers]]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": (raw or path).encode(), "query_string": query, "root_path": "", "headers": hdrs,
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8765)}
    if remote is not None:
        scope[reach.SCOPE_KEY] = remote
    scope.update(extra or {})
    scope = {k: v for k, v in scope.items() if v is not DROP}
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict(start["headers"]), b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


FORM = (("content-type", "application/x-www-form-urlencoded"),)


# -- the marker cannot be forged over HTTP ------------------------------------------------------------------------

def test_headers_params_cookies_and_body_never_make_a_request_remote(dash, ws, put):
    tid = put("backlog")
    assert dash.get("/workspace").status_code == 200
    forged = {"orch.remote": "1", "x-orch-remote": "device", "x-remote-scope": "type", "x-forwarded-for": "10.0.0.5"}
    r = dash.get("/workspace?orch.remote=1&remote=1&scope=type&device=x", headers=forged,
                 cookies={"orch.remote": "1", "remote": "type"})
    assert r.status_code == 200  # still a local request: the Workspace page opens
    r = dash.post(f"/t/{tid}/comment", data={"text": "hello", "orch.remote": "1", "scope": "type"}, headers=forged)
    assert r.status_code in (200, 303)
    events = [e for e in read_events(ws, tid) if e.kind == "ticket.comment"] or read_events(ws, tid)
    assert all(e.via == "dashboard" for e in events if str(e.actor).startswith("human"))


def test_a_malformed_marker_is_refused_not_local(app):
    for bad in ("type", 4, {"scope": 4}, object()):
        status, _, _ = call(app, "GET", "/board", remote=bad)
        assert status == 403


def test_request_and_origin_validation():
    for device in ("", "x" * 65, "a b", "a/b"):
        with pytest.raises(ValueError):
            RemoteOrigin(device, Scope.LOOK)
    with pytest.raises(ValueError):
        RemoteOrigin("d1", 4)  # a plain int is not a Scope
    assert [s.value for s in Scope] == [1, 2, 3, 4]
    assert Scope.LOOK < Scope.DECIDE < Scope.OPERATE < Scope.TYPE


# -- reach and Terminals -------------------------------------------------------------------------------------------

def _request(client="127.0.0.1", host="127.0.0.1:8765", marker=None):
    scope = {"type": "http", "method": "GET", "path": "/terminals", "headers": [(b"host", host.encode())],
             "client": (client, 5) if client else None}
    if marker is not None:
        scope[reach.SCOPE_KEY] = marker
    return Request(scope)


def test_reach_answers_local_remote_or_refused():
    assert reach.reach(_request()).kind == "local"
    assert reach.reach(_request(client="192.168.1.9")).kind == "refused"
    assert reach.reach(_request(host="evil.example")).kind == "refused"
    assert reach.reach(_request(client=None)).kind == "refused"
    r = reach.reach(_request(client="203.0.113.7", host="anything", marker=origin(Scope.DECIDE)))
    assert (r.kind, r.scope) == ("remote", Scope.DECIDE)
    assert reach.reach(_request(marker="type")).kind == "refused"


def test_terminals_gate_reads_reach():
    assert terminals.local_request(_request())
    assert not terminals.local_request(_request(client="192.168.1.9"))
    assert not terminals.local_request(_request(host="rebound.example"))
    assert terminals.local_request(_request(client="203.0.113.7", host="x", marker=origin(Scope.LOOK)))
    assert not terminals.local_request(_request(marker=object()))


# -- the actor -------------------------------------------------------------------------------------------------------

def test_actor_is_request_scoped():
    assert request_actor(_request()) == Actor("human", "you", "dashboard") == LOCAL_HUMAN
    a = request_actor(_request(marker=origin(label="Pixel 8")))
    assert (a.kind, a.name, a.via, a.device) == ("human", "you", "device:Pixel 8", "dev_abc123")
    assert request_actor(_request(marker=origin(label="a/b<c>" + "x" * 80))).via == "device:ab" + "c" + "x" * 37
    assert request_actor(_request(marker=origin(label="///"))).via == "device:dev_abc123"
    with pytest.raises(reach.BadOrigin):
        request_actor(_request(marker="type"))


def test_no_module_uses_the_fixed_actor_constant():
    """Derived: a new use of a fixed human actor in the dashboard fails here."""
    # the constant itself; the background runners (AI Factory, schedules) have no request
    allowed_local = {"reach.py", "factory_runner.py", "schedules.py"}
    for path in DASHBOARD.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(DASHBOARD).as_posix()
        assert not re.search(r"(?<![A-Za-z_])HUMAN\b", text), f"{rel} uses the fixed HUMAN actor; use request_actor(request)"
        if path.name not in allowed_local:
            assert "LOCAL_HUMAN" not in text, f"{rel} uses LOCAL_HUMAN; use request_actor(request)"
        if rel not in ("reach.py", "data/decisions.py"):  # the latter only lists which moves the page offers
            assert not re.search(r"Actor\(\s*[\"']human[\"']", text), f"{rel} builds a human actor"


def test_a_remote_comment_names_the_device_and_a_local_one_does_not(app, ws, put):
    tid = put("backlog")
    status, _, _ = call(app, "POST", f"/t/{tid}/comment", remote=origin(Scope.OPERATE), body=b"text=from+the+phone",
                        headers=FORM)
    assert status == 303
    status, _, _ = call(app, "POST", f"/t/{tid}/comment", body=b"text=at+the+desk", headers=FORM)
    assert status == 303
    humans = [e for e in read_events(ws, tid) if str(e.actor).startswith("human")]
    assert [e.via for e in humans] == ["device:Pixel 8", "dashboard"]
    assert humans[0].data.get("device") == "dev_abc123" and "device" not in humans[1].data


# -- the gate --------------------------------------------------------------------------------------------------------

def test_local_requests_are_untouched(app):
    assert call(app, "GET", "/workspace")[0] == 200
    assert call(app, "GET", "/board", query=b"a=1;token=x")[0] != 403  # the gate's query rule is for remote only
    assert call(app, "GET", "/t/a%2fb", raw="/t/a%2fb")[0] != 403  # and so is its path rule


def test_scope_ladder_on_a_few_real_routes(app, put):
    tid = put("backlog")
    ok = lambda s: s != 403  # noqa: E731
    assert ok(call(app, "GET", "/board", remote=origin(Scope.LOOK))[0])
    assert not ok(call(app, "POST", f"/t/{tid}/comment", remote=origin(Scope.DECIDE), body=b"text=x", headers=FORM)[0])
    assert ok(call(app, "POST", f"/t/{tid}/comment", remote=origin(Scope.OPERATE), body=b"text=x", headers=FORM)[0])
    assert not ok(call(app, "POST", f"/t/{tid}/answer", remote=origin(Scope.LOOK), body=b"qid=q1&qhash=x", headers=FORM)[0])
    assert ok(call(app, "POST", f"/t/{tid}/answer", remote=origin(Scope.DECIDE), body=b"qid=q1&qhash=x", headers=FORM)[0])
    assert not ok(call(app, "POST", "/terminals/new", remote=origin(Scope.OPERATE))[0])
    assert ok(call(app, "POST", "/terminals/new", remote=origin(Scope.TYPE))[0])


def test_the_refusal_is_a_readable_page(app):
    status, headers, body = call(app, "GET", "/workspace", remote=origin(Scope.TYPE, fresh=True))
    assert status == 403 and headers[b"content-type"].startswith(b"text/html")
    assert b"This device cannot do this" in body and b"<" in body


def test_static_files_are_look_and_get_only(app):
    assert call(app, "GET", "/static/app.css", remote=origin(Scope.LOOK))[0] == 200
    assert call(app, "POST", "/static/app.css", remote=origin(Scope.TYPE))[0] == 403
    assert call(app, "GET", "/static/../workspace", remote=origin(Scope.TYPE))[0] == 403


@pytest.mark.parametrize("path,raw", [
    ("/t/a/b", "/t/a%2fb"), ("/t/a/b", "/t/a%2Fb"), ("/t/a\\b", "/t/a%5cb"), ("/t/a\\b", "/t/a%5Cb"),
    ("/t/a\x00", "/t/a%00"), ("//board", "//board"), ("/board//", "/board//"), ("/t/../workspace", "/t/../workspace"),
    ("/./board", "/./board"), ("/board/.", "/board/."), ("/t/%2e%2e/x", "/t/%2e%2e/x"), ("/t/../x", "/t/%2e%2e/x"),
])
def test_odd_paths_are_refused_before_routing(app, path, raw):
    assert call(app, "GET", path, raw=raw, remote=origin(Scope.TYPE, fresh=True))[0] == 403


@pytest.mark.parametrize("method", ["OPTIONS", "PUT", "DELETE", "PATCH", "TRACE", "CONNECT", "PROPFIND"])
def test_only_get_head_post_pass(app, method):
    assert call(app, method, "/board", remote=origin(Scope.TYPE, fresh=True))[0] == 403


@pytest.mark.parametrize("query", [b"token=x", b"a=1&token=", b"TOKEN=x", b"%74oken=x", b"a=1;token=x", b"Token=x&b=2"])
def test_a_token_query_parameter_is_refused(app, query):
    assert call(app, "GET", "/board", query=query, remote=origin(Scope.TYPE, fresh=True))[0] == 403


def test_approve_is_decide_unless_it_arms_the_runner(app, put):
    tid = put("backlog")
    path = f"/t/{tid}/approve"
    plain = b"gate=requirements&seen=abc"

    def run(scope, body=plain, *, query=b"", fresh=False, headers=FORM):
        return call(app, "POST", path, remote=origin(scope, fresh=fresh), body=body, query=query, headers=headers)

    status, headers, _ = run(Scope.DECIDE)
    assert status == 303 and b"missing" not in headers[b"location"]  # allowed, and the body reached the form intact
    assert run(Scope.LOOK)[0] == 403
    for body, query in ((plain + b"&factory=1", b""), (plain + b"&delegate=on", b""), (plain + b"&max_children=3", b""),
                        (plain + b"&max_size=m", b""), (plain + b"&factory=on", b""), (plain + b"&factory=0", b""),
                        (plain, b"factory=1"), (plain + b";factory=1", b""), (plain + b"&%66actory=1", b"")):
        assert run(Scope.DECIDE, body, query=query)[0] == 403, (body, query)
        assert run(Scope.OPERATE, body, query=query)[0] == 403  # still needs Type
        assert run(Scope.TYPE, body, query=query)[0] == 403  # and a fresh assertion
        assert run(Scope.TYPE, body, query=query, fresh=True)[0] == 303
    # blank arming fields do not arm; a body that cannot be read as a form is treated as arming
    assert run(Scope.DECIDE, plain + b"&factory=&max_children=")[0] == 303
    assert run(Scope.DECIDE, plain, headers=(("content-type", "multipart/form-data; boundary=x"),))[0] == 403
    assert run(Scope.DECIDE, plain, headers=())[0] == 403
    assert run(Scope.TYPE, plain, headers=(), fresh=True)[0] == 303
    assert run(Scope.TYPE, b"x=" + b"a" * 70000, fresh=True)[0] == 403  # too large to read: refused like the rest


def test_fresh_assertion_routes_need_the_marker(app):
    assert call(app, "POST", "/permits/r1/grant", remote=origin(Scope.TYPE))[0] == 403
    assert call(app, "POST", "/permits/r1/grant", remote=origin(Scope.TYPE, fresh=True))[0] == 303
    assert call(app, "POST", "/permits/r1/deny", remote=origin(Scope.DECIDE))[0] == 303


def test_a_websocket_with_the_marker_is_closed(app):
    sent = []

    async def go():
        async def receive():
            return {"type": "websocket.connect"}

        async def send(m):
            sent.append(m)
        await app({"type": "websocket", "path": "/events", "headers": [], reach.SCOPE_KEY: origin()}, receive, send)
    asyncio.run(go())
    assert sent == [{"type": "websocket.close", "code": 1008}]


# -- the keeper: the real route list -------------------------------------------------------------------------------

def _sample(path):
    return re.sub(r"\{[^}]*:path\}", "x/y", re.sub(r"\{[^}:]*\}", "x", path))


def _route_keys():
    return [(m, r.path) for r in dashboard_routes() for m in sorted(r.methods)]


def test_the_module_list_is_every_routes_file():
    on_disk = {p.stem for p in DASHBOARD.glob("routes_*.py")}
    assert {m.__name__.rsplit(".", 1)[1] for m in router_modules()} == on_disk


def test_every_route_and_method_has_a_tag_and_every_tag_a_route():
    keys = _route_keys()
    assert len(keys) > 60 and len(set(keys)) == len(keys)
    untagged = [k for k in keys if k not in remote_gate.TAGS]
    assert not untagged, f"routes without a scope tag: {untagged}"
    assert set(remote_gate.TAGS) <= set(keys), "a tag for a route that does not exist"


def test_each_route_is_what_the_router_matches_for_its_own_path():
    routes = dashboard_routes()
    for method, path in _route_keys():
        scope = {"type": "http", "method": method, "path": _sample(path), "root_path": "", "headers": []}
        found = remote_gate.match_route(routes, scope)
        assert found is not None and found.path == path and method in found.methods, (method, path, found and found.path)


NEVER_REMOTE = re.compile(r"^/workspace(/|$)")


def test_the_workspace_family_is_never_remote():
    seen = [k for k in _route_keys() if NEVER_REMOTE.match(k[1])]
    assert len(seen) >= 12
    for key in seen:
        tag = remote_gate.TAGS[key]
        assert not callable(tag) and tag.scope is remote_gate.NEVER, key
    for key, tag in remote_gate.TAGS.items():
        if not callable(tag) and tag.scope is remote_gate.NEVER:
            ok = NEVER_REMOTE.match(key[1]) or key == ("GET", "/__orch/status")  # the loopback-only switcher probe
            assert ok, f"{key} is never-remote but is not in the Workspace family"


# What arms or launches something: derived from what a route's code calls, so a new route that does it is caught.
ARMS = re.compile(r"_arm_runner|factory_sessions\.arm|launch\.start|permit_grant|terminals\.(?:send|resize|end)\b"
                  r"|\.act\(|execute\(|sc\.arm\(|request_run\(")


def _source_one_level(endpoint) -> str:
    """The endpoint's source plus the source of each function in orch it calls by name (one level), so a route that
    arms or launches through a helper is seen as well."""
    source = inspect.getsource(endpoint)
    seen = {endpoint}
    for name in sorted(set(re.findall(r"(?<![.\w])([A-Za-z_]\w*)\(", source))):
        fn = getattr(inspect.getmodule(endpoint), name, None)
        if inspect.isfunction(fn) and fn not in seen and fn.__module__.startswith("orch.dashboard"):
            seen.add(fn)
            source += "\n" + inspect.getsource(fn)
    return source


def test_routes_that_arm_or_launch_are_type():
    found = set()
    for route in dashboard_routes():
        source = _source_one_level(route.endpoint)
        if not ARMS.search(source):
            continue
        for method in route.methods:
            key = (method, route.path)
            found.add(key)
            tag = remote_gate.TAGS[key]
            if callable(tag):  # conditional: must be Type with a fresh assertion for the arming form
                armed = tag({"factory": ["1"]})
                assert (armed.scope, armed.fresh) == (Scope.TYPE, True), key
                assert tag(None).scope == Scope.TYPE, key
            else:
                assert tag.scope is not None and tag.scope >= Scope.TYPE or key in ADDON_DECISIONS, key
    for expected in (("POST", "/t/{ref}/approve"), ("POST", "/t/{ref}/agent/start"), ("POST", "/permits/{rid}/grant"),
                     ("POST", "/terminals/{name}/keys"), ("POST", "/terminals/new"),
                     ("POST", "/addons/{name}/actions/{action_id}"), ("POST", "/schedules/{sid}/arm"),
                     ("POST", "/schedules/{sid}/run")):
        assert expected in found, f"the arming scan no longer sees {expected}"
    assert remote_gate.TAGS[("POST", "/permits/{rid}/grant")].fresh


# Every route a remote device may reach below Type, other than a plain read at Look, with the scope it is open at.
# A new route is never remote until it is tagged; a route tagged below Type must also be added here, which is the
# moment someone reviews that it neither arms, launches nor reaches the host. Lowering a route fails here too.
BELOW_TYPE = {
    ("GET", "/addons/{name}/files/{token}"): "OPERATE", ("GET", "/terminals"): "OPERATE",
    ("GET", "/terminals/stream"): "OPERATE", ("GET", "/terminals/{name}"): "OPERATE",
    ("GET", "/terminals/{name}/stream"): "OPERATE",
    ("POST", "/addons/{name}/decisions"): "OPERATE", ("POST", "/addons/{name}/refresh"): "OPERATE",
    ("POST", "/board/backlog"): "OPERATE", ("POST", "/new"): "OPERATE",
    ("POST", "/permits/grants/{gid}/revoke"): "DECIDE", ("POST", "/permits/{rid}/deny"): "DECIDE",
    ("POST", "/quick/add"): "OPERATE", ("POST", "/quick/{qid}/done"): "OPERATE",
    ("POST", "/quick/{qid}/drop"): "DECIDE", ("POST", "/quick/{qid}/promote"): "OPERATE",
    ("POST", "/quick/{qid}/release"): "OPERATE", ("POST", "/quick/{qid}/reopen"): "DECIDE",
    ("POST", "/schedules/runs/{rid}/{fid}/dismiss"): "DECIDE", ("POST", "/schedules/runs/{rid}/{fid}/file"): "OPERATE",
    ("POST", "/schedules/{sid}/pause"): "DECIDE",
    ("POST", "/t/{ref}/answer"): "DECIDE", ("POST", "/t/{ref}/approve"): "conditional",
    ("POST", "/t/{ref}/approve-together"): "DECIDE", ("POST", "/t/{ref}/artifacts"): "OPERATE",
    ("POST", "/t/{ref}/close"): "DECIDE", ("POST", "/t/{ref}/comment"): "OPERATE", ("POST", "/t/{ref}/edit"): "OPERATE",
    ("POST", "/t/{ref}/epic/pause"): "DECIDE", ("POST", "/t/{ref}/move"): "DECIDE", ("POST", "/t/{ref}/option"): "DECIDE",
    ("POST", "/t/{ref}/release"): "OPERATE", ("POST", "/t/{ref}/reopen"): "DECIDE",
    ("POST", "/t/{ref}/request-changes"): "DECIDE", ("POST", "/t/{ref}/task"): "OPERATE",
    ("POST", "/t/{ref}/task/add"): "OPERATE", ("POST", "/t/{ref}/verdict"): "DECIDE", ("POST", "/theme"): "OPERATE",
}


def test_routes_open_below_type_are_an_explicit_allow_list():
    actual = {}
    for key, tag in remote_gate.TAGS.items():
        if callable(tag):
            actual[key] = "conditional"
        elif tag.scope is not None and tag.scope < Scope.TYPE and not (key[0] == "GET" and tag.scope is Scope.LOOK):
            actual[key] = tag.scope.name
    assert actual == BELOW_TYPE, (
        "a route is open to a remote device below Type that this list does not know (or one changed scope): "
        "review that it cannot arm, launch or reach the host, then update BELOW_TYPE")


# an addon's decision applies an intent the addon returned; it is Operate (not Type) and is listed here on purpose
ADDON_DECISIONS = {("POST", "/addons/{name}/decisions")}


def test_decisions_carry_their_phone_switch_kind():
    kinds = {"/t/{ref}/approve": "approve", "/t/{ref}/approve-together": "approve", "/t/{ref}/request-changes":
             "request_changes", "/t/{ref}/answer": "answer", "/t/{ref}/verdict": "verdict"}
    for path, kind in kinds.items():
        tag = remote_gate.TAGS[("POST", path)]
        tag = tag(None) if callable(tag) else tag
        assert tag.kind == kind and tag.scope >= Scope.DECIDE
    assert remote_gate.TAGS[("POST", "/new")].kind == "ticket_request"


def test_tag_counts_per_scope():
    counts = {}
    for tag in remote_gate.TAGS.values():
        tag = tag(None) if callable(tag) else tag
        counts[tag.scope] = counts.get(tag.scope, 0) + 1
    assert counts[None] >= 12 and counts[Scope.LOOK] >= 25 and counts[Scope.TYPE] >= 5


# -- one policy for both phone paths --------------------------------------------------------------------------------

def test_bridge_needs_scope_and_switch(ws):
    root = ws.root
    for kind in bridge.SWITCHED_KINDS:
        assert bridge.allows(root, kind, origin(Scope.DECIDE), Scope.DECIDE)
        assert not bridge.allows(root, kind, origin(Scope.LOOK), Scope.DECIDE)
    phones.set_permissions(root, {"answer": True, "approve": False, "request_changes": True, "verdict": False})
    assert bridge.allows(root, "answer", origin(Scope.TYPE), Scope.DECIDE)
    assert not bridge.allows(root, "approve", origin(Scope.TYPE), Scope.DECIDE)
    assert not bridge.allows(root, "verdict", origin(Scope.TYPE), Scope.DECIDE)
    assert bridge.allows(root, None, origin(Scope.DECIDE), Scope.DECIDE)  # a move has no switch: scope alone
    assert not bridge.allows(root, "move", origin(Scope.TYPE), Scope.DECIDE)  # an unknown kind is no
    assert not bridge.allows(root, "approve", None, Scope.DECIDE)  # anything broken is no


def test_a_switched_off_kind_is_refused_by_the_gate(app, ws, put):
    tid = put("backlog")
    phones.set_permissions(ws.root, {"answer": True, "approve": False, "request_changes": True, "verdict": True})
    body = b"gate=requirements&seen=abc"
    assert call(app, "POST", f"/t/{tid}/approve", remote=origin(Scope.TYPE, fresh=True), body=body, headers=FORM)[0] == 403
    assert call(app, "POST", f"/t/{tid}/approve", body=body, headers=FORM)[0] == 303  # the desk is not a phone
    assert call(app, "POST", f"/t/{tid}/answer", remote=origin(Scope.DECIDE), body=b"qid=q1&qhash=x", headers=FORM)[0] == 303


def test_the_factory_lookup_runs_off_the_event_loop(app, ws, put, monkeypatch):
    import threading
    tid = put("backlog")
    seen = []
    real = remote_gate.factory_guarded

    def spy(w, ref):
        seen.append(threading.get_ident())
        return real(w, ref)

    monkeypatch.setattr(remote_gate, "factory_guarded", spy)
    main = threading.get_ident()
    status = call(app, "POST", f"/t/{tid}/comment", remote=origin(Scope.OPERATE), body=b"text=hi", headers=FORM)[0]
    assert status == 303 and seen and main not in seen  # the scan and ticket reads must not block the loop


def test_bridge_clock_window():
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    assert bridge.BRIDGE_SKEW_S == 300
    assert bridge.within_window(now + timedelta(seconds=300), now) and bridge.within_window(now - timedelta(seconds=300), now)
    assert not bridge.within_window(now + timedelta(seconds=301), now)
    assert not bridge.within_window(now - timedelta(seconds=301), now)
    assert not bridge.within_window(datetime(2026, 10, 5, 12, 0), now)  # naive: no


def test_the_signed_phone_path_keeps_its_own_windows():
    from orch.remote import verify
    assert (verify.MAX_AGE_DAYS, verify.MAX_SKEW_S) == (14, 300)
    assert "move" not in verify.DIRECT_KINDS


# -- what a refusal reveals, and the shapes that could dodge the tag lookup -----------------------------------------

def test_every_refusal_looks_the_same(app, ws, put):
    tid = put("backlog")
    phones.set_permissions(ws.root, {"answer": False, "approve": True, "request_changes": True, "verdict": True})
    refused = [
        call(app, "GET", "/no/such/route", remote=origin(Scope.TYPE, fresh=True)),  # no route
        call(app, "GET", "/workspace", remote=origin(Scope.TYPE, fresh=True)),  # never remote
        call(app, "POST", f"/t/{tid}/comment", remote=origin(Scope.LOOK), body=b"text=x", headers=FORM),  # scope
        call(app, "POST", f"/t/{tid}/answer", remote=origin(Scope.TYPE), body=b"qid=q1&qhash=x", headers=FORM),  # switch
        call(app, "POST", f"/t/{tid}/approve", remote=origin(Scope.LOOK), body=b"gate=requirements&seen=a&factory=1",
             headers=FORM),  # arming, scope too low: no hint that a fresh confirmation would help
        call(app, "POST", f"/t/{tid}/approve", remote=origin(Scope.DECIDE), body=b"x=" + b"a" * 70000,
                           headers=FORM),  # too large to read
        call(app, "GET", "/board", remote=origin(Scope.TYPE), query=b"token=x"),
        call(app, "GET", "/t/a/b", raw="/t/a%2fb", remote=origin(Scope.TYPE)),
        call(app, "TRACE", "/board", remote=origin(Scope.TYPE)),
        call(app, "GET", "/board", remote="forged"),
    ]
    assert {(status, body) for status, _, body in refused} == {(403, refused[0][2])}
    assert b"fresh" not in refused[0][2] and b"switch" not in refused[0][2]


def test_only_a_device_that_may_do_it_hears_about_the_fresh_confirmation(app, put):
    tid = put("backlog")
    body = b"gate=requirements&seen=a&factory=1"
    status, _, text = call(app, "POST", f"/t/{tid}/approve", remote=origin(Scope.TYPE), body=body, headers=FORM)
    assert status == 403 and b"fresh confirmation" in text


@pytest.mark.parametrize("path", ["/t/x%252fedit", "/board%2e", "/t/%41", "/static/%2e%2e/workspace"])
def test_a_path_that_still_holds_an_escape_is_refused(app, path):
    assert call(app, "GET", path, raw=path, remote=origin(Scope.TYPE, fresh=True))[0] == 403


def test_no_raw_path_or_a_root_path_is_refused(app):
    assert call(app, "GET", "/board", remote=origin(Scope.LOOK), extra={"raw_path": DROP})[0] == 403
    assert call(app, "GET", "/board", remote=origin(Scope.LOOK), extra={"raw_path": b""})[0] == 403
    assert call(app, "GET", "/static/app.css", remote=origin(Scope.LOOK), extra={"root_path": "/x"})[0] == 403
    assert call(app, "GET", "/board", remote=origin(Scope.LOOK))[0] == 200


def test_head_and_trailing_slashes_reach_no_untagged_handler(app):
    assert call(app, "HEAD", "/workspace", remote=origin(Scope.TYPE, fresh=True))[0] == 403
    assert call(app, "HEAD", "/board", remote=origin(Scope.TYPE, fresh=True))[0] == 403  # no HEAD route, no tag
    for path in ("/workspace/", "/board/", "/terminals/", "/t/x/approve/", "/static"):
        assert call(app, "GET", path, remote=origin(Scope.TYPE, fresh=True))[0] == 403, path
    assert call(app, "POST", "/t/x/approve/", remote=origin(Scope.TYPE, fresh=True), body=b"factory=1", headers=FORM)[0] == 403


# -- follow-ups from the independent review ---------------------------------------------------------------------------

def test_watching_a_terminal_needs_operate_and_typing_stays_type(app):
    reads = [("GET", "/terminals"), ("GET", "/terminals/stream"), ("GET", "/terminals/{name}"),
             ("GET", "/terminals/{name}/stream"), ("GET", "/addons/{name}/files/{token}")]
    for key in reads:
        assert remote_gate.TAGS[key].scope is Scope.OPERATE, key
    for key in (("POST", "/terminals/new"), ("POST", "/terminals/{name}/keys"), ("POST", "/terminals/{name}/size"),
                ("POST", "/terminals/{name}/end")):
        assert remote_gate.TAGS[key].scope is Scope.TYPE, key
    assert call(app, "GET", "/terminals", remote=origin(Scope.DECIDE))[0] == 403
    assert call(app, "GET", "/terminals", remote=origin(Scope.OPERATE))[0] != 403
    assert call(app, "GET", "/addons/x/files/abc", remote=origin(Scope.LOOK))[0] == 403


def test_the_live_stream_carries_no_content():
    src = inspect.getsource(__import__("orch.dashboard.routes_live", fromlist=["x"]))
    assert "event: change" in src and "data: {}" in src and "event: hello" in src  # a version tag and change pings only


# An addon decision's intent is known only after the addon answered, so the handler applies the gate's rules.
from addon_fixtures import loaded  # noqa: E402
from orch.addons.api import Intent, PendingDecision  # noqa: E402
from orch.addons.loader import AddonRegistry  # noqa: E402
from orch.core import store  # noqa: E402
from orch.core.questions import question_hash  # noqa: E402


class _Phone:
    def __init__(self):
        self.items, self.intent = [], None

    def decisions(self, view):
        return self.items

    def resolve(self, decision_id, choice, ctx):
        return self.intent


@pytest.fixture
def decider(ws, aops):
    t = aops.new("Decide through an addon")
    aops.ask(t.id, [{"text": "Which format?", "options": ["ISO 8601", "Local"], "recommended": "A"}])
    obj = _Phone()
    ws._addons = AddonRegistry(ws, {"phone": loaded(ws, obj, name="phone", capabilities=["decisions"], slots=[],
                                                    menu=None, settings_schema=[])})
    return t.id, obj


def _decide(app, remote):
    return call(app, "POST", "/addons/phone/decisions", remote=remote, body=b"id=p1&choice=apply", headers=FORM)


def _answer_intent(ws, tid):
    q = store.load(ws, tid)[1].meta["questions"][0]
    return Intent("answer", ref=tid, qid="Q1", value="A", expected_hash=question_hash(q))


def test_an_addon_decision_needs_the_scope_and_the_switch_of_its_intent(app, ws, decider):
    tid, obj = decider
    obj.items = [PendingDecision("p1", "Answer", ticket=tid, anchor="Q1")]
    obj.intent = _answer_intent(ws, tid)

    def answered():
        return store.load(ws, tid)[1].meta["questions"][0]["answer"]

    phones.set_permissions(ws.root, {"answer": False, "approve": True, "request_changes": True, "verdict": True})
    status, headers, _ = _decide(app, origin(Scope.TYPE, fresh=True))
    assert status == 303 and b"err=" in headers[b"location"] and answered() is None  # switch off
    phones.set_permissions(ws.root, {"answer": True, "approve": False, "request_changes": False, "verdict": False})
    status, headers, _ = _decide(app, origin(Scope.LOOK))
    assert status == 403  # the route itself needs Operate
    status, headers, _ = _decide(app, origin(Scope.OPERATE))
    assert b"err=" not in headers[b"location"] and answered() == "A"  # scope and switch both allow it


@pytest.mark.parametrize("kind,switch", [("approve", "approve"), ("request_changes", "request_changes"),
                                         ("verdict", "verdict")])
def test_other_intent_kinds_are_held_to_their_switch(app, ws, decider, kind, switch):
    tid, obj = decider
    obj.items = [PendingDecision("p1", "Decide", ticket=tid)]
    obj.intent = Intent(kind, ref=tid, gate="requirements", expected_hash="sha256:" + "0" * 64, reason="r")
    everything = {k: True for k in phones.KINDS}
    phones.set_permissions(ws.root, {**everything, switch: False})
    status, headers, _ = _decide(app, origin(Scope.TYPE, fresh=True))
    assert status == 303 and headers[b"location"].count(b"err=") == 1
    assert b"Open+the+dashboard" in headers[b"location"]  # refused by the policy, not by the ticket's state


def test_unknown_and_unmapped_intent_kinds_are_refused(ws):
    class _Req:
        scope = {reach.SCOPE_KEY: origin(Scope.TYPE, fresh=True)}
    for kind in ("import", "close", "reopen", "bogus", None):
        assert remote_gate.decision_refusal(_Req(), ws, Intent(kind or "none") if kind in ("import", "close", "reopen")
                                            else type("I", (), {"kind": kind, "ref": "x"})()) == remote_gate.NO_WAY
    assert remote_gate.decision_refusal(_Req(), ws, Intent("none")) is None
    assert remote_gate.decision_refusal(type("R", (), {"scope": {}})(), ws, Intent("close", ref="x")) is None  # local


# -- changes under a running AI Factory epic need a fresh assertion --------------------------------------------------

@pytest.fixture
def factory(configure, agent, human):
    from conftest import human_ops
    from orch.core.ops import Ops
    fws = configure(factory={"enabled": True})
    a, h = Ops(fws, agent), human_ops(fws, human)
    epic = a.new("Revamp", type="epic")
    a.set_section(epic.id, "Requirements", "r")
    a.set_section(epic.id, "Acceptance criteria", "- [ ] a")
    h.approve(epic.id, "requirements", delegate={"factory": True})
    child = a.new("child", epic=epic.id)
    other = a.new("unrelated")
    return fws, epic.id, child.id, other.id


FACTORY_POSTS = (("approve", b"gate=requirements&seen=x"), ("move", b"to=backlog"), ("request-changes", b"gate=plan&seen=x"),
                 ("answer", b"qid=q1&qhash=x"), ("comment", b"text=x"),
                 ("task", b"task=t1&action=done"), ("task/add", b"text=x"), ("verdict", b"verdict=done&seen=x"),
                 ("release", b""), ("epic/pause", b""), ("edit", b"text=x"))


def test_changes_under_a_running_factory_epic_need_a_fresh_assertion(factory):
    fws, epic, child, other = factory
    app = create_app(fws, "tok")
    for ref in (epic, child):  # refused without the assertion; nothing ran, so the delegation is still active
        for action, body in FACTORY_POSTS:
            path = f"/t/{ref}/{action}"
            assert call(app, "POST", path, remote=origin(Scope.TYPE), body=body, headers=FORM)[0] == 403, path
    for action, body in FACTORY_POSTS:  # with it they reach the handler (the child first: these really run)
        assert call(app, "POST", f"/t/{child}/{action}", remote=origin(Scope.TYPE, fresh=True), body=body,
                    headers=FORM)[0] != 403, action
    assert call(app, "POST", f"/t/{epic}/comment", remote=origin(Scope.TYPE, fresh=True), body=b"text=x",
                headers=FORM)[0] == 303
    for action, body in FACTORY_POSTS:  # a ticket outside the epic, and a local request, are not held
        assert call(app, "POST", f"/t/{other}/{action}", remote=origin(Scope.TYPE), body=body, headers=FORM)[0] != 403
        assert call(app, "POST", f"/t/{child}/{action}", body=body, headers=FORM)[0] != 403


def test_a_factory_lookup_error_fails_closed(factory, monkeypatch):
    fws, epic, child, other = factory
    from orch.core import epics
    monkeypatch.setattr(epics, "delegation", lambda *a, **k: 1 / 0)
    assert remote_gate.factory_guarded(fws, child) is True
    assert remote_gate.factory_guarded(fws, "nonexistent-99") is False  # the handler refuses that itself
    assert remote_gate.factory_guarded(fws, None) is True


def test_an_addon_decision_under_a_factory_epic_needs_a_fresh_assertion(factory):
    fws, epic, child, other = factory
    assert remote_gate.decision_refusal(type("R", (), {"scope": {reach.SCOPE_KEY: origin(Scope.TYPE)}})(), fws,
                                        Intent("move", ref=child, value="backlog")) is not None
    assert remote_gate.decision_refusal(type("R", (), {"scope": {reach.SCOPE_KEY: origin(Scope.TYPE, fresh=True)}})(),
                                        fws, Intent("move", ref=child, value="backlog")) is None


def test_within_window_never_raises():
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    for bad in (0, 1.5, "2026-10-05T12:00:00Z", None, b"x", object()):
        assert bridge.within_window(bad, now) is False
        assert bridge.within_window(now, bad) is False
    assert bridge.within_window(datetime(2026, 10, 5, 12, 0), now) is False  # naive
    assert bridge.within_window(now, datetime(2026, 10, 5, 12, 0)) is False
    assert bridge.within_window(now + timedelta(seconds=300), now) is True
    assert bridge.within_window(now - timedelta(seconds=300), now) is True
    assert bridge.within_window(now + timedelta(seconds=300, microseconds=1), now) is False
    assert bridge.within_window(datetime.max.replace(tzinfo=timezone.utc), datetime.min.replace(tzinfo=timezone.utc)) is False


# -- the factory lookup is explicit, and every doubt means guarded ----------------------------------------------------

def _wrap_resolve(monkeypatch, only, result):
    real = store.resolve

    def fake(ws, ref, entries=None):
        if ref == only:
            if isinstance(result, Exception):
                raise result
            return result(real(ws, ref, entries))
        return real(ws, ref, entries)
    monkeypatch.setattr(store, "resolve", fake)


def test_factory_guard_parent_cases(factory, put, monkeypatch):
    from orch.errors import NotFoundError, UsageError
    fws, epic, child, other = factory
    from orch.core.model import new_ticket  # noqa: F401
    assert remote_gate.factory_guarded(fws, child) is True
    assert remote_gate.factory_guarded(fws, other) is False  # no parent
    # alias forms of the parent id cannot slip past
    for alias in (epic.lower(), "1", epic + " "):
        tid = put("backlog", parent=alias)
        assert remote_gate.factory_guarded(fws, tid) is True, alias
    assert remote_gate.factory_guarded(fws, put("backlog", parent="L-0999")) is False  # dangling parent
    assert remote_gate.factory_guarded(fws, "L-0998") is False  # no such ticket: the handler refuses it
    # an ambiguous parent, an unparseable parent entry and a read error are guarded
    odd = put("backlog", parent=other)
    _wrap_resolve(monkeypatch, other, UsageError("ambiguous"))
    assert remote_gate.factory_guarded(fws, odd) is True
    monkeypatch.undo()
    _wrap_resolve(monkeypatch, other, lambda e: type("E", (), {"meta": None, "path": e.path, "id": e.id})())
    assert remote_gate.factory_guarded(fws, odd) is True
    monkeypatch.undo()
    _wrap_resolve(monkeypatch, other, NotFoundError("gone"))
    assert remote_gate.factory_guarded(fws, odd) is False
    monkeypatch.undo()
    monkeypatch.setattr(store, "read_ticket", lambda *a, **k: (_ for _ in ()).throw(OSError("unreadable")))
    assert remote_gate.factory_guarded(fws, child) is True
    monkeypatch.undo()
    monkeypatch.setattr(store, "scan", lambda ws: (_ for _ in ()).throw(OSError("scan failed")))
    assert remote_gate.factory_guarded(fws, other) is True


def test_an_ambiguous_ticket_ref_is_guarded(factory, monkeypatch):
    from orch.errors import UsageError
    fws, epic, child, other = factory
    _wrap_resolve(monkeypatch, other, UsageError("ambiguous"))
    assert remote_gate.factory_guarded(fws, other) is True


def test_an_epic_that_is_not_factory_or_not_active_does_not_guard(configure, agent, human):
    from conftest import human_ops
    from orch.core.ops import Ops
    fws = configure(factory={"enabled": True})
    a, h = Ops(fws, agent), human_ops(fws, human)
    epic = a.new("Plain", type="epic")
    a.set_section(epic.id, "Requirements", "r")
    a.set_section(epic.id, "Acceptance criteria", "- [ ] a")
    h.approve(epic.id, "requirements", delegate={"max_children": 3})  # delegation, but not a factory
    child = a.new("c", epic=epic.id)
    assert remote_gate.factory_guarded(fws, child.id) is False
    assert remote_gate.factory_guarded(fws, "") is True and remote_gate.factory_guarded(fws, None) is True


# -- addon actions under a running factory epic ------------------------------------------------------------------------

class _Acts:
    def __init__(self):
        self.intent, self.ran = None, 0

    def act(self, action_id, target, ctx, **kw):
        self.ran += 1
        return self.intent


@pytest.fixture
def acting(factory):
    fws, epic, child, other = factory
    obj = _Acts()
    acts = [{"id": k, "label": k.title(), "tickets": True} for k in ("close", "reopen", "import", "plain")]
    fws._addons = AddonRegistry(fws, {"demo": loaded(fws, obj, name="demo", actions=acts,
                                                    remote_actions=[a["id"] for a in acts])})
    return create_app(fws, "tok"), obj, child, other


def _act(app, action, target, remote):
    return call(app, "POST", f"/addons/demo/actions/{action}", remote=remote, body=f"target={target}".encode(),
                headers=FORM)


@pytest.mark.parametrize("kind", ["close", "reopen", "import"])
def test_action_intents_under_a_factory_epic_need_a_fresh_assertion(acting, kind):
    app, obj, child, other = acting
    obj.intent = Intent(kind, ref=child, value="t", reason="r")
    status, headers, _ = _act(app, kind, child, origin(Scope.TYPE))
    assert status == 303 and b"fresh+confirmation" in headers[b"location"] and obj.ran == 0  # refused before addon code
    obj.ran = 0
    status, headers, _ = _act(app, kind, child, origin(Scope.TYPE, fresh=True))
    assert status == 303 and obj.ran == 1 and b"fresh+confirmation" not in headers[b"location"]


@pytest.mark.parametrize("kind", ["close", "reopen", "import"])
def test_action_intents_outside_a_factory_epic_and_locally_are_not_held(acting, kind):
    app, obj, child, other = acting
    obj.intent = Intent(kind, ref=other, value="t", reason="r")
    _, headers, _ = _act(app, kind, other, origin(Scope.TYPE))
    assert obj.ran == 1 and b"fresh+confirmation" not in headers[b"location"]
    obj.intent = Intent(kind, ref=child, value="t", reason="r")
    _, headers, _ = _act(app, kind, child, None)
    assert obj.ran == 2 and b"fresh+confirmation" not in headers[b"location"]


def test_an_action_intent_naming_a_guarded_ticket_other_than_its_target_is_held(acting):
    app, obj, child, other = acting
    obj.intent = Intent("close", ref=child, reason="r")  # the target is unrelated; the intent is not
    _, headers, _ = _act(app, "close", other, origin(Scope.TYPE))
    assert b"fresh+confirmation" in headers[b"location"]


def test_action_refusal_rules(factory):
    fws, epic, child, other = factory
    req = type("R", (), {"scope": {reach.SCOPE_KEY: origin(Scope.TYPE)}})()
    fresh = type("R", (), {"scope": {reach.SCOPE_KEY: origin(Scope.TYPE, fresh=True)}})()
    local = type("R", (), {"scope": {}})()
    for kind in ("answer", "approve", "move", "new", "bogus", None):
        assert remote_gate.action_refusal(req, fws, type("I", (), {"kind": kind, "ref": other})()) == remote_gate.NO_WAY
        assert remote_gate.action_refusal(fresh, fws, type("I", (), {"kind": kind, "ref": other})()) == remote_gate.NO_WAY
        assert remote_gate.action_refusal(local, fws, type("I", (), {"kind": kind, "ref": other})()) is None
    assert remote_gate.action_refusal(req, fws, Intent("none")) is None
    assert remote_gate.action_refusal(req, fws, Intent("close", ref=None)) == remote_gate.FRESH  # no ref: guarded
    assert remote_gate.action_refusal(fresh, fws, Intent("close", ref=None)) is None
    assert remote_gate.action_refusal(req, fws, Intent("close", ref=other)) is None
    assert remote_gate.action_target_refusal(req, fws, child) == remote_gate.FRESH
    assert remote_gate.action_target_refusal(fresh, fws, child) is None
    assert remote_gate.action_target_refusal(local, fws, child) is None
    assert remote_gate.action_target_refusal(req, fws, "") is None

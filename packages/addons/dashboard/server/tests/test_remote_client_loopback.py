"""R5 T3: the remote client's loopback server (design sections 1, 4, 7). Real uvicorn listeners on 127.0.0.1, a fake
end-to-end session (headers built by the product's own ``frames``), a static build in tmp_path."""

from __future__ import annotations

import ast
import json
import socket
from pathlib import Path

import pytest
from remote_client_kit import WS_A, WS_B, FakeSession, fake_client, make_static, raw

from orch_dashboard.remote_client import Controller, StaticSite
from orch_dashboard.remote_client import origin as origin_mod
from orch_dashboard.remote_client.listener import Listener, bind_loopback
from orch_dashboard.remote_client.origin import Origin

PKG = Path(origin_mod.__file__).parent


@pytest.fixture
def static(tmp_path):
    return StaticSite.load(make_static(tmp_path / "static"))


def serve(origin_factory):
    lst = Listener(bind_loopback())
    origin = origin_factory(lst.port)
    lst.serve(origin)
    return lst, origin


@pytest.fixture
def session_origin(static):
    sess = FakeSession()
    lst, origin = serve(lambda port: Origin(port=port, kind="session", static=static, session=sess))
    yield lst, origin, sess
    lst.stop()


def login(lst, origin):
    st, _, body = raw(
        lst.port,
        "POST",
        "/auth/session",
        headers={"Origin": origin.origin, "Content-Type": "application/json"},
        body=json.dumps({"code": origin.launch_code()}).encode(),
    )
    assert st == 200
    return json.loads(body)["token"]


# --- UI only from the local install ---


def test_ui_comes_only_from_the_installed_build(static, session_origin):
    lst, origin, sess = session_origin
    assert raw(lst.port, "GET", "/")[2].endswith(b"DASHBOARD</html>")
    assert raw(lst.port, "GET", "/assets/dash.js")[2] == b"console.log('dashboard')"
    st, h, body = raw(lst.port, "GET", "/w/DEMO/today", headers={"Accept": "text/html"})
    assert st == 200 and body.endswith(b"DASHBOARD</html>")  # the app's own routes get the shell
    assert raw(lst.port, "GET", "/assets/missing.js")[0] == 404
    assert raw(lst.port, "GET", "/remote-client.html")[0] in (404,)  # the other origin's page is not served here
    assert raw(lst.port, "GET", "/index.html")[0] == 404  # served at "/" only
    assert sess.requests == [] and sess.ws_opens == []  # not one frame for any non-/api path
    assert set(static.manifest()) >= {"/index.html", "/remote-client.html", "/assets/dash.js"}


def test_home_serves_its_own_page(static):
    lst, _ = serve(lambda port: Origin(port=port, kind="home", static=static, home_api=lambda *a: (200, {})))
    try:
        assert raw(lst.port, "GET", "/")[2].endswith(b"REMOTE</html>")
    finally:
        lst.stop()


def test_package_imports_no_http_client_and_not_the_dashboard_app():
    banned = {
        "httpx",
        "requests",
        "urllib.request",
        "http.client",
        "aiohttp",
        "urllib3",
        "orch_dashboard.app",
        "orch_dashboard.gateway",
    }
    for f in PKG.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for n in names:
                assert n not in banned and not any(n.startswith(b + ".") for b in banned), (f.name, n)


# --- Host / Origin / token ---


@pytest.mark.parametrize("host", ["localhost:{p}", "127.0.0.1:1", "evil.com", "127.0.0.1", "[::1]:{p}", None])
def test_wrong_host_is_refused(session_origin, host):
    lst, origin, sess = session_origin
    h = None if host is None else host.format(p=lst.port)
    st, *_ = raw(lst.port, "GET", "/", host=h) if h is not None else raw(lst.port, "GET", "/", host=None)
    assert st in (400, 403)
    assert sess.requests == []


@pytest.mark.parametrize(
    "origin_for", ["null", "http://127.0.0.1:1", "https://127.0.0.1:{p}", "http://localhost:{p}", None]
)
def test_wrong_origin_is_refused_on_every_change(session_origin, origin_for):
    lst, origin, sess = session_origin
    token = login(lst, origin)
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if origin_for:
        h["Origin"] = origin_for.format(p=lst.port)
    assert raw(lst.port, "POST", f"/api/w/{WS_A}/x", headers=h, body=b"{}")[0] == 403
    assert raw(lst.port, "POST", "/auth/session", headers=h, body=b"{}")[0] == 403
    assert sess.requests == []
    if origin_for:  # also on a GET, when one is present
        assert raw(lst.port, "GET", "/api/ping", headers=h)[0] == 403


def test_workspace_b_cannot_use_workspace_a(static):
    a, b = FakeSession(WS_A), FakeSession(WS_B)
    la, oa = serve(lambda p: Origin(port=p, kind="session", static=static, session=a))
    lb, ob = serve(lambda p: Origin(port=p, kind="session", static=static, session=b))
    try:
        assert la.port != lb.port  # each workspace is its own origin
        ta, tb = login(la, oa), login(lb, ob)
        ok = {"Authorization": f"Bearer {ta}", "Origin": oa.origin}
        assert raw(la.port, "GET", "/api/x", headers=ok)[0] == 200
        # a page of B (origin B) talking to A: refused by origin, and its token is not A's
        assert raw(la.port, "GET", "/api/x", headers={"Authorization": f"Bearer {tb}", "Origin": ob.origin})[0] == 403
        assert raw(la.port, "GET", "/api/x", headers={"Authorization": f"Bearer {tb}", "Origin": oa.origin})[0] == 401
        assert len(a.requests) == 1 and b.requests == []
    finally:
        la.stop()
        lb.stop()


def test_token_rules(session_origin):
    lst, origin, sess = session_origin
    h = {"Origin": origin.origin}
    assert raw(lst.port, "GET", "/api/x", headers=h)[0] == 401
    assert raw(lst.port, "GET", "/api/x", headers={**h, "Authorization": "Bearer nope"})[0] == 401
    assert raw(lst.port, "GET", "/api/x", headers={**h, "Authorization": "Basic abc"})[0] == 401
    assert raw(lst.port, "GET", "/api/x?token=abc", headers=h)[0] == 401
    code = origin.launch_code()
    body = json.dumps({"code": code}).encode()
    hh = {"Origin": origin.origin, "Content-Type": "application/json"}
    assert raw(lst.port, "POST", "/auth/session", headers=hh, body=body)[0] == 200
    assert raw(lst.port, "POST", "/auth/session", headers=hh, body=body)[0] == 401  # single use
    assert raw(lst.port, "POST", "/auth/session", headers=hh, body=b'{"code":"x"}')[0] == 401
    assert sess.requests == []


def test_launch_codes_expire():
    clock = [0.0]
    o = Origin(port=1, kind="home", static=StaticSite({}), clock=lambda: clock[0])
    c = o.launch_code()
    clock[0] = 61
    assert o._redeem(c) is None  # noqa: SLF001
    c2 = o.launch_code()
    assert o._redeem(c2) is not None  # noqa: SLF001


def test_no_cors_no_service_worker_and_security_headers_everywhere(session_origin):
    lst, origin, sess = session_origin
    token = login(lst, origin)
    sess.answer = (
        200,
        [
            ("content-type", "text/plain"),
            ("access-control-allow-origin", "*"),
            ("content-security-policy", "default-src *"),
            ("service-worker-allowed", "/"),
        ],
        [b"x"],
    )
    ok = {"Authorization": f"Bearer {token}", "Origin": origin.origin}
    answers = [
        raw(lst.port, "GET", "/"),
        raw(lst.port, "GET", "/nope"),
        raw(lst.port, "GET", "/api/x", headers=ok),
        raw(lst.port, "GET", "/api/x"),
        raw(
            lst.port,
            "OPTIONS",
            "/api/x",
            headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
        ),
        raw(lst.port, "GET", "/", host="evil.com"),
    ]
    assert answers[4][0] == 405
    for st, h, _ in answers:
        assert not [k for k in h if k.startswith("access-control-")] and "service-worker-allowed" not in h
        csp = h["content-security-policy"]
        assert "default-src 'self'" in csp and "worker-src 'none'" in csp and "frame-ancestors 'none'" in csp
        assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
        assert h["cross-origin-resource-policy"] == "same-origin" and h["cache-control"] == "no-store"
    assert "*" not in answers[2][1]["content-security-policy"]


def test_service_worker_and_cross_site_fetches_are_refused(session_origin):
    lst, origin, sess = session_origin
    assert raw(lst.port, "GET", "/", headers={"Service-Worker": "script"})[0] == 403
    assert raw(lst.port, "GET", "/", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert raw(lst.port, "GET", "/", headers={"Sec-Fetch-Site": "same-site"})[0] == 403
    assert raw(lst.port, "GET", "/", headers={"Sec-Fetch-Site": "same-origin"})[0] == 200
    # the launcher's redirect file (file://) makes the first load a cross-site NAVIGATION: only /go takes it, and it is
    # a fixed page that bounces same-origin; a deep link or the entry page from another site is refused
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    for deep in ("/", "/w/BILL/settings", "/assets/dash.js", "/api/remote-client/state", "/remote-client.html"):
        assert raw(lst.port, "GET", deep, headers=nav)[0] == 403, deep
    st, _, body = raw(lst.port, "GET", "/go", headers=nav)
    assert st == 200 and b"/go.js" in body and b"orch" not in body.lower()
    assert raw(lst.port, "GET", "/go.js", headers={"Sec-Fetch-Site": "same-origin"})[2] == origin_mod._GO_SCRIPT
    assert raw(lst.port, "POST", "/go", headers={**nav, "Origin": "http://evil.example"})[0] == 403


def test_the_home_origin_refuses_cross_site_deep_links_too_and_the_launch_still_works(static, tmp_path):
    opened = []
    ctl = Controller(fake_client(), static, opener=opened.append, computer_name="T", launch_dir=tmp_path / "l")
    try:
        ctl.start_home()
        ctl.open_home()
        port = ctl.home_listener.port
        nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        for deep in ("/", "/#x", "/settings", "/remote-client.html"):
            assert raw(port, "GET", deep, headers=nav)[0] == 403, deep
        target = _redirect_target(opened[0])
        assert target.endswith("/go#launch=" + target.split("launch=")[1]) and "/go#" in target
        assert raw(port, "GET", "/go", headers=nav)[0] == 200
        assert raw(port, "GET", "/", headers={"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate"})[0] == 200
    finally:
        ctl.stop()


@pytest.mark.parametrize("path", ["/api/%2e%2e/x", "/api/a%2Fb", "/api/../x", "/api//x", "/api/a;b"])
def test_escaped_or_dotted_paths_are_refused(session_origin, path):
    lst, origin, sess = session_origin
    token = login(lst, origin)
    st, *_ = raw(lst.port, "GET", path, headers={"Authorization": f"Bearer {token}", "Origin": origin.origin})
    assert st in (403, 404)
    assert sess.requests == []


# --- the header filter and the frames ---


def test_authorization_and_x_orch_headers_are_never_forwarded(session_origin):
    lst, origin, sess = session_origin
    token = login(lst, origin)
    st, h, body = raw(
        lst.port,
        "POST",
        f"/api/w/{WS_A}/click/prepare?a=1",
        body=b'{"action":"x"}',
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": origin.origin,
            "Content-Type": "application/json",
            "X-Orch-Click-Contract": "4",
            "X-Orch-Anything": "1",
            "Cookie": "a=b",
            "Accept": "application/json",
            "Referer": origin.origin + "/x",
            "User-Agent": "x",
        },
    )
    assert st == 200 and body == b'{"ok":true}'
    (got,) = sess.requests
    names = [n for n, _ in got["head"]["headers"]]
    assert set(names) <= {
        "accept",
        "accept-language",
        "content-type",
        "if-none-match",
        "if-modified-since",
        "range",
        "cache-control",
    }
    assert "content-type" in names and "accept" in names
    blob = json.dumps(got["head"]).lower()
    assert token.lower() not in blob and "authorization" not in blob and "x-orch" not in blob and "cookie" not in blob
    assert got["head"]["method"] == "POST" and got["head"]["path"] == f"/api/w/{WS_A}/click/prepare"
    assert got["head"]["query"] == "a=1" and got["body"] == b'{"action":"x"}'


def test_a_workspace_answer_cannot_set_a_cookie_or_widen_the_policy(session_origin):
    lst, origin, sess = session_origin
    token = login(lst, origin)
    sess.answer = (200, [("content-type", "text/html"), ("set-cookie", "a=b")], [b"<html></html>"])
    st, h, _ = raw(lst.port, "GET", "/api/x", headers={"Authorization": f"Bearer {token}", "Origin": origin.origin})
    assert st == 200 and "set-cookie" not in h


def test_an_ended_session_answers_410(session_origin):
    lst, origin, sess = session_origin
    token = login(lst, origin)
    sess.closed = "expired"
    assert (
        raw(lst.port, "GET", "/api/x", headers={"Authorization": f"Bearer {token}", "Origin": origin.origin})[0] == 410
    )


def test_a_big_body_is_refused(session_origin):
    lst, origin, sess = session_origin
    token = login(lst, origin)
    st, *_ = raw(
        lst.port,
        "POST",
        "/api/x",
        body=b"x" * (1 << 20 + 1) if False else b"x" * ((1 << 20) + 5),
        headers={"Authorization": f"Bearer {token}", "Origin": origin.origin},
    )
    assert st == 413 and sess.requests == []


# --- WebSocket ---


def ws_url(lst, path="/term"):
    return f"ws://127.0.0.1:{lst.port}{path}"


def test_websocket_origin_rules_and_relay(session_origin):
    from websockets.sync.client import connect

    lst, origin, sess = session_origin
    proto = ["orch.b", f"orch.b.{login(lst, origin)}"]
    for bad in ("http://evil.example", "null", "http://127.0.0.1:1", None):
        with pytest.raises(Exception):  # noqa: B017 - the handshake is refused (403 / closed before accept)
            with connect(ws_url(lst), origin=bad, subprotocols=proto, open_timeout=5):
                pass
    assert sess.ws_opens == []
    with connect(ws_url(lst) + "?x=1", origin=origin.origin, subprotocols=proto, open_timeout=5) as ws:
        assert ws.subprotocol == "orch.b"  # echoed without the token
        ws.send('{"ticket":"t"}')
        assert ws.recv(timeout=5) == 'echo:{"ticket":"t"}'
    assert sess.ws_opens and sess.ws_opens[0]["path"] == "/term" and sess.ws_opens[0]["query"] == "x=1"
    names = [n for n, _ in sess.ws_opens[0]["headers"]]
    assert "origin" not in names and "host" not in names and "cookie" not in names
    assert "sec-websocket-protocol" not in names  # the bearer is never forwarded
    assert not any(proto[1][7:] in str(v) for _, v in sess.ws_opens[0]["headers"])


def test_websocket_without_the_bearer_is_closed_before_any_frame(session_origin):
    from websockets.sync.client import connect

    lst, origin, sess = session_origin
    good = login(lst, origin)
    for protos in (
        None,
        ["orch.b", "orch.b.nope"],
        ["orch.b", "orch.b."],
        ["orch.b"],
        ["orch.b", f"x.{good}"],
        ["orch.b", f"orch.b.{good[:-1]}"],
        [f"orch.b.{good}"],
    ):
        with pytest.raises(Exception):  # noqa: B017 - right Origin, no / wrong bearer: refused
            with connect(ws_url(lst), origin=origin.origin, subprotocols=protos, open_timeout=5) as ws:
                ws.recv(timeout=2)
        assert sess.ws_opens == []
    assert sess.requests == []


def test_session_page_is_marked_remote(session_origin):
    lst, origin, sess = session_origin
    body = raw(lst.port, "GET", "/")[2]
    assert b'<head><meta name="orch-nav" content="remote">' in body


def test_go_page_carries_only_a_well_formed_launch_code():
    assert b"{43}" in origin_mod._GO_SCRIPT and b"test(location.hash)" in origin_mod._GO_SCRIPT


def test_websocket_message_over_the_frame_size_closes_1009(session_origin):
    from websockets.sync.client import connect

    lst, origin, sess = session_origin
    with connect(
        ws_url(lst),
        origin=origin.origin,
        subprotocols=["orch.b", f"orch.b.{login(lst, origin)}"],
        open_timeout=5,
        max_size=None,
    ) as ws:
        ws.send("x" * 70000)
        with pytest.raises(Exception) as ei:  # noqa: B017
            ws.recv(timeout=5)
        assert getattr(getattr(ei.value, "rcvd", None), "code", None) == 1009


# --- bind ---


def test_binds_127_0_0_1_only(session_origin):
    lst, origin, sess = session_origin
    assert lst.sock.getsockname()[0] == "127.0.0.1"
    if socket.has_ipv6:
        s = socket.socket(socket.AF_INET6)
        s.settimeout(2)
        try:
            assert s.connect_ex(("::1", lst.port)) != 0
        finally:
            s.close()


# --- the controller: home screens, names, prompts ---


def run_home(client, static, gate=None, opened=None):
    ctl = Controller(
        client,
        static,
        gate=gate,
        opener=(opened.append if opened is not None else (lambda u: None)),
        computer_name="Test Mac",
    )
    url = ctl.start_home()
    port = ctl.home_listener.port
    code = url.split("#launch=")[1]
    st, _, body = raw(
        port,
        "POST",
        "/auth/session",
        headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"},
        body=json.dumps({"code": code}).encode(),
    )
    assert st == 200
    token = json.loads(body)["token"]

    def api(method, path, doc=None):
        st, _, b = raw(
            port,
            method,
            "/api/remote-client" + path,
            body=None if doc is None else json.dumps(doc).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": f"http://127.0.0.1:{port}",
                "Content-Type": "application/json",
            },
        )
        return st, json.loads(b)

    return ctl, api


def test_home_needs_the_bearer(static):
    ctl, api = run_home(fake_client(), static)
    try:
        port = ctl.home_listener.port
        assert raw(port, "GET", "/api/remote-client/state", headers={"Origin": f"http://127.0.0.1:{port}"})[0] == 401
        assert api("GET", "/state")[0] == 200
    finally:
        ctl.stop()


def test_open_starts_one_origin_per_workspace_and_end_stops_it(static):
    client = fake_client(own_name="Billing")
    opened: list[str] = []
    ctl, api = run_home(client, static, opened=opened)
    try:
        assert api("POST", "/open", {"workspace_id": WS_A})[0] == 200
        (uri,) = opened
        assert "launch" not in uri.split("/")[-1].replace("launch-", "") and "#" not in uri  # no code in argv
        url = _redirect_target(uri).replace("/go#launch=", "/#launch=")
        assert url.startswith("http://127.0.0.1:") and "#launch=" in url
        sport = int(url.split(":")[2].split("/")[0])
        assert sport != ctl.home_listener.port
        assert api("GET", "/state")[1]["open"] == [WS_A]
        st, _, b = raw(
            sport,
            "POST",
            "/auth/session",
            headers={"Origin": f"http://127.0.0.1:{sport}", "Content-Type": "application/json"},
            body=json.dumps({"code": url.split("#launch=")[1]}).encode(),
        )
        assert st == 200
        assert (
            api("POST", "/open", {"workspace_id": WS_A})[0] == 200 and len(opened) == 2
        )  # open again: a new tab, no new session
        assert [c[0] for c in client.calls].count("open") == 1
        assert api("POST", "/end", {"workspace_id": WS_A})[0] == 200
        assert api("GET", "/state")[1]["open"] == []
        with pytest.raises(OSError):
            raw(sport, "GET", "/", timeout=2)
    finally:
        ctl.stop()


SIGNABLE = b"orch/v2/remote-open|" + bytes(16 + 16 + 32 + 32)  # a message the presence key would sign


def test_an_unsignable_message_shows_no_confirm_at_all():
    from orch.remoteclient import ClientError, Prompt

    from orch_dashboard.remote_client import GatedPresence

    class Inner:
        kind = "fido2"

        def assert_(self, *a):
            raise AssertionError("signed")

    gate = GatedPresence(Inner(), wait_s=0.3)
    with pytest.raises(ClientError) as e:
        gate.assert_("r", b"not-a-presence-message", Prompt("t", (), "r"))
    assert e.value.code == "malformed" and gate.current() is None


def test_the_confirm_screen_names_the_workspace_from_the_session_not_the_hub(static):
    import threading

    from orch.remoteclient import Prompt

    from orch_dashboard.remote_client import GatedPresence
    from orch_dashboard.remote_client import controller as C

    class Inner:
        kind = "platform"
        bound = False

        def create(self, name):
            raise AssertionError

        def assert_(self, ref, msg, prompt):
            return {"form": "p256", "sig": "s"}

    client = fake_client(hub_name="Hub Says Mallory", own_name="Billing service")
    gate = GatedPresence(Inner())
    ctl, api = run_home(client, static, gate=gate)
    try:
        out: list = []

        def ask():
            with C._for_workspace(WS_A):
                out.append(
                    gate.assert_(
                        "ref",
                        SIGNABLE,
                        Prompt("Confirm in Billing service", ("Start an agent on AT-12", "Open a terminal"), "r"),
                    )
                )

        t = threading.Thread(target=ask)
        t.start()
        for _ in range(100):
            p = api("GET", "/state")[1]["prompt"]
            if p:
                break
            import time

            time.sleep(0.05)
        assert p["workspace"] == "Billing service" and "Mallory" not in json.dumps(p)
        assert p["lines"] == ["Start an agent on AT-12", "Open a terminal"]
        assert api("POST", "/answer", {"id": p["id"], "approve": True})[1] == {"ok": True}
        t.join(5)
        assert out == [{"form": "p256", "sig": "s"}]
        assert api("GET", "/state")[1]["prompt"] is None
    finally:
        ctl.stop()


def test_a_declined_confirm_signs_nothing(static):
    import threading

    from orch.remoteclient import ClientError, Prompt

    from orch_dashboard.remote_client import GatedPresence

    signed = []

    class Inner:
        kind = "fido2"

        def assert_(self, *a):
            signed.append(a)
            return {}

    gate = GatedPresence(Inner(), wait_s=0.3)
    res: list = []

    def ask():
        try:
            gate.assert_("r", SIGNABLE, Prompt("t", (), "r"))
        except ClientError as e:
            res.append(e.code)

    t = threading.Thread(target=ask)
    t.start()
    for _ in range(100):
        p = gate.current()
        if p:
            gate.answer(p.id, False)
            break
    t.join(5)
    assert res == ["remote_client.cancelled"] and signed == []
    t2 = threading.Thread(target=ask)  # nobody answers: times out, still nothing signed
    t2.start()
    t2.join(5)
    assert res == ["remote_client.cancelled"] * 2 and signed == []


def test_enroll_flow_shows_six_words_then_added(static):
    import threading

    words_seen = threading.Event()
    finish = threading.Event()
    client = fake_client()

    def enroll(code, name, on_words, cancel=None):
        client.calls.append(("enroll", (name,), {}))
        on_words("maple river tiger cloud seven amber")
        words_seen.set()
        finish.wait(5)
        return {"workspace_id": WS_A, "device_id": "rc_x"}

    client.enroll = enroll
    ctl, api = run_home(client, static)
    try:
        import orch_dashboard.remote_client.controller as C

        class Code:
            workspace_id = WS_A

        orig = C._enroll.parse_code
        C._enroll.parse_code = lambda t: Code() if t.startswith("orch-rc1.") else orig(t)
        try:
            st, doc = api("POST", "/enroll", {"code": "  orch-rc1.ok  "})
            assert st == 200 and doc["workspace_id"] == WS_A
            words_seen.wait(5)
            s = api("GET", f"/enroll/{doc['id']}")[1]
            assert s["status"] == "words" and s["words"] == ["maple", "river", "tiger", "cloud", "seven", "amber"]
            finish.set()
            for _ in range(100):
                s = api("GET", f"/enroll/{doc['id']}")[1]
                if s["status"] == "added":
                    break
                import time

                time.sleep(0.05)
            assert s["status"] == "added"
            assert client.calls[0][1] == ("Test Mac",)
        finally:
            C._enroll.parse_code = orig
        st, err = api("POST", "/enroll", {"code": "orch-rb1.something"})
        assert st == 409 and err["error"]["message"] == "This code is for a browser. Make a code for a computer."
    finally:
        finish.set()
        ctl.stop()


def test_sign_in_error_does_not_say_which_field(static):
    from orch.remoteclient import ClientError

    client = fake_client()

    def bad(*a, **k):
        raise ClientError("no_session")

    client.connect_hub = bad
    ctl, api = run_home(client, static)
    try:
        from orch.hubclient import connectlink

        import orch_dashboard.remote_client.controller as C

        orig = connectlink.resolve
        connectlink.resolve = lambda text, **k: {
            "endpoint": "hub.example:8444",
            "hub_card": {"tls_spki_sha256": "p" * 43},
        }
        orig_parse = connectlink.parse_link
        connectlink.parse_link = lambda t: type("L", (), {"origin": "https://hub.example"})()
        try:
            st, err = api(
                "POST",
                "/connect",
                {"link": "https://hub.example/c#h1.x", "user": "u", "password": "p", "code": "123456"},
            )
        finally:
            connectlink.resolve, connectlink.parse_link = orig, orig_parse
        assert st == 400 and err["error"]["message"] == "Check your details and try again."
        assert "password" not in err["error"]["message"].lower() and "user" not in err["error"]["message"].lower()
        assert C  # imported
    finally:
        ctl.stop()


def test_the_client_refuses_an_agent_environment():
    import io

    from orch_dashboard.remote_client.__main__ import main

    err = io.StringIO()
    assert main([], env={"ORCH_SESSION": "s"}, err=err) == 3
    assert "never opens it" in err.getvalue()


def _redirect_target(uri: str) -> str:
    """The address inside the private redirect file the browser was given."""
    from urllib.parse import unquote, urlparse

    text = open(unquote(urlparse(uri).path)).read()
    return json.loads(text.split("location.replace(")[1].split(");")[0])


def test_the_browser_is_given_a_private_redirect_file_never_the_code(static, tmp_path):
    import os
    import stat

    opened: list[str] = []
    ctl = Controller(fake_client(), static, opener=opened.append, computer_name="T", launch_dir=tmp_path / "launch")
    try:
        ctl.start_home()
        ctl.open_home()
        (uri,) = opened
        target = _redirect_target(uri)
        code = target.split("#launch=")[1]
        assert code not in uri and uri.startswith("file://")
        path = tmp_path / "launch" / uri.rsplit("/", 1)[1]
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(tmp_path / "launch").st_mode) == 0o700
        port = ctl.home_listener.port
        st, _, _ = raw(
            port, "POST", "/auth/session",
            headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"},
            body=json.dumps({"code": code}).encode(),
        )  # fmt: skip
        assert st == 200
        assert not path.exists()  # gone once the code is used
    finally:
        ctl.stop()


def test_a_redirect_file_that_is_never_used_expires_and_stale_ones_are_swept(tmp_path):
    from orch_dashboard.remote_client.launcher import Launcher

    d = tmp_path / "l"
    (d).mkdir(mode=0o700)
    (d / "launch-old.html").write_text("x")
    lc = Launcher(lambda u: True, d, ttl=0.2)
    assert not (d / "launch-old.html").exists()  # swept at start
    lc.open("k", "http://127.0.0.1:1/#launch=abc")
    (f,) = list(d.glob("launch-*.html"))
    import time

    time.sleep(0.6)
    assert not f.exists()
    d.chmod(0o755)
    with pytest.raises(RuntimeError):
        Launcher(lambda u: True, d)


def test_stop_ends_the_enrollment_and_adds_nothing(static):
    import threading
    import time

    from orch.remoteclient import ClientError

    seen = {}
    started = threading.Event()
    client = fake_client()
    client.added = []

    def enroll(code, name, on_words, cancel=None):
        on_words("maple river tiger cloud seven amber")
        started.set()
        while not cancel.is_set():
            time.sleep(0.02)
        seen["cancelled"] = True
        raise ClientError("remote_client.cancelled")  # the core closes the stream and stores nothing

    client.enroll = enroll
    ctl, api = run_home(client, static)
    import orch_dashboard.remote_client.controller as C

    class Code:
        workspace_id = WS_A

    orig = C._enroll.parse_code
    C._enroll.parse_code = lambda t: Code()
    try:
        st, doc = api("POST", "/enroll", {"code": "orch-rc1.ok"})
        assert started.wait(5)
        assert api("POST", f"/enroll/{doc['id']}/stop")[0] == 200
        for _ in range(100):
            if seen:
                break
            time.sleep(0.02)
        assert seen == {"cancelled": True}
        s = api("GET", f"/enroll/{doc['id']}")[1]
        assert s["status"] == "stopped" and client.added == []
    finally:
        C._enroll.parse_code = orig
        ctl.stop()


def test_the_security_key_pin_is_asked_on_the_home_page_and_used_once(static):
    import threading
    import time

    from orch_dashboard.remote_client import PinAsker

    pin = PinAsker(wait_s=5)
    ctl, api = run_home(fake_client(), static)
    ctl.pin = pin
    got = []
    t = threading.Thread(target=lambda: got.append(pin.ask()))
    try:
        assert api("GET", "/state")[1]["pin"] is None  # nothing asks: no PIN screen
        t.start()
        for _ in range(100):
            ident = api("GET", "/state")[1]["pin"]
            if ident:
                break
            time.sleep(0.02)
        assert ident
        assert api("POST", "/pin", {"id": "wrong", "pin": "1234"})[1] == {"ok": False}
        assert api("POST", "/pin", {"id": ident, "pin": "1234"})[1] == {"ok": True}
        t.join(5)
        assert got == ["1234"]
        assert api("GET", "/state")[1]["pin"] is None and pin._pin is None  # nothing is kept
        assert api("POST", "/pin", {"id": ident, "pin": 12})[0] == 400
    finally:
        ctl.stop()


def test_cancelling_the_pin_prompt_gives_the_key_nothing(static):
    import threading

    from orch_dashboard.remote_client import PinAsker

    pin = PinAsker(wait_s=5)
    got = []
    t = threading.Thread(target=lambda: got.append(pin.ask()))
    t.start()
    import time

    while not pin.current():
        time.sleep(0.01)
    assert pin.answer(pin.current(), None)
    t.join(5)
    assert got == [None]

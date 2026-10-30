"""H1: the hub switch routes. The hub is the authority for a ticket; this process only relays the host's answer and
opens a `switch` session (not `local`) for an `open` ticket."""

import json

import pytest
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient

from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.errors import HostError, fail
from orch_dashboard.gateway import Gateway

WS = "a" * 32
TICKET = WS + "." + "b" * 32


class Link:
    def __init__(self):
        self.calls, self.answers = [], {}

    def supports(self, op, cap="ops/1"):
        return op == "hub.switch"

    def call(self, op, args=None, **kw):
        self.calls.append(args)
        got = self.answers.get(args["kind"])
        if isinstance(got, Exception):
            raise got
        return got


class Gw(Gateway):
    def __init__(self, link, state):
        super().__init__(state)
        self._l = link

    def enabled(self, ws):
        if ws != WS:
            raise fail("not_found")
        return self._l


@pytest.fixture
def link():
    return Link()


@pytest.fixture
def auth():
    return Auth()


@pytest.fixture
def client(link, auth, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    with TestClient(create_app(PORT, Gw(link, state), auth, static), base_url=ORIGIN) as c:
        yield c


H = {"Origin": ORIGIN}


def test_an_open_ticket_opens_a_switch_session_that_is_not_local(client, link, auth):
    link.answers["redeem"] = {"user_id": "u", "name": "Ann", "return_to": "/w/x/tickets", "kind": "open"}
    res = client.post("/auth/switch", json={"t": TICKET}, headers=H)
    assert res.status_code == 200
    body = res.json()
    assert body["return_to"] == "/w/x/tickets" and body["workspace_id"] == WS
    assert auth.login_kind(body["token"]) == "switch"
    assert link.calls == [{"kind": "redeem", "ticket": TICKET}]


@pytest.mark.parametrize("bad", ["//evil.test", "https://evil.test", "/a/../b", "/a\\b", "x"])
def test_the_landing_path_is_ours_only(client, link, bad):
    link.answers["redeem"] = {"user_id": "u", "name": None, "return_to": bad, "kind": "open"}
    assert client.post("/auth/switch", json={"t": TICKET}, headers=H).json()["return_to"] == "/"


def test_a_signout_ticket_opens_nothing(client, link, auth):
    link.answers["redeem"] = {"user_id": "u", "name": None, "return_to": "/", "kind": "signout"}
    assert client.post("/auth/switch", json={"t": TICKET}, headers=H).json() == {"signed_out": True}
    assert not auth.sessions


def test_a_refused_ticket_is_one_plain_401(client, link, auth):
    link.answers["redeem"] = fail("hub.refused", "the hub is refused: bad_ticket")
    res = client.post("/auth/switch", json={"t": TICKET}, headers=H)
    assert res.status_code == 401 and not auth.sessions
    other = "c" * 32 + "." + "d" * 32  # a workspace this dashboard does not serve
    assert client.post("/auth/switch", json={"t": other}, headers=H).status_code == 401


@pytest.mark.parametrize("t", ["x", WS, WS + "." + "B" * 32, TICKET + "0"])
def test_a_malformed_ticket_never_reaches_the_host(client, link, t):
    assert client.post("/auth/switch", json={"t": t}, headers=H).status_code == 400
    assert link.calls == []


def test_ticket_and_rail_need_a_session_and_relay_closed_bodies(client, link, auth):
    token = client.post("/auth/session", json={"code": auth.launch(PORT)["code"]}, headers=H).json()["token"]
    h = {"Authorization": "Bearer " + token, "Origin": ORIGIN}
    assert client.post(f"/api/w/{WS}/hub/switch", json={}, headers={"Origin": ORIGIN}).status_code == 401
    link.answers["ticket"] = {"ticket": "t" * 32, "url": "https://hub.test/#t=" + "t" * 32}
    got = client.post(f"/api/w/{WS}/hub/switch", json={"return_to": "//evil", "kind": "signout"}, headers=h)
    assert got.json() == {"url": "https://hub.test/#t=" + "t" * 32}
    assert link.calls[-1] == {"kind": "ticket", "origin": ORIGIN, "return_to": "/", "ticket_kind": "signout"}
    link.answers["rail"] = {
        "hub": {"name": "Acme", "origin": "https://hub.test"},
        "user_id": "u",
        "workspaces": [
            {"workspace_id": WS, "name": "Product", "role": "owner", "online": True, "badge": 3,
             "origin": ORIGIN, "current": True, "secret": "x"},
            {"workspace_id": "w2", "name": None, "role": "edit", "online": False, "badge": -4,
             "origin": "javascript:1", "current": False},
        ],
    }  # fmt: skip
    rail = client.get(f"/api/w/{WS}/hub/rail", headers=h).json()
    assert rail["hub"] == {"name": "Acme", "origin": "https://hub.test"}
    assert rail["workspaces"][0] == {
        "workspace_id": WS, "name": "Product", "role": "owner", "online": True, "badge": 3,
        "origin": ORIGIN,
        "current": True,
    }  # fmt: skip
    assert rail["workspaces"][1]["origin"] is None and rail["workspaces"][1]["badge"] == 0


def test_no_hub_means_an_empty_rail(client, link, auth):
    token = client.post("/auth/session", json={"code": auth.launch(PORT)["code"]}, headers=H).json()["token"]
    link.answers["rail"] = fail("hub.not_joined")
    got = client.get(f"/api/w/{WS}/hub/rail", headers={"Authorization": "Bearer " + token})
    assert got.json() == {"hub": None, "workspaces": []}


def _switch_token(client, link):
    link.answers["redeem"] = {"user_id": "u", "name": None, "return_to": "/", "kind": "open"}
    return client.post("/auth/switch", json={"t": TICKET}, headers=H).json()["token"]


def test_a_switch_session_reads_only_its_own_workspace_and_never_a_shell(client, link):
    h = {"Authorization": "Bearer " + _switch_token(client, link), "Origin": ORIGIN}
    other = "c" * 32
    assert client.get(f"/api/w/{other}/head", headers=h).status_code == 403
    assert client.get(f"/api/events?ws={other}", headers=h).status_code == 403
    assert client.post(f"/api/w/{WS}/terminals/open", json={}, headers=h).status_code == 403
    assert client.post(f"/api/w/{WS}/runs/x/attach", json={}, headers=h).status_code == 403
    own = client.get(f"/api/w/{WS}/hub/rail", headers=h)
    assert own.status_code != 403


def test_ticket_tries_are_limited_and_a_refusal_costs_more(client, link):
    link.answers["redeem"] = fail("hub.refused", "x")
    codes = [client.post("/auth/switch", json={"t": TICKET}, headers=H).status_code for _ in range(8)]
    assert codes[0] == 401 and 429 in codes  # the loopback limiter (10 a minute) runs out early: two charges each


def test_remote_logout_everywhere_ends_switch_sessions(client, link, auth):
    token = _switch_token(client, link)
    auth.logout_everywhere()
    assert not auth.valid(token)


def test_every_route_a_switch_session_may_not_use_is_closed(client, link):
    """New routes default CLOSED for a switch session: only the table in security.py opens one."""
    from orch_dashboard.security import SWITCH_GLOBAL, switch_allows

    bound = WS
    seen = 0
    for route in client.app.routes:
        template = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or {"GET"}
        if not template.startswith(("/api", "/auth")):
            continue
        for method in methods - {"HEAD", "OPTIONS"}:
            seen += 1
            path = template.replace("{ws}", bound)
            for name in ("key", "run", "name", "page", "tab", "ticket", "id"):
                path = path.replace("{" + name + "}", "x")
            path = path.replace("{route:path}", "x").replace("{path:path}", "x")
            query = {"ws": [bound]}
            want = (method, template) in SWITCH_GLOBAL or (
                template.startswith("/api/w/{ws}/")
                and "/terminals" not in template
                and not template.endswith("/attach")
                and (method in ("GET", "HEAD") or template == "/api/w/{ws}/hub/switch")
            )
            assert switch_allows(method, path, bound, query) == want, (method, template)
    assert seen > 50
    assert not switch_allows("GET", "/api/w/" + "c" * 32 + "/head", bound)
    assert not switch_allows("GET", "/api/events", bound, {"ws": ["c" * 32]})
    for closed in ("/api/trusted", "/api/testrun/x", "/auth/passkey/options", "/api/passkeys"):
        assert not switch_allows("GET", closed, bound) and not switch_allows("POST", closed, bound)


def test_a_switch_token_cannot_write_anywhere_on_its_workspace(client, link):
    """B1: read-only. Every non-GET workspace route, the settings, market, testrun and hand-off writes among them, is
    refused for a switch session before it is routed; only the ticket to the hub is open."""
    h = {"Authorization": "Bearer " + _switch_token(client, link), "Origin": ORIGIN}
    walked = 0
    for route in client.app.routes:
        template = getattr(route, "path", "")
        if not template.startswith("/api/w/{ws}/") or template == "/api/w/{ws}/hub/switch":
            continue
        for method in (getattr(route, "methods", None) or set()) - {"GET", "HEAD", "OPTIONS"}:
            walked += 1
            path = template.replace("{ws}", WS)
            for name in ("key", "run", "name", "page", "tab", "ticket", "id", "route:path", "path:path"):
                path = path.replace("{" + name + "}", "x")
            res = client.request(method, path, json={}, headers=h)
            assert res.status_code == 403, (method, template, res.status_code)
    assert walked > 40


def test_a_switch_session_also_works_on_loopback_but_only_as_a_reader(client, link, auth):
    """B4, chosen on purpose: the dev setups reach the dashboard on loopback or a LAN address, so /auth/switch works
    there; the read-only allowlist is the only gate (no route_gate on loopback)."""
    token = _switch_token(client, link)
    assert auth.listener_of(token) == "loopback" and auth.login_kind(token) == "switch"
    h = {"Authorization": "Bearer " + token, "Origin": ORIGIN}
    link.answers["rail"] = {"hub": {"name": "Acme", "origin": None}, "user_id": "u", "workspaces": []}
    assert client.get(f"/api/w/{WS}/hub/rail", headers=h).status_code == 200
    assert client.post(f"/api/w/{WS}/settings", json={}, headers=h).status_code == 403


def test_p01_the_403_on_a_hub_switch_is_never_an_origin_problem_of_the_configured_address(client, link):
    """run-15 J02-02 reproduced: a POST /auth/switch whose Origin is this dashboard's own origin opens (no origin.bad).
    A tunnel or another address in the Host is refused as host.bad (421) before any origin test, and an Origin that is
    not the Host's own is origin.bad (403): both stay refused on purpose, the guard is unchanged. The 403 the tester
    saw is the host's own refusal `switch.refused` (D93), which the failure page now says in plain words."""
    link.answers["redeem"] = {"user_id": "u", "name": "Ann", "return_to": "/", "kind": "open"}
    assert client.post("/auth/switch", json={"t": TICKET}, headers=H).status_code == 200
    via_tunnel = {"Origin": "http://127.0.0.1:47821", "Host": "127.0.0.1:47821"}
    tunnel = client.post("/auth/switch", json={"t": TICKET}, headers=via_tunnel)
    assert tunnel.status_code == 421 and tunnel.json()["error"]["code"] == "host.bad"
    foreign = client.post("/auth/switch", json={"t": TICKET}, headers={"Origin": "http://127.0.0.1:47821"})
    assert foreign.status_code == 403 and foreign.json()["error"]["code"] == "origin.bad"


def _rail_read(client, link, auth, origin="https://hub.test"):
    """The authenticated rail read that fills the host-state cache."""
    link.answers["rail"] = {"hub": {"name": "Acme", "origin": origin}, "workspaces": []}
    token = _switch_token(client, link)
    link.calls.clear()
    assert client.get(f"/api/w/{WS}/hub/rail", headers={"Authorization": "Bearer " + token}).status_code == 200


def _refuse(client, link):
    link.answers["redeem"] = HostError({"code": "switch.refused", "message": "x", "retryable": False})
    link.calls.clear()
    return client.post("/auth/switch", json={"t": TICKET}, headers=H)


def test_p01_without_a_cached_hub_origin_the_failure_has_none_and_makes_no_link_call_for_it(client, link):
    refused = _refuse(client, link)
    assert refused.status_code == 403 and "hub_origin" not in refused.json()["error"]
    assert [c["kind"] for c in link.calls] == ["redeem"]  # no rail call on a pre-login request


def test_p01_loopback_failure_names_the_hub_from_the_cached_rail_read_only(client, link, auth):
    _rail_read(client, link, auth)
    refused = _refuse(client, link)
    assert refused.json()["error"]["hub_origin"] == "https://hub.test"
    assert [c["kind"] for c in link.calls] == ["redeem"]  # cached: still no rail call
    link.answers["redeem"] = HostError({"code": "ticket.used", "message": "x", "retryable": False})
    assert (
        client.post("/auth/switch", json={"t": TICKET}, headers=H).json()["error"]["hub_origin"] == "https://hub.test"
    )


def test_p01_only_a_closed_http_origin_is_ever_cached(client, link, auth):
    for bad in ("javascript:alert(1)", "https://hub.test/path", "https://a b", None):
        _rail_read(client, link, auth, origin=bad)
        link.calls.clear()
        assert "hub_origin" not in _refuse(client, link).json()["error"]


def test_p01_remote_listeners_never_carry_the_hub_origin():
    from orch_dashboard.policy import LOOPBACK, REMOTE
    from orch_dashboard.switch import switch_failure

    assert json.loads(switch_failure("switch.refused", 403, "x", "https://hub.test", LOOPBACK).body)["error"][
        "hub_origin"
    ]
    for listener in (REMOTE, "remote-device"):
        res = switch_failure("switch.refused", 403, "x", "https://hub.test", listener)
        assert "hub_origin" not in json.loads(res.body)["error"] and res.status_code == 403


def _asgi_post(app, server, host, origin=None):
    """A real ASGI call with the accepting socket's address in the scope (what uvicorn sets)."""
    import asyncio

    body = json.dumps({"t": TICKET}).encode()
    headers = [
        (b"host", host.encode()),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if origin:
        headers.append((b"origin", origin.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": "/auth/switch", "raw_path": b"/auth/switch", "query_string": b"", "headers": headers,
             "server": server, "client": ("192.168.1.99", 5555), "root_path": ""}  # fmt: skip
    out = {"status": None, "body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], json.loads(out["body"] or b"{}")


def test_p01_locality_comes_from_the_accepting_socket_not_the_host_header(link, auth, tmp_path):
    """`serve --listen` is a second socket of the loopback policy. A network client may send any Host."""
    lan = "192.168.1.20:8151"
    own = f"127.0.0.1:{PORT}"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    app = create_app(PORT, Gw(link, state), auth, static, alt_hosts=(lan,))
    with TestClient(app, base_url=ORIGIN) as c:
        _rail_read(c, link, auth)
        link.answers["redeem"] = HostError({"code": "switch.refused", "message": "x", "retryable": False})
        # accepted on the loopback socket with the loopback Host: refused by the host, hub_origin included
        status, got = _asgi_post(app, ("127.0.0.1", PORT), own, ORIGIN)
        assert status == 403 and got["error"]["hub_origin"] == "https://hub.test"
        # accepted on the network socket but claiming the loopback Host: 421 host.bad, nothing learned
        status, got = _asgi_post(app, ("192.168.1.20", PORT), own, ORIGIN)
        assert status == 421 and "hub_origin" not in got.get("error", {})
        # the network address on the network socket: served, but never with the hub origin
        status, got = _asgi_post(app, ("192.168.1.20", PORT), lan, "http://" + lan)
        assert status == 403 and "hub_origin" not in got["error"]
        # an alt host claimed on the loopback socket (an ssh tunnel with a forged Host): 421
        status, got = _asgi_post(app, ("127.0.0.1", PORT), lan, "http://" + lan)
        assert status == 421

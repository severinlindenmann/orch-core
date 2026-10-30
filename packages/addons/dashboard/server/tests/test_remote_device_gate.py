"""R3 T3 (orch-core #1591; spec D89/D90): the `remote-device` route column and the stream's listener policy, enforced
in `Security.route_check`. A stream is answered in-process; here a stand-in for the adapter (`device_kit.Adapter`)
builds the scope the real one will."""

# ruff: noqa: F811

import json
import re
from pathlib import Path

import pytest
from conftest import PORT
from device_kit import (  # noqa: F401
    D90_REFUSED,
    DEVICE,
    HOST,
    PERSON,
    STREAM,
    app,
    click_start,
    device,
    fake,
    passkey_store,
    stream_client,
    ws,
)
from fastapi.routing import APIRoute, APIWebSocketRoute
from orch.click.protocol import ClickError
from starlette.websockets import WebSocketDisconnect

from orch_dashboard import routegate
from orch_dashboard.policy import REMOTE_DEVICE, remote_device_policy
from orch_dashboard.remote_identity import RemoteIdentity, route_digest
from orch_dashboard.routegate import (
    BY_KIND,
    LEVELS,
    PRESENCE,
    REFUSED,
    TABLE,
    VIEW_WRITES,
    rule_device,
)
from orch_dashboard.routegate import (
    REMOTE_DEVICE as COLUMN,
)

#: routes R3 T4 adds (the handlers are not there yet); their column rows exist so they are decided before they exist
T4_ROWS = {
    ("POST", "/api/w/{ws}/remote/presence/nonce"),
    ("POST", "/api/w/{ws}/remote/presence"),
    ("POST", "/api/w/{ws}/remote/sessions/{stream_id}/end"),
}
CONTRACT = Path(__file__).resolve().parents[5] / "contracts" / "hub"
GATE_REFUSED = "remote.refused"


def app_routes(app):
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield from ((m, route.path) for m in sorted(route.methods) if m not in ("HEAD", "OPTIONS"))
        elif isinstance(route, APIWebSocketRoute):
            yield ("WEBSOCKET", route.path)


def code(res):
    return res.json().get("error", {}).get("code")


def fill(path, ws):
    return path.replace("{ws}", ws)


def concrete(template, ws):
    return fill(re.sub(r"\{(?!ws\})[^}]*\}", "x", template), ws)


# --- the walk ---
def test_route_walk_every_route_has_device_entry(app):
    routes = set(app_routes(app))
    assert routes - set(COLUMN) == set(), "a route without a remote-device rule: add it to routegate.REMOTE_DEVICE"
    assert set(COLUMN) - routes == T4_ROWS & (set(COLUMN) - routes), "a column row for a route that does not exist"
    assert (set(COLUMN) - routes) <= T4_ROWS
    assert set(TABLE) <= set(COLUMN)
    for rule, level in COLUMN.values():
        assert rule in routegate.DEVICE_RULES and level in LEVELS


def test_the_by_kind_rows_are_exactly_the_signing_steps():
    W = routegate.W
    assert {k for k, (rule, _) in COLUMN.items() if rule == BY_KIND} == {
        ("POST", W + "/click/" + step) for step in ("prepare", "sign", "view", "cancel", "state")
    }


def test_presence_rows_are_the_agent_work_and_the_terminals():
    W = routegate.W
    assert {k for k, (rule, _) in COLUMN.items() if rule == PRESENCE} == {
        ("POST", W + "/agents/start"),
        ("POST", W + "/runs/{run}/link"),
        ("POST", W + "/runs/{run}/detach"),
        ("POST", W + "/runs/{run}/attach"),
        ("POST", W + "/terminals/open"),
        ("POST", W + "/terminals/preview"),
        ("WEBSOCKET", "/term"),
    }
    for key, (rule, level) in COLUMN.items():
        if rule == PRESENCE and key[1] != W + "/agents/start" and "link" not in key[1] and "detach" not in key[1]:
            assert level == "terminals", key  # a terminal route is a Terminals-level route


# --- D90: refused at every level, from an explicit list ---
@pytest.mark.parametrize("level", LEVELS)
def test_d90_list_refused_at_every_level(app, auth, ws, level):
    client = stream_client(app, auth, ws, level=level, stream=level.encode().hex().ljust(32, "0"))[0]
    for method, path in D90_REFUSED:
        res = client.request(method, fill(path, ws), json={})
        # `/auth/session` and the widget frame are switched off by the policy itself: 404, before any table
        off = path in ("/auth/session",) or path.startswith("/widget-frame")
        assert (res.status_code == 404) if off else (res.status_code == 403 and code(res) == GATE_REFUSED), (
            level,
            method,
            path,
            res.status_code,
            res.text[:120],
        )


def test_d90_refused_in_the_column_itself_at_every_level(app, ws):
    """The same list, through the template the real app matches: the column row says refused, whatever the level."""
    gate = routegate.RouteGate(app.router)
    for method, path in D90_REFUSED:
        scope = {"type": "http", "method": method, "path": fill(path, ws), "headers": [], "query_string": b""}
        template = gate.template(scope)
        for level in LEVELS:
            rule, refusal = rule_device(method, template, level) if template else (REFUSED, GATE_REFUSED)
            assert (rule, refusal) == (REFUSED, GATE_REFUSED), (level, method, path, template)


def test_new_route_default_refused(app, auth, ws):
    @app.post("/api/w/{ws}/brand-new")
    def brand_new(ws: str):
        return {"ok": True}

    assert ("POST", "/api/w/{ws}/brand-new") not in COLUMN
    client = stream_client(app, auth, ws, level="terminals")[0]
    res = client.post(f"/api/w/{ws}/brand-new", json={})
    assert res.status_code == 403 and code(res) == GATE_REFUSED
    res = client.post(f"/api/w/{ws}/no-such-route", json={})
    assert res.status_code == 403 and code(res) == GATE_REFUSED
    assert rule_device("POST", "/api/w/{ws}/brand-new", "terminals") == (REFUSED, GATE_REFUSED)
    assert rule_device("GET", "/api/w/{ws}/today", "godmode") == (REFUSED, GATE_REFUSED)  # a level that is not ours


# --- levels ---
def test_view_level_reads_only():
    for (method, template), (rule, _) in COLUMN.items():
        got, refusal = rule_device(method, template, "view")
        if rule == REFUSED:
            assert (got, refusal) == (REFUSED, GATE_REFUSED)
        elif method in ("GET", "HEAD") or (method, template) in VIEW_WRITES:
            assert refusal is None, (method, template)
        else:
            assert refusal == "remote.level", (method, template)  # a write, a presence route or a signing step
    assert VIEW_WRITES == {("POST", routegate.W + "/remote/off")}  # D84: off never needs more than on


def test_level_work_has_no_terminal_and_terminals_has_everything_listed():
    for (method, template), (rule, least) in COLUMN.items():
        if rule == REFUSED:
            continue
        assert rule_device(method, template, "terminals")[1] is None, (method, template)
        wanted = None if least != "terminals" else "remote.level"
        assert rule_device(method, template, "work")[1] == wanted or method in ("GET",) and least == "view", (
            method,
            template,
        )


def test_view_level_reads_only_over_http(app, auth, ws):
    client = stream_client(app, auth, ws, level="view", stream="77" * 16)[0]
    res = client.post(f"/api/w/{ws}/wiki/create", json={})
    assert res.status_code == 403 and code(res) == "remote.level"
    res = client.post(f"/api/w/{ws}/click/prepare", json={})
    assert res.status_code == 403 and code(res) == "remote.level"
    res = client.post(f"/api/w/{ws}/agents/start", json={})
    assert res.status_code == 403 and code(res) == "remote.level"  # level first: no presence is asked for
    assert code(client.get(f"/api/w/{ws}/today")) != GATE_REFUSED
    assert client.get(f"/api/w/{ws}/today").status_code != 403
    assert code(client.post(f"/api/w/{ws}/remote/off", json={})) not in (GATE_REFUSED, "remote.level")


def test_work_level_terminals_are_a_higher_level(app, auth, ws):
    client = stream_client(app, auth, ws, level="work", stream="88" * 16)[0]
    res = client.post(f"/api/w/{ws}/terminals/open", json={})
    assert res.status_code == 403 and code(res) == "remote.level"


# --- inside a stream: nothing before login, no passkeys, no switch ---
@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/auth/switch", "POST"),
        ("/auth/switch/challenge", "POST"),
        ("/auth/passkey/options", "POST"),
        ("/auth/passkey/login", "POST"),
        ("/auth/enroll/options", "POST"),
        ("/auth/enroll/register", "POST"),
        ("/api/passkeys", "GET"),
        ("/api/stepup/options", "POST"),
        ("/api/stepup/verify", "POST"),
    ],
)
def test_auth_switch_and_passkey_routes_refused_in_stream(app, auth, ws, device, path, method):
    res = device.request(method, path, json={})
    assert res.status_code == 403 and code(res) == GATE_REFUSED
    assert remote_device_policy().pre_login(path, path.encode(), method) is False  # no route answers before login


def test_no_pre_login_surface_in_the_policy():
    policy = remote_device_policy()
    assert policy.name == REMOTE_DEVICE == "remote-device" and policy.stream and policy.route_gate
    for path in ("/", "/assets/main-hash.js", "/auth/passkey/login", "/auth/enroll/register", "/index.html"):
        assert policy.pre_login(path, path.encode(), "GET") is False
    assert policy.hosts_for(True) == ("orch-remote",)  # even the setup-page hosts: there are none
    assert not policy.launch_codes and not policy.widget_frames
    assert policy.session_kind_ok("remote-device")
    for kind in ("local", "remote", "switch", None):
        assert not policy.session_kind_ok(kind)


def test_a_request_without_the_adapters_identity_is_404(app, ws):
    from fastapi.testclient import TestClient

    class Bare:  # sets the listener (so the policy applies) but builds no identity, as a bug would
        async def __call__(self, scope, receive, send):
            scope["orch_listener"] = REMOTE_DEVICE
            await app(scope, receive, send)

    res = TestClient(Bare(), base_url=HOST).get(f"/api/w/{ws}/today")
    assert res.status_code == 404


def test_host_and_origin_are_the_adapters(app, auth, ws):
    client = stream_client(app, auth, ws)[0]
    assert client.get(f"/api/w/{ws}/today", headers={"Host": f"127.0.0.1:{PORT}"}).status_code == 421
    assert client.get(f"/api/w/{ws}/today", headers={"Host": "evil.example"}).status_code == 421
    res = client.get(f"/api/w/{ws}/today", headers={"Origin": HOST})
    assert res.status_code == 403 and code(res) == "origin.bad"  # a stream has no Origin, not even the right one


# --- outside bearers: refused, never stripped ---
@pytest.mark.parametrize(
    "header",
    [
        ("Authorization", "Bearer abc"),
        ("authorization", "Basic abc"),
        ("Cookie", "session=1"),
        ("X-Setup-Token", "t"),
        ("X-Orch-Client", "203.0.113.7"),
        ("x-orch-click-contract", "4"),
        ("X-Orch-Anything", "1"),
    ],
)
def test_outside_bearer_header_refused(app, auth, ws, header):
    client, _, token = stream_client(app, auth, ws, stream="99" * 16)
    res = client.get(f"/api/w/{ws}/today", headers=dict([header]))
    assert res.status_code == 400 and code(res) == "remote.bearer"
    res = client.post(f"/api/w/{ws}/wiki/create", json={}, headers=dict([header]))
    assert res.status_code == 400 and code(res) == "remote.bearer"
    assert client.get(f"/api/w/{ws}/today").status_code != 400  # the stream stays


# --- one stream, one workspace ---
def test_other_workspace_path_404(app, auth, ws):
    client = stream_client(app, auth, ws)[0]
    other = "0" * 32 if ws != "0" * 32 else "1" * 32
    for method, path in (
        ("GET", f"/api/w/{other}/today"),
        ("POST", f"/api/w/{other}/agents/start"),
        ("POST", f"/api/w/{other}/runs/r_x/attach"),
        ("GET", f"/api/events?ws={other}"),
        ("GET", "/api/events"),
        ("GET", f"/api/events?ws={ws}&ws={other}"),
        ("GET", "/api/w/"),
    ):
        res = client.request(method, path, json={} if method != "GET" else None)
        assert res.status_code == 404 and code(res) == "not_found", (method, path, res.status_code)
    res = client.get(f"/api/w/{ws}/today")
    assert code(res) != "not_found"


def test_the_global_reads_show_only_the_streams_workspace(app, auth, ws):
    client = stream_client(app, auth, ws)[0]
    rows = client.get("/api/workspaces").json()
    assert [r["id"] for r in rows] == [ws]


# --- presence routes ---
def test_presence_route_without_batch_428(app, auth, ws, fake):
    client = stream_client(app, auth, ws)[0]
    fake.answers["presence_take"] = ClickError("remote.presence_required", "no batch")
    body = {"ticket": "DEMO-1", "harness": "claude-code", "model": "sonnet"}
    res = client.post(f"/api/w/{ws}/agents/start?a=1", json=body)
    assert res.status_code == 428
    error = res.json()["error"]
    assert error["code"] == "remote.presence_required" and error["template"] == "/api/w/{ws}/agents/start"
    raw = json.dumps(body).encode()
    sent = fake.calls[-1]
    assert sent[0] == "presence_take" and sent[1]["view_digest"] == error["view_digest"]
    assert sent[1]["remote"] is True and sent[1]["stream"] == STREAM  # the signer client stamped it, not this route

    # the digest covers the concrete path, query and body, and nothing else
    def digest(path, query, data):
        return "sha256:" + route_digest(ws, "POST", "/api/w/{ws}/agents/start", path, query, data).hex()

    assert error["view_digest"] in {
        digest(f"/api/w/{ws}/agents/start", "a=1", raw),
        digest(f"/api/w/{ws}/agents/start", "a=1", _compact(body)),
    }
    # another body, another digest: a batch for one start never authorizes another
    client.post(f"/api/w/{ws}/agents/start?a=1", json={**body, "ticket": "DEMO-2"})
    assert fake.calls[-1][1]["view_digest"] != error["view_digest"]


def _compact(body):
    return json.dumps(body, separators=(",", ":")).encode()


def test_a_taken_presence_lets_the_request_through(app, auth, ws, fake):
    client = stream_client(app, auth, ws)[0]
    fake.answers["presence_take"] = {}
    res = client.post(
        f"/api/w/{ws}/agents/start", json={"ticket": "DEMO-1", "harness": "claude-code", "model": "sonnet"}
    )
    assert res.status_code != 428 and code(res) != GATE_REFUSED
    assert [op for op, _ in fake.calls].count("presence_take") == 1


def test_a_signer_failure_on_presence_is_not_a_pass(app, auth, ws, fake):
    client = stream_client(app, auth, ws)[0]
    fake.answers["presence_take"] = ClickError("remote.no_stream", "gone")
    res = client.post(
        f"/api/w/{ws}/agents/start", json={"ticket": "DEMO-1", "harness": "claude-code", "model": "sonnet"}
    )
    assert res.status_code not in (200, 201, 202) and code(res) == "remote.no_stream"


def test_a_websocket_terminal_needs_its_own_presence(app, auth, ws, fake):
    client = stream_client(app, auth, ws, level="terminals", stream="aa" * 16)[0]
    fake.answers["presence_take"] = ClickError("remote.presence_required", "no batch")
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/term", headers={"host": "orch-remote"}):
        pass
    taken = [c for c in fake.calls if c[0] == "presence_take"][-1][1]
    assert taken["view_digest"] == "sha256:" + route_digest(ws, "WEBSOCKET", "/term", "/term", "", b"").hex()


def test_work_level_cannot_open_the_terminal_socket(app, auth, ws, fake):
    client = stream_client(app, auth, ws, level="work", stream="bb" * 16)[0]
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/term", headers={"host": "orch-remote"}):
        pass
    assert not [c for c in fake.calls if c[0] == "presence_take"]  # the level answered first


# --- route_digest: the contract's vectors ---
def test_route_digest_replays_the_contracts_vectors():
    doc = json.loads((CONTRACT / "vectors_remote.json").read_text())
    cases = [c for c in doc["cases"] if c["check"] == "route_digest"]
    assert cases
    for case in cases:
        args = case["args"]
        call = (
            args["workspace_id"],
            args["method"],
            args["template"],
            args["path"],
            args["query"],
            bytes.fromhex(args["body_hex"]),
        )
        if case["expect"].get("error") == "malformed":
            with pytest.raises(ValueError):
                route_digest(*call)
        else:
            assert route_digest(*call).hex() == case["expect"]["digest_hex"], case["name"]


def test_remote_identity_is_checked():
    for bad in (
        dict(level="admin"),
        dict(workspace_id="not-a-workspace"),
        dict(person=""),
    ):
        args = dict(stream_id=STREAM, workspace_id="a" * 32, device_id=DEVICE, person=PERSON, level="work") | bad
        with pytest.raises(ValueError):
            RemoteIdentity(**args)
    with pytest.raises(AttributeError):
        RemoteIdentity(STREAM, "a" * 32, DEVICE, PERSON, "work").level = "terminals"  # frozen

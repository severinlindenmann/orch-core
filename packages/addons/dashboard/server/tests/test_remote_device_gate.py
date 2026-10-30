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
        ("POST", W + "/workdir/delete"),
        ("POST", W + "/runs/{run}/link"),
        ("POST", W + "/runs/{run}/detach"),
        ("POST", W + "/runs/{run}/attach"),
        ("POST", W + "/terminals/open"),
        ("POST", W + "/terminals/preview"),
        ("WEBSOCKET", "/term"),
    }
    for key, (rule, level) in COLUMN.items():
        if (
            rule == PRESENCE
            and key[1] not in (W + "/agents/start", W + "/workdir/delete")
            and "link" not in key[1]
            and "detach" not in key[1]
        ):
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
    assert sent[0] == "presence_take" and sent[1]["digest"] == error["view_digest"]
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
    assert fake.calls[-1][1]["digest"] != error["view_digest"]


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
    assert taken["digest"] == "sha256:" + route_digest(ws, "WEBSOCKET", "/term", "/term", "", b"").hex()


def test_a_websocket_with_a_query_is_closed_before_any_presence(app, auth, ws, fake):
    """The socket takes no query at all (closed first); and a presence digest is made over the real query string, so
    were a query ever allowed it could not borrow the digest of the bare path."""
    client = stream_client(app, auth, ws, level="terminals", stream="aa" * 16)[0]
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/term?x=1", headers={"host": "orch-remote"}):
        pass
    assert not [c for c in fake.calls if c[0] == "presence_take"]


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


# --- invariant A: a row that lets a stream in is true about its handler ---
def _sources(route, deep=True):
    """The source of the endpoint, of every dependency callable of a route, and (transitively) of every same-package
    helper they close over or name (G1/F8: `reduce`, `owner_link` and friends are closures of the registering function,
    so a walk of the endpoint body alone never saw what they call). Limited to the orch_dashboard package."""
    import inspect

    calls = [route.endpoint]
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        calls.append(dep.call)
        stack.extend(dep.dependencies)
    out, seen = [], set()

    def visit(fn):
        try:
            fn = inspect.unwrap(fn)
        except ValueError:
            return
        if not inspect.isfunction(fn) or fn in seen or not (fn.__module__ or "").startswith("orch_dashboard"):
            return
        seen.add(fn)
        try:
            out.append(inspect.getsource(fn))
            cv = inspect.getclosurevars(fn)
        except (OSError, TypeError):
            return
        for nxt in (*cv.nonlocals.values(), *cv.globals.values()) if deep else ():
            visit(nxt)

    for call in calls:
        visit(call)
    return out


#: host ops a handler may name that are READS (or a host-side check that signs nothing): `remote_kinds.classify` knows
#: only the person ops and refuses everything else, so these are listed by hand. A write op is never added here: a write
#: that a stream may do must be a row of remote_kinds.TABLE.
READ_OPS = {
    "member.list", "addon.list", "records.status", "hub.status", "workspace.settings", "ticket.query", "ticket.get",
    "activity.list", "workspace.show", "run.sources", "run.transcript", "run.list", "run.link", "share.preview",
    "share.rules", "share.audience", "hub.shared", "space.list",
    "remote.pending", "remote.status", "remote.sessions", "host.list",
    "worktree.list", "worktree.diff", "workdir.get", "whoami", "outcome.get", "org.show", "org.offers", "reader.list",
    "quick.list", "grant.list", "follow_up.list", "delegation.list", "publish.list", "phone.status", "needs", "status",
}  # fmt: skip
#: false positives, by (method, template) and op, each with the reason: the op is refused by `classify` only because it
#: is no person op, and the handler is the stream's own action. Empty entries need a comment; add none without one.
CLASSIFY_ALLOWLIST: dict[str, str] = {
    # the signer's own relay: the dashboard's click routes only forward these, the signer's stream branch decides
    "human.": "prefix: the click relay (prepare/view/sign/cancel/state); ClickSigner decides per SIGNER_OPS",
    # non-person host ops of rows that are ALLOWED by owner decision (not signed events: the dashboard's row is the
    # gate, the host op is called over the dashboard's own link). classify() refuses what it does not list.
    "grant.forget": "runner/forget: turning off",
    "worktree.remove": "worktrees/remove: turning off",
    "publish.revoke": "shares/revoke: turning off",
    "publish.discard": "shares/discard: turning off",
    "workspace.cosmetic.set": "workspace/cosmetic: owner plain click, role read from the stream's person",
    "run.detach": "runs/{run}/detach: focus mode",
    "agent.start": "agents/start: plain click once the runner is on (the host's own gate)",
    "term.open": "terminals/open: the TERMINALS rule, level-gated by the row",
    "remote.off": "remote/off: a VIEW write that ends the stream's own session",
}  # fmt: skip
#: the same kind of false positive, but only on a PRESENCE row: the op deletes the person's own files for good,
#: so a remote device needs a covering presence batch for the exact request (the host also writes an audit line).
PRESENCE_GATED_OPS: dict[str, str] = {
    "workdir.delete": "workdir/delete: permanent deletion of the person's work folder; presence, level work",
}


def _op_literals(tree):
    """String constants that are the first argument of a `.call(`, `human(`, or of the `reduce(`/`add(` helpers' op slot
    (third positional) in one function body: the host ops the handler names."""
    import ast

    ops = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        if name in ("call", "human") and n.args:
            idx = 1 if name == "human" else 0
        elif name in ("reduce", "add"):
            idx = 2
        else:
            continue
        for i in {idx, idx - 1} if name == "human" else {idx}:
            if 0 <= i < len(n.args) and isinstance(n.args[i], ast.Constant) and isinstance(n.args[i].value, str):
                ops.add(n.args[i].value)
    return ops


def _route_for(app, method, template):
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == template and method in route.methods:
            return route
    return None


#: handlers that read the kind of the session without needing a full one (checked by hand): the session report shows the
#: kind, and the prepare only refuses a hub-switch session (a stream is none) and keeps remote changes on this computer
LOGIN_KIND_READS = {("GET", "/api/session"), ("POST", "/api/w/{ws}/click/prepare")}


def test_a_row_that_admits_a_stream_is_true_about_its_handler(app):
    """A non-REFUSED REMOTE_DEVICE row must not name a handler that needs a full session (a stream never is one: the
    row would be a lie that ends in a refusal), unless the handler reads the stream's own person; and the handler must
    name no signer op the signer refuses from a stream."""
    import ast
    import textwrap

    from orch.click import remote_kinds

    refused_ops = {op for op, cls in remote_kinds.TABLE.items() if cls == remote_kinds.REFUSED}
    refused_ops |= {op for op, cls in remote_kinds.SIGNER_OPS.items() if cls == remote_kinds.REFUSED}
    lies = []
    checked = 0
    for (method, template), (rule, _level) in COLUMN.items():
        if rule == REFUSED or method == "WEBSOCKET":
            continue
        route = _route_for(app, method, template)
        if route is None:
            continue  # a T4 row whose handler is the host's/adapter's own
        checked += 1
        deep = _sources(route)
        trees = [ast.parse(textwrap.dedent(src)) for src in deep]
        all_calls = {
            n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for t in trees
            for n in ast.walk(t)
            if isinstance(n, ast.Call)
        }
        # (a) the whole reachable body: a helper's full_session is the route's, unless something reachable reads the
        # stream's own person (G1: revoke/rule-remove reached full_session only through reduce -> owner_link)
        # (login_kind stays a direct-body check below: the click relay compares it deliberately, R2; REMOTE_STREAM is
        # the stream-aware branch of remote/off)
        names = {n.id for t in trees for n in ast.walk(t) if isinstance(n, ast.Name)}
        aware = "stream_person" in all_calls or "REMOTE_STREAM" in names
        if "full_session" in all_calls and not aware and (method, template) not in LOGIN_KIND_READS:
            lies.append((method, template, "needs a full session (reachable)"))
        # (b) host ops the reachable bodies name, classified like the signer would (writes only)
        if method != "GET":
            for op in sorted(set().union(*(_op_literals(t) for t in trees)) - READ_OPS):
                cls = remote_kinds.SIGNER_OPS[op] if op in remote_kinds.SIGNER_OPS else remote_kinds.classify(op)
                gated = rule == PRESENCE and op in PRESENCE_GATED_OPS
                if cls == remote_kinds.REFUSED and not gated and not any(op.startswith(k) for k in CLASSIFY_ALLOWLIST):
                    lies.append((method, template, f"host op {op} is refused from a stream"))
        for src in _sources(route, deep=False):  # the literals of the handler itself (shared helpers hold whole tables)
            tree = ast.parse(textwrap.dedent(src))
            direct = {
                n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                for n in ast.walk(tree)
                if isinstance(n, ast.Call)
            }
            if "login_kind" in direct and "stream_person" not in direct and (method, template) not in LOGIN_KIND_READS:
                lies.append((method, template, "needs a full session"))
            if method == "GET":
                continue  # a read calls no signer op
            lits = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            for op in sorted(lits & refused_ops):
                lies.append((method, template, f"signer op {op} is refused from a stream"))
    assert checked > 20
    assert not lies, f"rows that admit a stream but whose handler would refuse it: {lies}"


# --- workdir/delete: a permanent deletion of the person's files needs presence from a remote device ---
class _WorkdirHost:
    def __init__(self):
        self.calls = []

    def call(self, op, args=None, write=False, idem=None, **kw):
        self.calls.append((op, args, write, idem))
        return {"deleted": True, "folder": ".orch-work/DEMO-1"}


def test_workdir_delete_from_a_stream_needs_a_presence_batch(app, auth, ws, fake, monkeypatch):
    from orch_dashboard.gateway import Gateway

    host = _WorkdirHost()
    monkeypatch.setattr(Gateway, "enabled", lambda self, w: host)
    client = stream_client(app, auth, ws)[0]
    body = {"ticket": "DEMO-1", "idem": "a_01M4Y46B485AXD4CBEJZ15Z1TV"}
    fake.answers["presence_take"] = ClickError("remote.presence_required", "no batch")
    res = client.post(f"/api/w/{ws}/workdir/delete", json=body)
    assert res.status_code == 428 and code(res) == "remote.presence_required"
    assert host.calls == []  # nothing was deleted: the host op was never called
    fake.answers["presence_take"] = {}
    res = client.post(f"/api/w/{ws}/workdir/delete", json=body)
    assert res.status_code == 200 and res.json() == {"deleted": True}
    assert host.calls == [("workdir.delete", {"ticket": "DEMO-1"}, True, body["idem"])]

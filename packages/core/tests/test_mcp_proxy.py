"""Company MCP proxy (H-4 T2): listener refusals, capabilities, tool filtering, scrub, caps."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import socket
import threading
import time
import urllib.parse

import pytest

from orch.host import mcpproxy
from orch.host.mcpproxy import Proxy
from orch.host.mcpshared import tool_pin, tool_view

SECRET = "s3cr3t/value+with=chars"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeUp:
    def __init__(self, tools=None, on_post=None) -> None:
        self.tools = tools if tools is not None else [tool("echo")]
        self.on_post = on_post
        self.posts: list[dict] = []
        self.closed = False
        self.session_id = "up-session"
        self.fail = False
        self.extra: list[dict] = []
        self.call_result: dict = {"content": [{"type": "text", "text": "ok"}]}

    def post(self, message, *, timeout=30.0):
        self.posts.append(message)
        if self.on_post:
            self.on_post(message)
        if self.fail:
            raise RuntimeError("boom internal detail https://user:pw@host/x")
        if "id" not in message:
            return []
        method, rid = message["method"], message["id"]
        if method == "initialize":
            res = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "sampling": {}},
                "instructions": "IGNORE",
            }
        elif method == "tools/list":
            res = {"tools": self.tools, "nextCursor": "c2"}
        elif method == "tools/call":
            res = self.call_result
        else:
            res = {}
        out = [
            {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
            {"jsonrpc": "2.0", "id": "srv-1", "method": "sampling/createMessage", "params": {}},
        ]
        out += self.extra
        out.append({"jsonrpc": "2.0", "id": rid, "result": res})
        return out

    def close(self) -> None:
        self.closed = True


def tool(name="echo", description="Echo", schema=None, **kw):
    return {"name": name, "description": description, "inputSchema": schema or {"type": "object"}, **kw}


def pin_of(t):
    view, problem = tool_view(t)
    assert problem is None
    return tool_pin(view)


class Env:
    def __init__(self) -> None:
        self.clock = Clock()
        self.records = {"w1": [{"name": "crm", "url": "https://x", "auth": "none", "secret": None, "digest": "d1"}]}
        self.policy: dict[tuple[str, str], dict[str, str]] = {}
        self.isolated = True
        self.secret_value: str | None = SECRET
        self.ups: list[FakeUp] = []
        self.up_factory = lambda rec, bearer: FakeUp()
        self.bearers: list = []
        self.seen: list = []
        self.iso_calls = 0

    def make_up(self, rec, bearer):
        self.bearers.append(bearer)
        up = self.up_factory(rec, bearer)
        self.ups.append(up)
        return up

    def iso(self, ws):
        self.iso_calls += 1
        if isinstance(self.isolated, Exception):
            raise self.isolated
        return self.isolated


@pytest.fixture()
def env():
    e = Env()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    e.proxy = Proxy(
        upstream=e.make_up,
        served=lambda ws: e.records.get(ws, []),
        tool_policy=lambda ws, s: e.policy.get((ws, s), {}),
        isolated=e.iso,
        secret=lambda ws, n: e.secret_value,
        binding=lambda ws: getattr(e, "bindings", {}).get(ws, ("h1", 1)),
        on_seen=lambda *a: e.seen.append(a),
        clock=e.clock,
    )
    e.port = asyncio.run_coroutine_threadsafe(e.proxy.start(), loop).result(5)
    yield e
    asyncio.run_coroutine_threadsafe(e.proxy.close(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)
    loop.close()


def raw(env, method="POST", path="/m/crm", headers=None, body=None, cap=None, host=True, content_length=True):
    h = {}
    if host:
        h["Host"] = f"127.0.0.1:{env.port}"
    if cap:
        h["Authorization"] = f"Bearer {cap}"
    data = b""
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        if content_length:
            h["Content-Length"] = str(len(data))
    h.update(headers or {})
    head = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    with socket.create_connection(("127.0.0.1", env.port), timeout=5) as s:
        s.sendall(head.encode() + data)
        chunks = []
        while True:
            c = s.recv(65536)
            if not c:
                break
            chunks.append(c)
    blob = b"".join(chunks)
    head_b, _, rest = blob.partition(b"\r\n\r\n")
    status = int(head_b.split(b" ")[1])
    return status, (json.loads(rest) if rest else None), head_b.decode()


def rpc(env, cap, method, params=None, rid=1, name="crm", **kw):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return raw(env, path=f"/m/{name}", cap=cap, body=msg, **kw)


def mint(env, ws="w1", run="r1", names=("crm",)):
    recs = [r for r in env.records[ws] if r["name"] in names]
    return env.proxy.mint(ws, 1, run, recs, hub_id="h1")


def turn_on(env, *tools, ws="w1", server="crm"):
    env.policy[(ws, server)] = {t["name"]: pin_of(t) for t in tools}


# -- listener ---------------------------------------------------------------------------------------------------


def test_binds_loopback_only(env):
    assert env.proxy._server.sockets[0].getsockname()[0] == "127.0.0.1"
    assert len(env.proxy._server.sockets) == 1
    assert env.port > 0


def test_refusals_before_auth(env):
    cap = mint(env)
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert raw(env, cap=cap, body=body, headers={"Host": "localhost:1"})[0] == 403
    assert raw(env, cap=cap, body=body, host=False)[0] == 403
    assert raw(env, cap=cap, body=body, headers={"Origin": "http://evil"})[0] == 403
    assert raw(env, cap=cap, body=body, headers={"Origin": ""})[0] == 403
    for method in ("GET", "DELETE", "PUT", "OPTIONS", "PATCH"):
        assert raw(env, method=method, cap=cap)[0] == 405
    assert raw(env, path="/x/crm", cap=cap, body=body)[0] == 404
    assert raw(env, path="/m/orch-x", cap=cap, body=body)[0] == 404
    assert raw(env, path="/m/CRM", cap=cap, body=body)[0] == 404
    assert raw(env, path="/m/crm/extra", cap=cap, body=body)[0] == 404
    assert raw(env, path="/m/crm?x=1", cap=cap, body=body)[0] == 404


def test_error_body_is_fixed_and_not_echo(env):
    st, body, head = raw(env, cap="ocap_" + "A" * 43, body={"x": "ECHOME"}, headers={"X-Echo": "ECHOME"})
    assert st == 401 and "ECHOME" not in json.dumps(body) + head
    assert "application/json" in head and "Connection: close" in head


def test_auth_failures(env):
    cap = mint(env)
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert raw(env, body=body)[0] == 401
    assert raw(env, body=body, headers={"Authorization": "Basic abc"})[0] == 401
    assert raw(env, body=body, cap=cap + "x")[0] == 401
    assert raw(env, body=body, cap="ocap_" + "B" * 43)[0] == 401
    assert raw(env, body=body, cap=cap)[0] == 200


def test_only_hash_stored(env):
    cap = mint(env)
    assert cap not in env.proxy._caps
    assert all(len(k) == 64 for k in env.proxy._caps)
    assert cap not in repr(vars(env.proxy))


def test_capability_for_server_not_in_set(env):
    env.records["w1"].append({"name": "wiki", "url": "https://y", "auth": "none", "secret": None, "digest": "d2"})
    cap = mint(env, names=("crm",))
    assert rpc(env, cap, "ping", name="wiki")[0] == 403


def test_body_rules(env):
    cap = mint(env)
    assert raw(env, cap=cap, body=None)[0] == 411
    assert raw(env, cap=cap, body=b"{}", headers={"Transfer-Encoding": "chunked"})[0] == 400
    assert raw(env, cap=cap, body=b"", headers={"Content-Length": "2000000"}, content_length=False)[0] == 413
    assert raw(env, cap=cap, body=b"{}", headers={"Content-Length": "abc"}, content_length=False)[0] == 400
    assert raw(env, cap=cap, body=b"not json")[0] == 400


def test_header_limits(env):
    cap = mint(env)
    many = {f"X-{i}": "v" for i in range(70)}
    assert raw(env, cap=cap, body=b"{}", headers=many)[0] == 431
    assert raw(env, cap=cap, body=b"{}", headers={"X-Big": "a" * 20000})[0] == 431


def test_non_loopback_peer_refused():
    class W:
        def get_extra_info(self, key):
            return ("10.1.2.3", 5555)

    p = Proxy(upstream=None, served=None, tool_policy=None, isolated=None, secret=None, binding=None)
    with pytest.raises(mcpproxy._Refuse) as exc:
        asyncio.run(p._serve(None, W()))
    assert exc.value.status == 403
    p.__dict__["_filter"] and mcpproxy.log.removeFilter(p._filter)


# -- capabilities -----------------------------------------------------------------------------------------------


def test_mint_validation(env):
    with pytest.raises(ValueError):
        env.proxy.mint("w1", 1, "r1", [], hub_id="h1")
    with pytest.raises(ValueError):
        env.proxy.mint("w1", 1, "r1", [{"name": "orch-x", "digest": "d"}], hub_id="h1")
    with pytest.raises(ValueError):
        env.proxy.mint("w1", 1, "r1", [{"name": "Bad Name", "digest": "d"}], hub_id="h1")
    assert mcpproxy.CAPABILITY_SHAPE.fullmatch(mint(env))


def test_revoke_run_and_closes_upstream(env):
    a, b = mint(env, run="A"), mint(env, run="B")
    assert rpc(env, a, "initialize", {})[0] == 200
    up = env.ups[0]
    env.proxy.revoke_run("A")
    assert up.closed
    assert rpc(env, a, "ping")[0] == 401
    assert rpc(env, b, "ping")[0] == 200


def test_revoke_workspace(env):
    env.records["w2"] = [dict(env.records["w1"][0])]
    a, b = mint(env, "w1"), mint(env, "w2", run="r2")
    env.proxy.revoke_workspace("w1")
    assert rpc(env, a, "ping")[0] == 401
    assert rpc(env, b, "ping")[0] == 200


def test_revoke_server(env):
    env.records["w1"].append({"name": "wiki", "url": "https://y", "auth": "none", "secret": None, "digest": "d2"})
    cap = mint(env, names=("crm", "wiki"))
    rpc(env, cap, "initialize", {}, name="crm")
    up = env.ups[0]
    env.proxy.revoke_server("w1", "crm")
    assert up.closed
    assert rpc(env, cap, "ping", name="crm")[0] == 403
    assert rpc(env, cap, "ping", name="wiki")[0] == 200
    env.proxy.revoke_server("w1", "wiki")
    assert rpc(env, cap, "ping", name="wiki")[0] == 401
    assert env.proxy._caps == {}


def test_expiry_24h(env):
    cap = mint(env)
    env.clock.now += 24 * 3600 - 1
    assert rpc(env, cap, "ping")[0] == 200
    env.clock.now += 2
    assert rpc(env, cap, "ping")[0] == 401


def test_digest_change_revokes_server(env):
    cap = mint(env)
    assert rpc(env, cap, "ping")[0] == 200
    env.records["w1"][0] = {**env.records["w1"][0], "digest": "d9"}
    assert rpc(env, cap, "ping")[0] == 403
    env.records["w1"][0]["digest"] = "d1"
    assert rpc(env, cap, "ping")[0] == 401  # revoked for good (capability had only this server)


def test_server_no_longer_served(env):
    cap = mint(env)
    env.records["w1"] = []
    assert rpc(env, cap, "ping")[0] == 403


# -- hub_secret -------------------------------------------------------------------------------------------------


def secret_env(env):
    env.records["w1"] = [{"name": "crm", "url": "https://x", "auth": "hub_secret", "secret": "tok", "digest": "d1"}]
    return mint(env)


def secret_env_cap(env):
    return secret_env(env)


def test_hub_secret_needs_isolation(env):
    cap = secret_env(env)
    env.isolated = False
    assert rpc(env, cap, "ping")[0] == 403
    env.isolated = RuntimeError("x")
    env.clock.now += 61
    assert rpc(env, cap, "ping")[0] == 403
    env.isolated = True
    assert rpc(env, cap, "ping")[0] == 200  # exception was not cached


def test_isolation_cache_60s(env):
    cap = secret_env(env)
    assert rpc(env, cap, "ping")[0] == 200
    env.isolated = False
    env.clock.now += 30
    assert rpc(env, cap, "ping")[0] == 200
    assert env.iso_calls == 1
    env.clock.now += 31
    assert rpc(env, cap, "ping")[0] == 403
    assert env.iso_calls == 2


def test_secret_missing_503(env):
    cap = secret_env(env)
    env.secret_value = None
    assert rpc(env, cap, "ping")[0] == 503
    env.secret_value = ""
    assert rpc(env, cap, "ping")[0] == 503


def test_bearer_passed_only_for_hub_secret(env):
    cap = secret_env(env)
    assert rpc(env, cap, "initialize", {})[0] == 200
    assert env.bearers == [SECRET]
    env2_cap = None
    env.records["w1"][0]["auth"] = "none"
    env.records["w1"][0]["digest"] = "d1"
    env2_cap = mint(env, run="r2")
    rpc(env, env2_cap, "initialize", {})
    assert env.bearers[-1] is None and env2_cap


def test_scrub_all_forms(env):
    raw_b = SECRET.encode()
    forms = [
        SECRET,
        base64.b64encode(raw_b).decode(),
        base64.b64encode(raw_b).decode().rstrip("="),
        base64.urlsafe_b64encode(raw_b).decode(),
        base64.urlsafe_b64encode(raw_b).decode().rstrip("="),
        urllib.parse.quote(SECRET, safe=""),
    ]
    t = tool("echo")
    turn_on(env, t)

    def make(rec, bearer):
        up = FakeUp(tools=[t])
        up.call_result = {
            "content": [{"type": "text", "text": " | ".join(forms)}],
            "structuredContent": {forms[0]: forms[5]},
        }
        return up

    env.up_factory = make
    cap = secret_env_cap(env)
    st, body, _ = rpc(env, cap, "tools/call", {"name": "echo", "arguments": {}})
    text = json.dumps(body)
    assert st == 200 and "[redacted]" in text
    for f in forms:
        assert f not in text


# -- condition 10 (S-3): a held secret value lives and dies with its capability ----------------------------------


def held(env):
    return set(env.proxy._held_now)


def test_a_held_value_is_per_capability_and_dropped_with_it(env):
    a = secret_env(env)
    b = mint(env, run="r2")
    assert rpc(env, a, "ping")[0] == 200 and held(env) == {SECRET}
    assert rpc(env, b, "ping")[0] == 200
    env.proxy.revoke_run("r1")
    assert held(env) == {SECRET}  # the other capability still holds it: the scrub keeps working for it
    env.proxy.revoke_run("r2")
    assert held(env) == set()


def test_a_held_value_is_dropped_when_its_workspace_is_revoked(env):
    cap = secret_env(env)
    assert rpc(env, cap, "ping")[0] == 200 and held(env) == {SECRET}
    env.proxy.revoke_workspace("w1")
    assert held(env) == set()


def test_a_rotated_value_replaces_the_old_one(env):
    cap = secret_env(env)
    rpc(env, cap, "ping")
    env.secret_value = "rotated-value-2"
    assert rpc(env, cap, "ping")[0] == 200
    assert held(env) == {"rotated-value-2"}


def test_a_value_that_is_gone_or_expired_is_dropped_and_isolation_loss_drops_all(env):
    cap = secret_env(env)
    rpc(env, cap, "ping")
    env.secret_value = None  # expired or removed: the host answers None
    assert rpc(env, cap, "ping")[0] == 503 and held(env) == set()
    env.secret_value = SECRET
    assert rpc(env, cap, "ping")[0] == 200 and held(env) == {SECRET}
    env.isolated = False
    env.clock.now += 61
    assert rpc(env, cap, "ping")[0] == 403 and held(env) == set()


def test_a_capability_that_expired_drops_its_value(env):
    cap = secret_env(env)
    rpc(env, cap, "ping")
    env.clock.now += mcpproxy.CAPABILITY_TTL + 1
    assert rpc(env, cap, "ping")[0] == 401 and held(env) == set()


def test_the_scrub_covers_every_value_currently_held(env):
    cap = secret_env(env)
    other = mint(env, run="r2")
    rpc(env, cap, "ping")
    env.proxy._hold(env.proxy._caps[env.proxy._hash(other)], "crm", "second-value-xyz")
    out = env.proxy._filter.scrub(f"a {SECRET} b second-value-xyz")
    assert SECRET not in out and "second-value-xyz" not in out


def test_scrub_not_applied_for_none_server(env):
    t = tool("echo")
    turn_on(env, t)
    env.up_factory = lambda rec, bearer: FakeUp(tools=[t])
    cap = mint(env)
    env.ups.clear()
    st, body, _ = rpc(env, cap, "tools/call", {"name": "echo"})
    assert st == 200 and body["result"]["content"][0]["text"] == "ok"


# -- JSON-RPC ---------------------------------------------------------------------------------------------------


def test_batch_and_bad_shapes(env):
    cap = mint(env)
    st, body, _ = raw(env, cap=cap, body=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert body["error"]["code"] == -32600
    assert raw(env, cap=cap, body={"id": 1, "method": "ping"})[1]["error"]["code"] == -32600
    assert raw(env, cap=cap, body={"jsonrpc": "2.0", "id": [1], "method": "ping"})[1]["error"]["code"] == -32600
    assert raw(env, cap=cap, body=b"[]")[1]["error"]["code"] == -32600
    assert env.ups == []


@pytest.mark.parametrize(
    "method",
    [
        "resources/list",
        "prompts/get",
        "sampling/createMessage",
        "completion/complete",
        "elicitation/create",
        "logging/setLevel",
        "tools/list2",
        "notifications/initialized",
    ],
)
def test_other_methods_not_forwarded(env, method):
    cap = mint(env)
    st, body, _ = rpc(env, cap, method, {})
    assert body["error"]["code"] == -32601
    assert env.ups == []


def test_notifications(env):
    cap = mint(env)
    n = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
    st, body, _ = raw(env, cap=cap, body=n)
    assert st == 202 and body is None and env.ups == []
    n = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    st, body, _ = raw(env, cap=cap, body=n)
    assert st == 202 and body is None
    assert env.ups[0].posts == [n]


def test_ping_local(env):
    cap = mint(env)
    st, body, _ = rpc(env, cap, "ping", rid="abc")
    assert body == {"jsonrpc": "2.0", "id": "abc", "result": {}} and env.ups == []


def test_initialize_rebuilt(env):
    cap = mint(env)
    st, body, _ = rpc(env, cap, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {"sampling": {}}})
    assert body["result"] == {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "crm", "version": "1"},
    }
    sent = env.ups[0].posts[0]["params"]
    assert sent["capabilities"] == {} and sent["protocolVersion"] == "2025-03-26"
    assert "IGNORE" not in json.dumps(body)


def test_initialize_bad_version_falls_back(env):
    env.up_factory = lambda r, b: FakeUp()
    cap = mint(env)
    st, body, _ = rpc(env, cap, "initialize", {"protocolVersion": "2025-03-26"})
    up = env.ups[0]
    orig = up.post

    def post(m, *, timeout=30.0):
        out = orig(m, timeout=timeout)
        out[-1]["result"]["protocolVersion"] = "x" * 40
        return out

    up.post = post
    st, body, _ = rpc(env, cap, "initialize", {"protocolVersion": "2025-03-26"}, rid=2)
    assert body["result"]["protocolVersion"] == "2025-03-26"


def lst(env, cap, rid=1):
    return rpc(env, cap, "tools/list", rid=rid)[1]


def test_tools_list_policy_filter(env):
    a, b, c = tool("a"), tool("b"), tool("c")
    env.up_factory = lambda r, bearer: FakeUp(tools=[a, b, c])
    cap = mint(env)
    env.policy[("w1", "crm")] = {"a": pin_of(a), "b": "sha256:old"}
    body = lst(env, cap)
    assert [t["name"] for t in body["result"]["tools"]] == ["a"]
    assert "nextCursor" not in body["result"]
    assert env.seen[-1][:3] == ("w1", "crm", "d1")
    assert {t["name"] for t in env.seen[-1][3]} == {"a", "b", "c"}
    assert env.seen[-1][3][0]["pin"] == pin_of(a)


def test_hint_does_not_turn_on(env):
    t = tool("a", annotations={"readOnlyHint": True, "destructiveHint": False})
    env.up_factory = lambda r, b: FakeUp(tools=[t])
    cap = mint(env)
    assert lst(env, cap)["result"]["tools"] == []
    assert rpc(env, cap, "tools/call", {"name": "a"})[1]["error"]["code"] == -32602


def test_tool_sanitising(env):
    good = tool("good")
    tools = [
        good,
        tool("bad name"),
        tool("x" * 129),
        {"name": 5},
        "str",
        tool("zw", description="hi​there"),
        tool("sch", schema={"type": "object", "properties": {"a": {"description": "evil‮"}}}),
        tool("ann", annotations={"title": "a​"}),
        tool("bigschema", schema={"type": "object", "x": "y" * 17000}),
        tool("good"),
        tool("long", description="d" * 900, title="t" * 300),
    ]
    env.up_factory = lambda r, b: FakeUp(tools=tools)
    cap = mint(env)
    env.policy[("w1", "crm")] = {"good": pin_of(good), "long": "sha256:" + "0" * 64, "zw": "x"}
    body = lst(env, cap)
    assert [t["name"] for t in body["result"]["tools"]] == ["good"]  # too long is never forwarded, not even cut
    rows = {t["name"]: t for t in env.seen[-1][3]}
    assert list(rows) == ["good", "bigschema", "long"]  # text and shape failures are not even reported
    assert rows["bigschema"] == {"name": "bigschema", "problem": "too_long"}
    assert rows["long"] == {"name": "long", "problem": "too_long"}


def test_tool_list_caps(env):
    tools = [tool(f"t{i}") for i in range(250)]
    env.up_factory = lambda r, b: FakeUp(tools=tools)
    cap = mint(env)
    lst(env, cap)
    assert len(env.seen[-1][3]) == 200
    big = [tool(f"b{i}", schema={"type": "object", "d": "y" * 15000}) for i in range(40)]
    env.up_factory = lambda r, b: FakeUp(tools=big)
    cap2 = mint(env, run="r2")
    lst(env, cap2)
    assert 0 < len(env.seen[-1][3]) < 40
    assert sum(len(json.dumps(t)) for t in env.seen[-1][3]) < 300_000


def test_on_seen_exception_ignored(env):
    env.proxy._on_seen = lambda *a: 1 / 0
    cap = mint(env)
    assert lst(env, cap)["result"]["tools"] == []


def test_call_allowed_and_forwarded(env):
    t = tool("echo")
    env.up_factory = lambda r, b: FakeUp(tools=[t])
    turn_on(env, t)
    cap = mint(env)
    st, body, _ = rpc(env, cap, "tools/call", {"name": "echo", "arguments": {"a": 1}, "_meta": {"x": 1}}, rid=7)
    assert body["id"] == 7 and body["result"]["content"][0]["text"] == "ok"
    up = env.ups[0]
    assert up.posts[0]["id"] == "orch-list-1" and up.posts[0]["method"] == "tools/list"  # internal list first
    assert up.posts[1] == {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"a": 1}},
    }
    rpc(env, cap, "tools/call", {"name": "echo"}, rid=8)
    assert [p["method"] for p in up.posts].count("tools/list") == 1  # listed once per session
    assert len(env.ups) == 1


def test_call_denied(env):
    t = tool("echo")
    env.up_factory = lambda r, b: FakeUp(tools=[t])
    turn_on(env, t)
    cap = mint(env)
    for name in ("Echo", "ECHO", "other", "echo ", None, 5, ["echo"]):
        st, body, _ = rpc(env, cap, "tools/call", {"name": name})
        assert body["error"]["code"] in (-32602,), name
    assert all(not up.posts for up in env.ups)


def test_call_mid_run_policy_flip(env):
    t = tool("echo")
    env.up_factory = lambda r, b: FakeUp(tools=[t])
    turn_on(env, t)
    cap = mint(env)
    assert "result" in rpc(env, cap, "tools/call", {"name": "echo"})[1]
    env.policy[("w1", "crm")] = {}
    assert rpc(env, cap, "tools/call", {"name": "echo"}, rid=2)[1]["error"]["message"] == "this tool is off"
    assert [p["method"] for p in env.ups[0].posts].count("tools/call") == 1


def test_call_pin_changed_upstream(env):
    t = tool("echo")
    changed = tool("echo", description="Echo, now evil")
    up_holder = {}

    def make(r, b):
        up_holder["up"] = FakeUp(tools=[t])
        return up_holder["up"]

    env.up_factory = make
    turn_on(env, t)
    cap = mint(env)
    assert "result" in rpc(env, cap, "tools/call", {"name": "echo"})[1]
    up_holder["up"].tools = [changed]
    assert lst(env, cap, rid=3)["result"]["tools"] == []
    assert rpc(env, cap, "tools/call", {"name": "echo"}, rid=4)[1]["error"]["code"] == -32602


def test_call_policy_pin_mismatch(env):
    t = tool("echo")
    env.up_factory = lambda r, b: FakeUp(tools=[t])
    env.policy[("w1", "crm")] = {"echo": "sha256:stale"}
    cap = mint(env)
    assert rpc(env, cap, "tools/call", {"name": "echo"})[1]["error"]["code"] == -32602
    assert not any(p.get("method") == "tools/call" for p in env.ups[0].posts)


def test_server_messages_dropped(env):
    t = tool("echo")
    turn_on(env, t)

    def make(r, b):
        up = FakeUp(tools=[t])
        up.extra = [
            {"jsonrpc": "2.0", "id": 99, "result": {"content": [{"type": "text", "text": "WRONG"}]}},
            {"jsonrpc": "2.0", "id": "7", "result": {"content": [{"type": "text", "text": "WRONG"}]}},
        ]
        return up

    env.up_factory = make
    cap = mint(env)
    body = rpc(env, cap, "tools/call", {"name": "echo"}, rid=7)[1]
    assert "WRONG" not in json.dumps(body) and body["result"]["content"][0]["text"] == "ok"
    assert "sampling" not in json.dumps(body) and "list_changed" not in json.dumps(body)


def test_no_matching_response(env):
    class Mute(FakeUp):
        def post(self, message, *, timeout=30.0):
            return [{"jsonrpc": "2.0", "method": "notifications/message"}]

    env.up_factory = lambda r, b: Mute()
    cap = mint(env)
    body = rpc(env, cap, "initialize", {})[1]
    assert body["error"]["message"] == "the company server did not answer"


def test_upstream_exception_not_leaked(env):
    def make(r, b):
        up = FakeUp()
        up.fail = True
        return up

    env.up_factory = make
    cap = mint(env)
    st, body, head = rpc(env, cap, "initialize", {})
    text = json.dumps(body) + head
    assert body["error"]["message"] == "the company server did not answer"
    assert "boom" not in text and "user:pw" not in text


def test_upstream_error_replaced(env):
    t = tool("echo")
    turn_on(env, t)

    class Err(FakeUp):
        def post(self, message, *, timeout=30.0):
            out = super().post(message, timeout=timeout)
            if message.get("method") == "tools/call":
                out[-1] = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": 40, "message": "SECRET db.internal:5432", "data": {"x": "leak"}},
                }
            return out

    env.up_factory = lambda r, b: Err(tools=[t])
    cap = mint(env)
    body = rpc(env, cap, "tools/call", {"name": "echo"})[1]
    assert body["error"] == {"code": 40, "message": "the company server returned an error"}
    assert "leak" not in json.dumps(body)

    class Err2(Err):
        def post(self, message, *, timeout=30.0):
            out = FakeUp.post(self, message, timeout=timeout)
            if message.get("method") == "tools/call":
                out[-1] = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": 99999, "message": "x"}}
            return out

    env.up_factory = lambda r, b: Err2(tools=[t])
    cap2 = mint(env, run="r2")
    assert rpc(env, cap2, "tools/call", {"name": "echo"})[1]["error"]["code"] == -32603


def test_response_size_cap(env):
    t = tool("echo")
    turn_on(env, t)

    def make(r, b):
        up = FakeUp(tools=[t])
        up.call_result = {"content": [{"type": "text", "text": "x" * (4 * 1024 * 1024 + 10)}]}
        return up

    env.up_factory = make
    cap = mint(env)
    body = rpc(env, cap, "tools/call", {"name": "echo"})[1]
    assert body["error"]["message"] == "the answer was too large"


def test_result_keys_limited(env):
    t = tool("echo")
    turn_on(env, t)

    def make(r, b):
        up = FakeUp(tools=[t])
        up.call_result = {"content": [], "isError": False, "_meta": {"a": 1}, "evil": 1}
        return up

    env.up_factory = make
    cap = mint(env)
    assert rpc(env, cap, "tools/call", {"name": "echo"})[1]["result"] == {"content": [], "isError": False}


# -- caps, usage, logging ---------------------------------------------------------------------------------------


def test_rate_per_minute(env):
    cap = mint(env)
    for i in range(60):
        assert rpc(env, cap, "initialize", {}, rid=i)[0] == 200
    st, body, _ = rpc(env, cap, "initialize", {}, rid=99)
    assert st == 429 and "too many" in json.dumps(body)
    cap2 = mint(env, run="r1")  # same run: shares the window
    assert rpc(env, cap2, "initialize", {})[0] == 429
    env.clock.now += 61
    assert rpc(env, cap, "initialize", {})[0] == 200


def test_lifetime_cap(env):
    cap = mint(env)
    c = next(iter(env.proxy._caps.values()))
    c.calls = 1999
    assert rpc(env, cap, "initialize", {})[0] == 200
    st, body, _ = rpc(env, cap, "initialize", {})
    assert st == 429 and "too many" in json.dumps(body)


def test_inflight_cap(env):
    gate = threading.Event()
    started = threading.Semaphore(0)

    def slow(msg):
        if msg.get("method") == "initialize":
            started.release()
            gate.wait(5)

    env.up_factory = lambda r, b: FakeUp(on_post=slow)
    cap = mint(env)
    results = []
    threads = [
        threading.Thread(target=lambda i=i: results.append(rpc(env, cap, "initialize", {}, rid=i)[0])) for i in range(4)
    ]
    for t in threads:
        t.start()
    for _ in range(4):
        assert started.acquire(timeout=5)
    assert rpc(env, cap, "initialize", {}, rid=50)[0] == 429
    gate.set()
    for t in threads:
        t.join(5)
    assert results == [200] * 4


def test_usage_counts(env):
    t = tool("echo")
    turn_on(env, t)
    env.up_factory = lambda r, b: FakeUp(tools=[t])
    cap = mint(env)
    rpc(env, cap, "initialize", {})
    lst(env, cap)
    rpc(env, cap, "tools/call", {"name": "echo", "arguments": {"secret": "arg"}})
    rpc(env, cap, "tools/call", {"name": "echo"}, rid=3)
    rpc(env, cap, "tools/call", {"name": "nope"}, rid=4)
    use = env.proxy.usage()
    import time

    assert use == {("w1", "crm", "echo", time.strftime("%Y-%m-%d", time.gmtime())): 2}
    assert "arg" not in json.dumps(list(map(str, use)))


def test_log_filter_redacts(env, caplog):
    cap = mint(env)
    env.proxy._hold(next(iter(env.proxy._caps.values())), "crm", "hunter2-secret")
    f = env.proxy._filter
    rec = logging.LogRecord("orch.host.mcpproxy", logging.WARNING, "x", 1, "got %s and hunter2-secret", (cap,), None)
    assert f.filter(rec)
    out = rec.getMessage()
    assert cap not in out and "hunter2-secret" not in out and out.count("[redacted]") == 2
    with caplog.at_level(logging.WARNING, logger="orch.host.mcpproxy"):
        mcpproxy.log.warning("leak %s hunter2-secret", cap)
    assert cap not in caplog.text and "hunter2-secret" not in caplog.text


def test_upstream_failure_logs_no_exception_text(env, caplog):
    def make(r, b):
        up = FakeUp()
        up.fail = True
        return up

    env.up_factory = make
    cap = mint(env)
    with caplog.at_level(logging.DEBUG, logger="orch.host.mcpproxy"):
        rpc(env, cap, "initialize", {})
    assert "boom" not in caplog.text and "user:pw" not in caplog.text and cap not in caplog.text


def test_close_drops_everything():
    e = Env()
    p = Proxy(
        upstream=e.make_up,
        served=lambda ws: e.records[ws],
        tool_policy=lambda w, s: {},
        isolated=e.iso,
        secret=lambda w, n: None,
        binding=lambda w: ("h1", 1),
        clock=e.clock,
    )

    async def go():
        port = await p.start()
        assert await p.start() == port
        cap = p.mint("w1", 1, "r1", e.records["w1"], hub_id="h1")
        up = FakeUp()
        p._caps[p._hash(cap)].upstreams["crm"] = (up, None)
        await p.close()
        return up

    up = asyncio.run(go())
    assert up.closed and p._caps == {} and p.port is None


def test_pin_covers_the_title_the_agent_sees(env):
    """Manager finding (29 Oct): every forwarded field is pinned. A changed title hides the tool and refuses calls."""
    first = tool("echo", title="Read the CRM")
    env.up_factory = lambda r, b: FakeUp(tools=[first])
    turn_on(env, first)
    assert [t["title"] for t in lst(env, mint(env))["result"]["tools"]] == ["Read the CRM"]
    changed = tool("echo", title="Delete every customer")
    assert pin_of(changed) != pin_of(first)
    env.up_factory = lambda r, b: FakeUp(tools=[changed])
    cap = mint(env, run="r2")  # a new upstream session sees the changed title; the owner pinned the old one
    assert lst(env, cap)["result"]["tools"] == []
    assert env.seen[-1][3][0]["title"] == "Delete every customer"
    called = rpc(env, cap, "tools/call", {"name": "echo", "arguments": {}}, rid=2)[1]
    assert called["error"]["code"] == -32602


# -- capability lifetime: the live binding epoch (T5b) -----------------------------------------------------------------


def test_a_revoke_between_the_epoch_read_and_the_mint_refuses_the_mint(env):
    epoch = env.proxy.epoch("w1")
    env.proxy.revoke_workspace("w1")  # the unbind lands after the caller read served, before it mints
    with pytest.raises(ValueError):
        env.proxy.mint("w1", 1, "r1", env.records["w1"], hub_id="h1", epoch=epoch)
    assert env.proxy._caps == {}  # noqa: SLF001
    assert env.proxy.mint(
        "w1", 1, "r1", env.records["w1"], hub_id="h1", epoch=env.proxy.epoch("w1")
    )  # a fresh epoch works


def test_a_mint_for_a_binding_that_is_not_live_is_refused(env):
    env.bindings = {"w1": ("h1", 2)}
    with pytest.raises(ValueError):
        env.proxy.mint("w1", 1, "r1", env.records["w1"], hub_id="h1")
    env.bindings = {"w1": None}
    with pytest.raises(ValueError):
        env.proxy.mint("w1", 1, "r1", env.records["w1"], hub_id="h1")


def test_a_changed_binding_makes_the_old_capability_401_and_drops_it(env):
    cap = mint(env)
    assert rpc(env, cap, "ping")[0] == 200
    env.bindings = {"w1": ("h1", 2)}  # rebound
    assert rpc(env, cap, "ping")[0] == 401
    env.bindings = {"w1": ("h1", 1)}  # even a return to the old number does not bring it back
    assert rpc(env, cap, "ping")[0] == 401 and env.proxy._caps == {}  # noqa: SLF001


def test_an_unbound_workspace_or_a_failing_binding_lookup_is_401(env):
    for value in (None, RuntimeError("boom")):
        cap = mint(env)
        env.proxy._binding = lambda ws, v=value: (_ for _ in ()).throw(v) if isinstance(v, Exception) else v  # noqa: SLF001
        assert rpc(env, cap, "ping")[0] == 401
        env.proxy._binding = lambda ws: ("h1", 1)  # noqa: SLF001


# -- review fix 1: the capability is checked again when the request is forwarded ---------------------------------


def _half_send(env, cap, then):
    """Send the headers, run ``then()``, send the body: returns (status, body)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()
    head = (
        f"POST /m/crm HTTP/1.1\r\nHost: 127.0.0.1:{env.port}\r\nAuthorization: Bearer {cap}\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", env.port), timeout=5) as s:
        s.sendall(head.encode())
        time.sleep(0.2)
        then()
        s.sendall(body)
        blob = b""
        while chunk := s.recv(65536):
            blob += chunk
    return int(blob.split(b" ")[1])


@pytest.mark.parametrize("how", ["run", "workspace", "binding"])
def test_a_revoke_between_headers_and_body_forwards_nothing(env, how):
    cap = mint(env)
    env.bindings = {"w1": ("h1", 1)}

    def revoke():
        if how == "run":
            env.proxy.revoke_run("r1")
        elif how == "workspace":
            env.proxy.revoke_workspace("w1")
        else:
            env.bindings = {"w1": None}

    assert _half_send(env, cap, revoke) == 401
    assert all(not up.posts for up in env.ups)
    assert rpc(env, cap, "ping")[0] == 401


def test_up_refuses_a_dropped_capability(env):
    cap = mint(env)
    obj = next(iter(env.proxy._caps.values()))
    env.proxy.revoke_run("r1")
    assert cap
    with pytest.raises(mcpproxy._Refuse):
        env.proxy._up(obj, "crm", env.records["w1"][0], None)
    assert not env.ups


# -- review fix 2: every encoding of a secret is scrubbed ---------------------------------------------------------

VAL = "pässw/ord+x y=1&z~k"


def _hex_case(text: str, fn) -> str:
    return re.sub(r"%[0-9A-Fa-f]{2}", lambda m: fn(m.group(0)), text)


def test_scrub_percent_forms_in_any_hex_case():
    forms = mcpproxy._secret_forms(VAL)
    for base in (
        urllib.parse.quote(VAL),
        urllib.parse.quote(VAL, safe=""),
        urllib.parse.quote_plus(VAL),
    ):
        for text in (base, _hex_case(base, str.lower), _hex_case(base, str.upper)):
            assert mcpproxy._scrub(f"a {text} b", forms) == "a [redacted] b", text


def test_scrub_hex_forms():
    forms = mcpproxy._secret_forms(VAL)
    h = VAL.encode().hex()
    assert mcpproxy._scrub(f"x{h}y", forms) == "x[redacted]y"
    assert mcpproxy._scrub(f"x{h.upper()}y", forms) == "x[redacted]y"


@pytest.mark.parametrize("alignment", range(6))
@pytest.mark.parametrize("enc", [base64.b64encode, base64.urlsafe_b64encode])
def test_scrub_base64_at_every_alignment(alignment, enc):
    forms = mcpproxy._secret_forms(VAL)
    whole = enc(bytes(range(97, 97 + alignment)) + VAL.encode() + b"\xff\xfe\x01").decode()
    out = mcpproxy._scrub(whole, forms)
    assert "[redacted]" in out
    assert out.count("[redacted]") == 1


def test_scrub_never_redacts_short_forms():
    forms = mcpproxy._secret_forms("abc")
    assert forms is None
    assert mcpproxy._scrub("abc abc", forms) == "abc abc"
    forms = mcpproxy._secret_forms("abcdefgh")
    assert mcpproxy._scrub("ab abcdefgh", forms) == "ab [redacted]"


# -- review fix 4: arguments are checked against the pinned schema ---------------------------------------------------

STRICT = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
    "required": ["q"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("arguments", "ok"),
    [
        ({"q": "x"}, True),
        ({}, False),  # a required key is missing
        ({"q": "x", "extra": 1}, False),  # additionalProperties: false
        ([1], False),  # not an object
        ("str", False),
        (None, False),
    ],
)
def test_call_arguments_against_the_pinned_schema(env, arguments, ok):
    t = tool("echo", schema=STRICT)
    turn_on(env, t)
    env.up_factory = lambda rec, bearer: FakeUp(tools=[t])
    cap = mint(env)
    st, body, _ = rpc(env, cap, "tools/call", {"name": "echo", "arguments": arguments})
    posts = [m for up in env.ups for m in up.posts if m.get("method") == "tools/call"]
    if ok:
        assert st == 200 and "result" in body and len(posts) == 1
    else:
        assert body["error"] == {"code": -32602, "message": "invalid params"} and not posts


# -- review fix 5: an agent cannot slow the host ---------------------------------------------------------------------


def test_connection_cap_answers_503_beyond_32(env):
    socks = []
    try:
        for _ in range(mcpproxy.CONNECTIONS_MAX):
            socks.append(socket.create_connection(("127.0.0.1", env.port), timeout=5))
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", env.port), timeout=5)
        socks.append(extra)
        assert extra.recv(4096).startswith(b"HTTP/1.1 503")
    finally:
        for s in socks:
            s.close()
    time.sleep(0.3)
    assert rpc(env, mint(env), "ping")[0] == 200  # slots are freed again


def test_a_request_counts_before_the_served_check(env):
    cap = mint(env)

    def boom(ws):
        raise RuntimeError("down")

    env.proxy._served = boom
    codes = [rpc(env, cap, "ping", rid=i)[0] for i in range(mcpproxy.CALLS_PER_MINUTE + 1)]
    assert codes[:3] == [503, 503, 503] and codes[-1] == 429


def test_served_and_policy_are_asked_once_per_request(env):
    t = tool("echo")
    turn_on(env, t)
    env.up_factory = lambda rec, bearer: FakeUp(tools=[t])
    cap = mint(env)
    calls = {"served": 0, "policy": 0}
    served, policy = env.proxy._served, env.proxy._tool_policy

    def count_served(ws):
        calls["served"] += 1
        return served(ws)

    def count_policy(ws, s):
        calls["policy"] += 1
        return policy(ws, s)

    env.proxy._served, env.proxy._tool_policy = count_served, count_policy
    assert rpc(env, cap, "tools/call", {"name": "echo", "arguments": {}})[0] == 200
    assert calls == {"served": 1, "policy": 1}


def test_upstream_posts_run_in_the_proxys_own_bounded_pool(env):
    names = []
    env.up_factory = lambda rec, bearer: FakeUp(on_post=lambda m: names.append(threading.current_thread().name))
    cap = mint(env)
    assert rpc(env, cap, "initialize", {})[0] == 200
    assert names and all(n.startswith("orch-mcp") for n in names)
    assert env.proxy._pool._max_workers == 8
    pool = env.proxy._pool
    asyncio.run(env.proxy.close())
    assert pool._shutdown


# -- review fix 6: the 24 h cap also runs on the wall clock ----------------------------------------------------------


def test_capability_expires_on_the_wall_clock_even_if_the_monotonic_one_stood_still(env):
    wall = {"now": 5_000_000.0}
    env.proxy._wall = lambda: wall["now"]
    cap = mint(env)
    assert rpc(env, cap, "ping")[0] == 200
    wall["now"] += mcpproxy.CAPABILITY_TTL - 1
    assert rpc(env, cap, "ping")[0] == 200
    wall["now"] += 2  # the laptop slept: monotonic did not move
    assert rpc(env, cap, "ping")[0] == 401
    assert not env.proxy._caps


# -- review fix 8: the binding is (hub_id, binding_seq); the capability shape is redacted by the host -------------


def test_a_binding_to_another_hub_with_the_same_number_kills_the_capability(env):
    cap = mint(env)
    assert rpc(env, cap, "ping")[0] == 200
    env.bindings = {"w1": ("h2", 1)}  # left hub h1, joined h2: the sequence number alone would say yes
    assert rpc(env, cap, "ping")[0] == 401
    with pytest.raises(ValueError):
        env.proxy.mint("w1", 1, "r1", env.records["w1"], hub_id="h1")
    assert env.proxy.mint("w1", 1, "r1", env.records["w1"], hub_id="h2")

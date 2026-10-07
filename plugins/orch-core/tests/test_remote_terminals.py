"""Terminals over the bridge (R6): who may watch and who may type, the typing lease, post numbers, the rate limit, the
size cap, the snapshot, and a revoke ending a terminal stream. A fake tmux; no real tmux, network or TIX."""
import asyncio
import json
import subprocess

import pytest

pytest.importorskip("cryptography")
pytest.importorskip("fastapi")

import test_bridge_host as H  # noqa: E402
import test_remote_serve as S  # noqa: E402
from orch.dashboard import routes_terminals, terminals  # noqa: E402
from orch.dashboard.app import create_app, dashboard_routes  # noqa: E402
from orch.dashboard.bridge_dispatch import Body, BridgeRequest, Limits, Start, dispatch  # noqa: E402
from orch.dashboard.bridge_loop import route_hook  # noqa: E402
from orch.dashboard.reach import RemoteOrigin, Scope  # noqa: E402
from orch.remote.bridge_host import envelope as E  # noqa: E402
from orch.remote.bridge_host.host_check import Host, Requirement  # noqa: E402
from orch.remote.bridge_host.registry import Device  # noqa: E402
from orch.remote.bridge_host import files  # noqa: E402

fake = S.fake  # the fake sharing child's directory
_forget_loops = S._forget_loops  # noqa: SLF001


class FakeTmux:
    """list-sessions, capture-pane and send-keys for one session, DEMO-1; the keys typed are kept."""

    def __init__(self, root):
        self.root, self.typed, self.ended = root, [], False

    def __call__(self, args, timeout=5):
        out = ""
        if args[0] == "list-sessions":
            fmt = args[args.index("-F") + 1]
            out = "DEMO-1" if fmt == "#{session_name}" else f"DEMO-1\t{self.root}\t100\t200\t200\t50\t4000"
        elif args[0] == "capture-pane":
            out = "hello\n200 50\n"
        elif args[0] == "send-keys":
            self.typed.append(args[-1])
        elif args[0] == "kill-session":
            self.ended = True
        return subprocess.CompletedProcess(args, 0, out, "")


@pytest.fixture
def tmux(ws, monkeypatch):
    t = FakeTmux(ws.root)
    monkeypatch.setattr(terminals, "tmux", t)
    monkeypatch.setattr(terminals, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(terminals, "addon_on", lambda w: True)
    monkeypatch.setattr(routes_terminals, "VIEW_SECONDS", 0.02)
    monkeypatch.setattr(routes_terminals, "VIEW_IDLE_SECONDS", 0.05)
    routes_terminals._DEVICE_LAST.clear()  # noqa: SLF001
    routes_terminals._DEVICE_RATE.clear()  # noqa: SLF001
    return t


def origin(scope, device="dev_abc123"):
    return RemoteOrigin(device, scope, "Pixel 8", False)


def call(app, method, path, scope, body=None):
    """One request through the dispatcher as a paired device; (status, body bytes)."""
    async def main():
        req = BridgeRequest(method, path, {"content-type": "application/json"} if body is not None else {},
                            json.dumps(body).encode() if body is not None else b"")
        status, out = None, b""
        async for ev in dispatch(app, req, origin(scope), still_authorized=lambda: True, limits=Limits()):
            if isinstance(ev, Start):
                status = ev.status
            elif isinstance(ev, Body):
                out += ev.chunk
        return status, out
    return asyncio.run(main())


@pytest.fixture
def app(ws):
    return create_app(ws, "tok-terminals-1")


# -- the scope ladder, at the dispatcher -----------------------------------------------------------------------------

TYPING = [("POST", "/terminals/DEMO-1/keys", {"seq": [{"text": "x"}], "n": 1}),
          ("POST", "/terminals/DEMO-1/size", {"cols": 80, "rows": 24}),
          ("POST", "/terminals/DEMO-1/end", None), ("POST", "/terminals/new", None)]
WATCHING = [("GET", "/terminals"), ("GET", "/terminals/DEMO-1"), ("GET", "/terminals/DEMO-1/snapshot")]


@pytest.mark.parametrize("scope", [Scope.LOOK, Scope.DECIDE])
def test_a_look_or_decide_device_can_neither_watch_nor_type(app, tmux, scope):
    for method, path in WATCHING:
        assert call(app, method, path, scope)[0] == 403, path
    for method, path, body in TYPING:
        assert call(app, method, path, scope, body)[0] == 403, path
    assert tmux.typed == [] and not tmux.ended


def test_an_operate_device_can_watch_but_not_type(app, tmux):
    status, body = call(app, "GET", "/terminals/DEMO-1/snapshot", Scope.OPERATE)
    assert status == 200 and json.loads(body)["screen"]["tail"] and json.loads(body)["info"]["title"] == "DEMO-1"
    for method, path, body in TYPING:
        assert call(app, method, path, Scope.OPERATE, body)[0] == 403, path
    assert tmux.typed == [] and not tmux.ended


def test_a_type_device_types_and_a_local_post_needs_no_number(app, tmux, ws):
    assert call(app, "POST", "/terminals/DEMO-1/keys", Scope.TYPE, {"seq": [{"text": "ls"}, {"key": "Enter"}], "n": 7})[0] == 204
    assert tmux.typed == ["ls", "Enter"]
    from fastapi.testclient import TestClient
    c = TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    assert c.get("/?token=tok-terminals-1").status_code == 200
    r = c.post("/terminals/DEMO-1/keys", json={"seq": [{"text": "local"}]}, headers={"origin": "http://127.0.0.1:8765"})
    assert r.status_code == 204 and tmux.typed[-1] == "local"


# -- post numbers, rate limit, size ----------------------------------------------------------------------------------

def keys(app, n, text="a", device_scope=Scope.TYPE):
    body = {"seq": [{"text": text}]}
    if n is not None:
        body["n"] = n
    return call(app, "POST", "/terminals/DEMO-1/keys", device_scope, body)[0]


def test_a_post_that_arrives_twice_or_late_never_types_twice(app, tmux):
    assert keys(app, 10, "one") == 204
    assert keys(app, 10, "again") == 409  # the same post twice
    assert keys(app, 9, "late") == 409  # a post older than one already taken
    assert keys(app, 11, "two") == 204
    assert tmux.typed == ["one", "two"]


def test_a_device_post_without_a_number_is_refused(app, tmux):
    assert keys(app, None) == 400
    assert call(app, "POST", "/terminals/DEMO-1/keys", Scope.TYPE, {"seq": [{"text": "a"}], "n": "5"})[0] == 400
    assert call(app, "POST", "/terminals/DEMO-1/keys", Scope.TYPE, {"seq": [{"text": "a"}], "n": True})[0] == 400
    assert tmux.typed == []


def test_numbers_are_kept_per_device(app, tmux):
    async def one(device, n):
        req = BridgeRequest("POST", "/terminals/DEMO-1/keys", {"content-type": "application/json"},
                            json.dumps({"seq": [{"text": "k"}], "n": n}).encode())
        return [e.status async for e in dispatch(app, req, origin(Scope.TYPE, device), still_authorized=lambda: True)
                if isinstance(e, Start)][0]
    assert asyncio.run(one("dev_a", 5)) == 204
    assert asyncio.run(one("dev_b", 5)) == 204  # another device's counter is its own
    assert asyncio.run(one("dev_a", 5)) == 409


def test_the_rate_limit_counts_per_device_and_a_refused_post_keeps_its_number(app, tmux, monkeypatch):
    monkeypatch.setattr(routes_terminals.time, "monotonic_ns", lambda: 0)
    statuses = [keys(app, n) for n in range(1, routes_terminals.REMOTE_POSTS + 3)]
    assert statuses == [204] * routes_terminals.REMOTE_POSTS + [429, 429]
    n_refused = routes_terminals.REMOTE_POSTS + 1
    monkeypatch.setattr(routes_terminals.time, "monotonic_ns",
                        lambda: (routes_terminals.REMOTE_WINDOW_MS + 1) * 1_000_000)
    assert keys(app, n_refused) == 204  # the same number goes through once the window has passed
    assert len(tmux.typed) == routes_terminals.REMOTE_POSTS + 1


def test_a_post_over_the_size_cap_is_refused(app, tmux):
    assert call(app, "POST", "/terminals/DEMO-1/keys", Scope.TYPE,
                {"seq": [{"text": "x" * 513}] * 5, "n": 1})[0] == 413  # 2565 characters
    assert call(app, "POST", "/terminals/DEMO-1/keys", Scope.TYPE,
                {"seq": [{"key": "Up"}] * 65, "n": 2})[0] == 413
    assert keys(app, 3) == 204  # a refused post took no number
    assert tmux.typed == ["a"]


# -- the typing lease, at the host with the real route hook ---------------------------------------------------------

HOOK = route_hook(dashboard_routes())


def meta(method, path):
    return {"method": method, "path": path}


def test_the_hook_asks_for_the_lease_on_every_typing_route_and_for_nothing_on_watching():
    need = lambda m, p: HOOK(meta(m, p), b"")  # noqa: E731
    for m, p in [("POST", "/terminals/DEMO-1/keys"), ("POST", "/terminals/DEMO-1/size"),
                 ("POST", "/terminals/DEMO-1/end"), ("POST", "/terminals/new"), ("POST", "/t/B-0001/agent/start")]:
        assert need(m, p) == Requirement("type", "lease"), p
    for p in ("/terminals", "/terminals/stream", "/terminals/DEMO-1", "/terminals/DEMO-1/stream",
              "/terminals/DEMO-1/snapshot"):
        assert need("GET", p) == Requirement("operate", "none"), p
    assert need("POST", "/schedules/x/arm") == Requirement("type", "none")  # a fresh route stays the gate's to refuse
    assert need("POST", "/workspace/tidy") is None


@pytest.fixture
def clock():
    return H.V.Clock(H.NOW)


def typing_host(tmp_path, clock, scope="type", cred=True):
    host = Host(workspace=H.WS, k_ws=H.V.K_WS, host_key=S.HOST_KEY, root=files.bridge_dir(tmp_path, H.WS_HEX),
                clock=clock, route=HOOK, phone_key=lambda pid: None, rp_id=H.V.RP_ID, origin=H.V.ORIGIN)
    host.registry.add(Device(H.did(H.KEY_A), H.pub(H.KEY_A), scope, "Laptop", 0,
                             credential=H.credential() if cred else None), 0)
    return host


def open_stream(host, path="/terminals/DEMO-1/stream", seq=1):
    e = H.env(H.KEY_A, {"op": "http", "method": "GET", "path": path}, seq=seq, flags=E.F_STREAM)
    assert host.authorize(H.send(host, e)).result == "run"
    return bytes.fromhex(H.rid_of(e))


def post(host, stream, seq, path="/terminals/DEMO-1/keys", ts=None):
    e = H.env(H.KEY_A, {"op": "http", "method": "POST", "path": path}, b'{"seq":[],"n":1}', seq=seq,
              stream=stream, ts=ts or H.NOW)
    checked = H.send(host, e)
    return e, host.authorize(checked) if checked.result == "accept" else checked  # an unknown stream: refused at check


def test_a_type_device_types_only_inside_a_fresh_lease_on_its_own_stream(tmp_path, clock):
    host = typing_host(tmp_path, clock)
    stream = open_stream(host)
    e, refusal = post(host, stream, 2)
    assert refusal.code == "lease_required"  # a Type scope alone types nothing
    run = host.authorize(H.send(host, H.env(H.KEY_A, H.assertion_for(refusal, H.rid_of(e)), seq=3)))
    assert run.result == "run"
    assert post(host, stream, 4)[1].result == "run"  # inside the lease
    for path in ("/terminals/DEMO-1/size", "/terminals/DEMO-1/end"):
        assert post(host, stream, 5 + len(path) % 7, path)[1].result == "run"
    clock.now += 15 * 60_000
    assert post(host, stream, 20, ts=clock.now)[1].code == "lease_required"  # 15 minutes: over


def test_typing_without_a_stream_of_its_own_is_refused_whatever_the_scope(tmp_path, clock):
    host = typing_host(tmp_path, clock)
    assert post(host, E.ZERO_ID, 1)[1].code == "assertion_failed"
    assert post(host, bytes(range(16)), 2)[1].result == "refuse"  # a stream id this device did not open


def test_a_device_without_a_platform_authenticator_cannot_get_type(tmp_path, clock):
    host = typing_host(tmp_path, clock, cred=False)
    stream = open_stream(host)
    e, refusal = post(host, stream, 2)
    assert refusal.code == "lease_required"
    answer = host.authorize(H.send(host, H.env(H.KEY_A, {"op": "assert", "for": H.rid_of(e), "credential_id": "",
                                                         "authenticator_data": "", "client_data_json": "",
                                                         "signature": ""}, seq=3)))
    assert answer.code == "assertion_failed" and host.leases == {}
    assert post(host, stream, 4)[1].code == "lease_required"


@pytest.mark.parametrize("scope", ["look", "decide", "operate"])
def test_below_type_never_types_and_only_operate_watches(tmp_path, clock, scope):
    host = typing_host(tmp_path, clock, scope)
    e = H.env(H.KEY_A, {"op": "http", "method": "GET", "path": "/terminals/DEMO-1/stream"}, seq=1, flags=E.F_STREAM)
    watch = host.authorize(H.send(host, e))
    assert (watch.result == "run") == (scope == "operate")
    assert watch.result == "run" or watch.code == "forbidden_scope"
    stream = bytes.fromhex(H.rid_of(e))
    assert post(host, stream, 2)[1].code == "forbidden_scope"


def test_a_revoke_cancels_the_lease_and_the_streams(tmp_path, clock):
    host = typing_host(tmp_path, clock)
    stream = open_stream(host)
    e, refusal = post(host, stream, 2)
    host.authorize(H.send(host, H.env(H.KEY_A, H.assertion_for(refusal, H.rid_of(e)), seq=3)))
    assert host.leases
    ended = host.revoke(H.did(H.KEY_A))
    assert ended == [stream.hex()] and host.leases == {} and host.streams == {}
    assert post(host, stream, 4)[1].code == "revoked"


# -- a revoke ends a terminal stream, through the loop ---------------------------------------------------------------

def test_a_revoke_ends_the_devices_terminal_stream_at_once(ws, fake, tmux):
    host, a = S.make_host(), S.Device_(S.KEY_A)

    async def main():
        loop = S.make_loop(ws, host, keepalive_s=60)  # no keepalive would come in time: the close does it
        task = await S.started(loop)
        rid = fake.request(a.envelope(S.http("GET", "/terminals/DEMO-1/stream", {"accept": "text/event-stream"}),
                                      stream_flag=True))
        await S.until(lambda: any(b"event: " in c[2] for c in fake.chunks(rid)))  # frames keep only the latest event
        loop.close_streams(host.revoke(S.did(S.KEY_A)), "revoked")
        await S.until(lambda: fake.chunks(rid)[-1][0].flags & E.F_LAST, timeout=3)
        assert fake.chunks(rid)[-1][1] == {"refusal": "revoked"}
        await S.until(lambda: rid not in loop._streams and rid not in host.streams)  # noqa: SLF001
        await S.finish(loop, task)
    S.arun(main())
    captures = len([c for c in tmux.typed])
    assert captures == 0


def test_a_scope_change_returns_the_devices_streams_and_clears_its_lease(tmp_path, clock):
    host = typing_host(tmp_path, clock)
    stream = open_stream(host)
    e, refusal = post(host, stream, 2)
    host.authorize(H.send(host, H.env(H.KEY_A, H.assertion_for(refusal, H.rid_of(e)), seq=3)))
    assert host.set_scope(H.did(H.KEY_A), "operate") == [stream.hex()]
    assert host.leases == {} and host.streams == {}

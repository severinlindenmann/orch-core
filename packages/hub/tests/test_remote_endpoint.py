"""The remote-client endpoint and its authorization (R4, #1592): protocol, fail-closed parsing, scopes, logs."""

from __future__ import annotations

import logging
import threading
import time

import pytest
from remote_helpers import CODE, DEV, DEV2, HUB, WS_A, WS_B, FakeConn, FakePort, answer, data_frame
from webapi_helpers import Web

from orch_hub.channels.service import REMOTE_SESSION_TTL_MS
from orch_hub.contract import remote as R
from orch_hub.contract.canon import b64u
from orch_hub.remote import Authz, AuthzError, RemoteEndpoint, StreamRouter, wire_remote
from orch_hub.webapi.api import ROUTES, SCOPE_ROUTES


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    yield web
    web.close()


def remote_token(w, user=None):
    u = user or w.ann
    s, o, _ = w.call(
        "POST",
        "/session",
        {"user_id": u, "password": "correct horse battery", "totp": w.env.next_code(u), "client": "remote"},
        origin=None,
    )
    assert s == 200, o
    return o["token"]


class Rig:
    def __init__(self, w, rail=None, **ep):
        self.w = w
        self.router = StreamRouter(hub_id=HUB)
        self.port, self.port_b = FakePort(), FakePort()
        self.router.attach(WS_A, self.port)
        self.router.attach(WS_B, self.port_b)
        self.rail = rail if rail is not None else {w.ann: {WS_A}}
        self.authz = Authz(w.env.svc, lambda u: self.rail.get(u, ()))
        self.ep = RemoteEndpoint(self.router, self.authz, setup_timeout=3, idle_poll=0.2, **ep)
        self.threads = []

    def connect(self):
        srv, cli = FakeConn.pair()
        t = threading.Thread(target=self.ep.serve, args=(srv,), daemon=True)
        t.start()
        self.threads.append(t)
        return cli

    def session(self, token, ws=WS_A, dev=DEV, kind="session", host_body=None):
        port = self.port if ws == WS_A else self.port_b
        n = len(port.ops("stream_open")) + 1
        c = self.connect()
        c.put({"t": "hello", "v": 1, "token": token})
        assert c.get() == {"t": "ready", "hub_id": HUB}
        c.put({"t": "open", "workspace_id": ws, "device_id": dev, "kind": kind})
        sid = answer(self.router, ws, port, host_body, n)
        return c, sid


class RealRig(Rig):
    """The production wiring (``wire_remote``) on the real Channels, Access and rail rule; WS (full mode, ann edits)."""

    def __init__(self, w, **ep):
        self.w = w
        w.reg.add(WS_A, "full")
        w.env.acc.grant(WS_A, "team", "edit")
        self.router, endpoint = wire_remote(w.env.svc, w.reg, w.env.acc, hub_id=HUB)
        self.port = FakePort()
        self.router.attach(WS_A, self.port)
        self.ep = RemoteEndpoint(self.router, endpoint._authz, setup_timeout=3, idle_poll=0.2, **ep)
        self.threads = []


def test_stream_sign_out_everywhere_closes_remote_streams_revoked_at_once(w):
    rig = RealRig(w)
    c, sid = rig.session(remote_token(w))
    assert c.get() == {"t": "opened", "stream_id": sid}
    w.env.svc.end_all_sessions(w.ann)
    assert c.get() == {"t": "close", "reason": "revoked"}
    assert rig.router.open_count() == 0 and rig.port.wait_op("stream_close")[0][1]["reason"] == "revoked"


def test_stream_disabled_user_closed_at_the_next_sweep(w):
    rig = RealRig(w)
    c, _sid = rig.session(remote_token(w))
    c.get()
    rig.router.sweep()
    assert rig.router.open_count() == 1  # nothing changed: still allowed
    w.env.acc.remove_user(w.ann)
    rig.router.sweep()
    assert c.get() == {"t": "close", "reason": "revoked"} and rig.router.open_count() == 0


def test_stream_signed_out_token_closed_at_the_next_sweep(w):
    rig = RealRig(w)
    tok = remote_token(w)
    c, _sid = rig.session(tok)
    c.get()
    w.env.svc.sign_out(tok)  # the token that opened the stream is gone: the stream must not outlive it
    rig.router.sweep()
    assert c.get() == {"t": "close", "reason": "revoked"}


# --- sign-in scope ----------------------------------------------------------------------------------------------


def test_stream_remote_sign_in_makes_a_seven_day_bearer_token(w):
    s, o, h = w.call(
        "POST",
        "/session",
        {"user_id": w.ann, "password": "correct horse battery", "totp": w.env.next_code(w.ann), "client": "remote"},
        origin=None,
    )
    assert s == 200 and "Set-Cookie" not in h and o["expires_ms"] - w.clock() == REMOTE_SESSION_TTL_MS
    assert w.env.svc.session_info(o["token"])["scope"] == "remote"


def test_stream_remote_token_refused_on_every_web_route(w):
    tok = remote_token(w)
    assert {h for h, v in SCOPE_ROUTES.items() if "remote" in v} == {"sign_out"}  # the one route: ending its own token
    for rt in ROUTES:
        if rt.handler == "sign_out":
            continue
        path = rt.pattern if hasattr(rt, "pattern") and isinstance(rt.pattern, str) else None
        path = path or getattr(rt, "path", "/")
        path = "/".join(x if not x.startswith("{") else "x" * 32 for x in path.split("/"))
        for cookie in (False, True):
            s, o, _ = w.call(rt.method, path, {} if rt.method != "GET" else None, token=tok, cookie=cookie)
            assert s in (401, 403, 404, 405) and s != 200, (rt.method, path, s, o)
            assert s != 404 or o == {"error": "unknown_op"}


def test_stream_remote_token_can_sign_itself_out_only(w):
    tok = remote_token(w)
    s, _, _ = w.call("DELETE", "/session", None, token=tok, cookie=False)
    assert s == 200
    with pytest.raises(AuthzError):
        Authz(w.env.svc, lambda u: ()).authenticate(tok)


def test_stream_full_scope_token_refused_on_endpoint(w):
    rig = Rig(w)
    for tok in (w.login(w.ann), w.login(w.ann, client="app"), "nonsense", ""):
        c = rig.connect()
        c.put({"t": "hello", "v": 1, "token": tok})
        assert c.get() == {"t": "error", "error": "no_session"}
        with pytest.raises(Exception):  # noqa: B017  # closed after the error
            c.get(0.5)


def test_stream_authenticate_checks_user_still_active(w):
    rig = Rig(w)
    tok = remote_token(w)
    assert rig.authz.authenticate(tok) == w.ann
    w.env.acc.remove_user(w.ann)
    with pytest.raises(AuthzError):
        rig.authz.authenticate(tok)


# --- authorization ----------------------------------------------------------------------------------------------


def test_stream_other_users_workspace_not_allowed(w):
    rig = Rig(w, rail={w.ann: {WS_A}, w.admin: {WS_B}})
    c = rig.connect()
    c.put({"t": "hello", "v": 1, "token": remote_token(w)})
    assert c.get()["t"] == "ready"
    c.put({"t": "open", "workspace_id": WS_B, "device_id": DEV, "kind": "session"})
    assert c.get() == {"t": "refused", "error": "not_allowed"}
    assert rig.port_b.ops("stream_open") == []


def test_stream_unbound_and_unknown_same_answer(w):
    rig = Rig(w, rail={w.ann: {WS_A, "33" * 16}})  # in the rail but no live link
    answers = []
    for ws in ("33" * 16, "44" * 16, WS_B):  # offline-in-rail, unknown, someone else's
        c = rig.connect()
        c.put({"t": "hello", "v": 1, "token": remote_token(w)})
        c.get()
        c.put({"t": "open", "workspace_id": ws, "device_id": DEV, "kind": "session"})
        answers.append(c.get())
    assert answers == [{"t": "refused", "error": "not_allowed"}] * 3


def test_stream_workspace_not_in_rail_refused_even_if_live(w):
    rig = Rig(w, rail={w.ann: set()})
    c = rig.connect()
    c.put({"t": "hello", "v": 1, "token": remote_token(w)})
    c.get()
    c.put({"t": "open", "workspace_id": WS_A, "device_id": DEV, "kind": "session"})
    assert c.get() == {"t": "refused", "error": "not_allowed"}


def test_stream_rail_rule_uses_workspace_rows(w):
    from webapi_helpers import WS

    a = Authz.from_hub(w.env.svc, w.reg, w.env.acc)
    assert a.may_open(w.ann, WS) and not a.may_open(w.env.user("stranger"), WS)
    assert not a.may_open(w.ann, "99" * 16)


# --- the happy path and data ------------------------------------------------------------------------------------


def test_stream_round_trip_through_the_endpoint(w):
    rig = Rig(w)
    c, sid = rig.session(remote_token(w))
    assert c.get() == {"t": "opened", "stream_id": sid}
    c.put({"t": "data", "b": b64u(b"msg1")})
    _seq, body = rig.port.wait_op("stream_data")[0]
    assert body == {"stream_id": sid, "seq": 1, "bytes": b64u(b"msg1")}
    rig.router.on_frame(WS_A, rig.port, data_frame(sid, 1, b"reply"), 1024)
    assert c.get() == {"t": "data", "b": b64u(b"reply")}
    c.put({"t": "close", "reason": "closed"})
    assert rig.port.wait_op("stream_close")[0][1] == {"stream_id": sid, "reason": "closed"}
    assert c.get() == {"t": "close", "reason": "closed"}
    assert rig.router.open_count() == 0


def test_stream_enroll_open_uses_code_id(w):
    rig = Rig(w)
    c, _sid = rig.session(remote_token(w), dev=CODE, kind="enroll")
    assert c.get()["t"] == "opened" and rig.port.ops("stream_open")[0][1]["kind"] == "enroll"


def test_stream_host_close_reaches_client_verbatim(w):
    rig = Rig(w)
    c, sid = rig.session(remote_token(w))
    c.get()
    from remote_helpers import close_frame

    rig.router.on_frame(WS_A, rig.port, close_frame(sid, "handshake"), 1024)
    assert c.get() == {"t": "close", "reason": "handshake"}


def test_stream_host_refusal_reaches_client(w):
    rig = Rig(w)
    c, _ = rig.session(remote_token(w), host_body={"error": "remote.off"})
    assert c.get() == {"t": "refused", "error": "remote.off"}
    c, _ = rig.session(remote_token(w), dev=DEV2, host_body={"error": "free text"})
    assert c.get() == {"t": "refused", "error": "unavailable"}


def test_stream_reconnect_closes_client_connection_closed(w):
    rig = Rig(w)
    c, _sid = rig.session(remote_token(w))
    c.get()
    rig.router.detach(WS_A, rig.port)
    assert c.get() == {"t": "close", "reason": "closed"}
    assert rig.router.open_count() == 0


def test_stream_slow_client_closes_busy(w):
    rig = Rig(w, queue_frames=2)
    c, sid = rig.session(remote_token(w))
    # the client never reads; the writer drains into the fake conn buffer, so stall it by closing our read side
    c.get()
    srv_side = c.peer
    srv_side.send_all = lambda *a, **k: threading.Event().wait(5)  # a stuck socket  # type: ignore[method-assign]
    for i in range(1, 8):
        rig.router.on_frame(WS_A, rig.port, data_frame(sid, i, b"x"), 1024)
    assert not rig.router.alive(sid)
    assert rig.port.ops("stream_close")[-1][1]["reason"] == "remote.busy"


# --- fail closed ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hello",
    [
        {"t": "hello", "v": 2, "token": "x"},
        {"t": "hello", "v": 1},
        {"t": "hello", "v": 1, "token": "x", "extra": 1},
        {"t": "open", "v": 1, "token": "x"},
        {"t": "hello", "v": 1, "token": 5},
    ],
)
def test_stream_malformed_hello_fails_closed(w, hello):
    rig = Rig(w)
    c = rig.connect()
    c.put(hello)
    assert c.get() == {"t": "error", "error": "malformed"}


def test_stream_token_signed_out_after_ready_is_refused_at_open(w):
    rig = Rig(w)
    tok = remote_token(w)
    c = rig.connect()
    c.put({"t": "hello", "v": 1, "token": tok})
    assert c.get() == {"t": "ready", "hub_id": HUB}
    w.env.svc.sign_out(tok)  # signed out between ready and open: the open re-authenticates
    c.put({"t": "open", "workspace_id": WS_A, "device_id": DEV, "kind": "session"})
    assert c.get() == {"t": "error", "error": "no_session"}
    assert rig.port.ops("stream_open") == [] and rig.router.open_count() == 0


@pytest.mark.parametrize(
    "op",
    [
        {"t": "data", "b": "AAAA"},  # data before opened
        {"t": "open", "workspace_id": WS_A, "device_id": DEV},
        {"t": "open", "workspace_id": WS_A, "device_id": DEV, "kind": "session", "stream_id": "x"},
        {"t": "open", "workspace_id": "XYZ", "device_id": DEV, "kind": "session"},
        {"t": "open", "workspace_id": WS_A, "device_id": CODE, "kind": "session"},
        {"t": "open", "workspace_id": WS_A, "device_id": DEV, "kind": "enroll"},
        {"t": "open", "workspace_id": WS_A, "device_id": DEV, "kind": "other"},
    ],
)
def test_stream_malformed_open_refused_nothing_sent_to_host(w, op):
    rig = Rig(w)
    c = rig.connect()
    c.put({"t": "hello", "v": 1, "token": remote_token(w)})
    c.get()
    c.put(op)
    assert c.get() == {"t": "refused", "error": "malformed"}
    assert rig.port.ops("stream_open") == []


@pytest.mark.parametrize(
    "frame,reason",
    [
        ({"t": "data", "b": "!!!"}, "malformed"),
        ({"t": "data", "b": ""}, "malformed"),
        ({"t": "data", "b": "AAAA", "x": 1}, "malformed"),
        ({"t": "data"}, "malformed"),
        ({"t": "open", "workspace_id": WS_A, "device_id": DEV, "kind": "session"}, "malformed"),
        ({"t": "close", "reason": "because"}, "malformed"),
        ({"t": "close", "reason": "closed", "x": 1}, "malformed"),
        ({"t": "weird"}, "malformed"),
        ({"t": "data", "b": b64u(b"x" * (R.FRAME_MAX + 1))}, "too_large"),
    ],
)
def test_stream_bad_client_frame_closes_stream(w, frame, reason):
    rig = Rig(w)
    c, sid = rig.session(remote_token(w))
    c.get()
    c.put(frame)
    assert c.get() == {"t": "close", "reason": reason}
    assert rig.port.wait_op("stream_close")[0][1] == {"stream_id": sid, "reason": reason}
    assert rig.port.ops("stream_data") == [] and rig.router.open_count() == 0


def test_stream_oversize_wire_frame_closes_connection(w):
    rig = Rig(w)
    c, sid = rig.session(remote_token(w))
    c.get()
    c.put({"t": "data", "b": "A" * (100 * 1024)})  # over the 96 KiB frame cap
    assert not rig.router.alive(sid) or rig.port.wait_op("stream_close")
    assert rig.port.wait_op("stream_close")[0][1]["reason"] == "malformed"


def test_stream_client_disconnect_closes_host_side(w):
    rig = Rig(w)
    c, sid = rig.session(remote_token(w))
    c.get()
    c.close()
    assert rig.port.wait_op("stream_close")[0][1] == {"stream_id": sid, "reason": "closed"}


def test_stream_connection_pool_limits(w):
    rig = Rig(w, max_connections=2, max_unauthenticated=1)
    c1 = rig.connect()  # holds the one unauthenticated slot (no hello yet)
    c2 = rig.connect()  # second connection: no pre slot, dropped
    with pytest.raises(Exception):  # noqa: B017
        c2.get(1)
    c1.put({"t": "hello", "v": 1, "token": remote_token(w)})
    assert c1.get()["t"] == "ready"
    c3 = rig.connect()  # the slot is free again after sign-in
    c3.put({"t": "hello", "v": 1, "token": remote_token(w)})
    assert c3.get()["t"] == "ready"
    c4 = rig.connect()  # connection cap (2) reached
    with pytest.raises(Exception):  # noqa: B017
        c4.get(1)


def test_stream_one_user_cannot_hold_all_connections_without_opening(w):
    rig = Rig(w, max_idle_per_user=3)
    tok = remote_token(w)
    held = []
    for _ in range(3):
        c = rig.connect()
        c.put({"t": "hello", "v": 1, "token": tok})
        assert c.get()["t"] == "ready"
        held.append(c)  # signed in, no open sent yet
    c = rig.connect()
    c.put({"t": "hello", "v": 1, "token": tok})
    assert c.get() == {"t": "error", "error": "remote.busy"}
    other = rig.connect()  # another user is not affected
    other.put({"t": "hello", "v": 1, "token": remote_token(w, w.admin)})
    assert other.get()["t"] == "ready"
    held[0].close()  # a slot frees when the connection goes
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        c = rig.connect()
        c.put({"t": "hello", "v": 1, "token": tok})
        if c.get()["t"] == "ready":
            break
    else:
        pytest.fail("slot not freed")


def test_stream_signed_in_hook_called_after_hello(w):
    rig = Rig(w)
    calls = []
    srv, cli = FakeConn.pair()
    srv.signed_in = lambda: calls.append(1)  # type: ignore[attr-defined]
    threading.Thread(target=rig.ep.serve, args=(srv,), daemon=True).start()
    cli.put({"t": "hello", "v": 1, "token": remote_token(w)})
    cli.get()
    assert calls == [1]


# --- logs -------------------------------------------------------------------------------------------------------


def test_stream_endpoint_logs_hold_no_token_device_or_bytes(w, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    rig = Rig(w)
    tok = remote_token(w)
    secret = b"ENDPOINT-SECRET-MARKER-42"
    c, sid = rig.session(tok)
    c.get()
    c.put({"t": "data", "b": b64u(secret)})
    rig.port.wait_op("stream_data")
    rig.router.on_frame(WS_A, rig.port, data_frame(sid, 1, secret), 1024)
    c.get()
    c.put({"t": "bogus"})
    c.get()
    text = "\n".join(r.getMessage() for r in caplog.records)
    out = capsys.readouterr()
    for hay in (text, out.out, out.err):
        assert tok not in hay and DEV not in hay and secret.decode() not in hay and b64u(secret) not in hay
    assert "stream_close" in text

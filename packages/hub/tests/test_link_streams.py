"""R4 T1 (#1592, protocol 10.2c): the link plumbing for remote streams. A fake ``StreamSink`` and a fake
``RemoteEndpoint`` stand in for the router (T2); the link server, TLS and the reference host client are real."""

from __future__ import annotations

import os
import threading
import time

import pytest
from conftest import NOW, Hub

from orch_hub.contract import remote as R
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, cj
from orch_hub.contract.labels import L
from orch_hub.link import LinkError, framing, tls

STREAM = "ab" * 16
DEVICE = "rd_" + "cd" * 16


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.ports: list = []
        self.raise_on_frame = False
        self.cv = threading.Condition()

    def _add(self, *e) -> None:
        with self.cv:
            self.events.append(e)
            self.cv.notify_all()

    def attach(self, ws, port) -> None:
        self.ports.append(port)
        self._add("attach", ws, port)

    def detach(self, ws, port) -> None:
        self._add("detach", ws, port)

    def on_frame(self, ws, port, o, cost) -> None:
        self._add("frame", ws, port, o, cost)
        if self.raise_on_frame:
            raise RuntimeError("sink fault")

    def on_reply(self, ws, port, o) -> None:
        self._add("reply", ws, port, o)

    def wait(self, kind: str, n: int = 1, timeout: float = 5.0) -> list[tuple]:
        end = time.monotonic() + timeout
        with self.cv:
            while len([e for e in self.events if e[0] == kind]) < n:
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no {kind} #{n}: {[e[0] for e in self.events]}")
                self.cv.wait(left)
            return [e for e in self.events if e[0] == kind]

    def kinds(self) -> list[str]:
        return [e[0] for e in self.events]


class Endpoint:
    """Records the connections it is handed; ``hold`` keeps each one open until released."""

    def __init__(self, hold: bool = False, boom: bool = False) -> None:
        self.conns: list[tls.TlsConn] = []
        self.alpns: list[bytes] = []
        self.hold, self.boom = hold, boom
        self.release = threading.Event()
        self.cv = threading.Condition()

    def serve(self, conn) -> None:
        with self.cv:
            self.conns.append(conn)
            self.alpns.append(conn.alpn())
            self.cv.notify_all()
        conn.send_all(b"R", time.monotonic() + 5)  # a marker byte: "the endpoint got me"
        if self.boom:
            raise RuntimeError("endpoint fault")
        if self.hold:
            self.release.wait(10)

    def wait(self, n: int = 1) -> None:
        end = time.monotonic() + 5
        with self.cv:
            while len(self.conns) < n:
                if (left := end - time.monotonic()) <= 0:
                    raise AssertionError("endpoint not reached")
                self.cv.wait(left)


@pytest.fixture
def mk():
    made: list[Hub] = []

    def make(sink=None, endpoint=None, **kw) -> Hub:
        h = Hub(streams=sink, remote=endpoint, **kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def joined(hub: Hub):
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    return c


def refused(c, code):
    with pytest.raises(LinkError) as e:
        c.recv_reply()
    assert e.value.code == code
    with pytest.raises((tls.LinkClosed, tls.LinkTimeout)):
        framing.read_frame(c._c, first_timeout=2, rest_timeout=2)


def ping(c) -> None:
    """A ``hub_where`` round trip (the one request a stream link answers): every frame sent before it was processed."""
    try:
        c.request("hub_where", {})
    except LinkError as e:  # the test hub has no addresses to advertise: the signed refusal is still the reply
        assert e.code == "unavailable"


def data_body(seq: int = 1, payload: bytes = b"\x01" * 40) -> dict:
    return {"stream_id": STREAM, "seq": seq, "bytes": b64u(payload)}


def live(hub: Hub, sink: Sink):
    """A bound workspace with a stream link (ALPN ``orch-link-streams/1``) attached to the sink. The plain join link
    before it never attaches to the sink."""
    joined(hub).close()
    c = hub.open(alpn=(tls.STREAM_LINK_ALPN,))
    sink.wait("attach")
    return c, sink.ports[-1]


# --- one-way frames get no reply --------------------------------------------------------------------------------------


def test_stream_one_way_frames_get_no_reply(mk):
    sink = Sink()
    hub = mk(sink)
    c, port = live(hub, sink)
    f1 = c.build("stream_data", data_body())
    c.send_raw(f1)
    c.send_raw(c.build("stream_close", {"stream_id": STREAM, "reason": "closed"}))
    # the very next frame the hub writes is the reply to the request below: nothing was answered before it
    req = c.build("hub_where", {})
    c.send_raw(req)
    r = c.recv_reply()
    assert r["op"] == "hub_where" and r["re"] == req["o"]["seq"] and r["seq"] == 1
    frames = [e for e in sink.events if e[0] == "frame"]
    assert [e[3]["op"] for e in frames] == ["stream_data", "stream_close"]
    assert frames[0][4] == R.frame_cost(f1)  # the exact cost of the signed frame
    assert frames[0][2] is port and frames[0][1] == G.WS_A


def test_stream_open_reply_reaches_sink_by_re_and_port(mk):
    sink = Sink()
    hub = mk(sink)
    c, port = live(hub, sink)
    seq = port.send_request("stream_open", {"stream_id": STREAM, "device_id": DEVICE, "kind": "session"})
    o = c.recv_reply()  # a verified h2w request: the exporter-bound HK_op form
    assert (o["op"], o["re"], o["seq"], o["body"]["stream_id"]) == ("stream_open", None, seq, STREAM)
    c.seq += 1
    reply = {
        "v": 2,
        "suite": 2,
        "kind": "hub.link",
        "hv": 0,
        "hub_id": G.HUB_ID,
        "workspace_id": G.WS_A,
        "dir": "w2h",
        "seq": c.seq,
        "ts_ms": NOW,
        "op": "stream_open",
        "re": seq,
        "body": {"error": "remote.off"},
    }
    c.send_raw(c._wsk.sign_link(reply, c._c.exporter()))
    ev = sink.wait("reply")[0]
    assert ev[1] == G.WS_A and ev[2] is port and ev[3]["re"] == seq and ev[3]["body"] == {"error": "remote.off"}
    # the link stays up and a reply is not answered
    ping(c)


def test_stream_frame_and_reply_dropped_after_binding_changed(mk):
    """The binding changes under a live port (a second link of the same workspace raises binding_seq): the NEXT
    frame and the NEXT stream_open reply are not forwarded and the port is detached from the sink."""
    sink = Sink()
    hub = mk(sink)
    c, port = live(hub, sink)
    seq = port.send_request("stream_open", {"stream_id": STREAM, "device_id": DEVICE, "kind": "session"})
    c.recv_reply()
    c.seq += 1
    c2 = hub.open()
    assert c2.request("binding_put", {"object": G.binding(seq=2, mode="full")}) == {}
    reply = {
        "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": G.HUB_ID, "workspace_id": G.WS_A,
        "dir": "w2h", "seq": c.seq, "ts_ms": NOW, "op": "stream_open", "re": seq, "body": {},
    }  # fmt: skip
    c.send_raw(c._wsk.sign_link(reply, c._c.exporter()))
    c.send_raw(c.build("stream_data", data_body()))
    ping(c)  # processed in order: both are behind us
    assert "reply" not in sink.kinds() and "frame" not in sink.kinds()
    assert port in [e[2] for e in sink.wait("detach")]


def test_stream_open_from_the_host_is_unknown_op(mk):
    sink = Sink()
    c, _ = live(hub := mk(sink), sink)
    c.send_raw(c.build("stream_open", {"stream_id": STREAM, "device_id": DEVICE, "kind": "session"}))
    refused(c, "unknown_op")
    assert "frame" not in sink.kinds() and hub.registry.pin(G.WS_A) is not None


def test_stream_data_as_a_reply_is_unknown_op(mk):
    sink = Sink()
    c, _ = live(mk(sink), sink)
    c.send_raw(c.build("stream_data", data_body(), re=1))
    refused(c, "unknown_op")
    assert "frame" not in sink.kinds()


def test_stream_bad_body_closes_link_without_sink(mk):
    sink = Sink()
    c, _ = live(mk(sink), sink)
    c.send_raw(c.build("stream_data", data_body() | {"bytes": ""}))
    refused(c, "malformed")
    assert "frame" not in sink.kinds()


def test_stream_frames_without_a_sink_are_dropped_silently(mk):
    hub = mk(None)
    joined(hub).close()
    c = hub.open()
    c.send_raw(c.build("stream_data", data_body()))
    ping(c)


# --- the signature form (F1) and replay (R1) --------------------------------------------------------------------------


def test_stream_old_signature_form_refused(mk):
    sink = Sink()
    c, _ = live(mk(sink), sink)
    o = c.build("stream_data", data_body())["o"]
    c.send_raw({"o": o, "sig": b64u(S.sign(G.K.priv["wsk_a"], L["sig_hub_link"] + cj(o)))})  # pre-R1: no exporter
    refused(c, "bad_signature")
    assert "frame" not in sink.kinds()


def test_stream_frame_signed_for_another_connection_refused(mk):
    sink = Sink()
    c, _ = live(mk(sink), sink)
    o = c.build("stream_data", data_body())["o"]
    other = {"o": o, "sig": b64u(S.sign(G.K.priv["wsk_a"], L["sig_hub_link"] + os.urandom(32) + cj(o)))}
    c.send_raw(other)
    refused(c, "bad_signature")
    assert "frame" not in sink.kinds()


def test_stream_replayed_w2h_frame_closes_link(mk):
    sink = Sink()
    hub = mk(sink)
    c, _ = live(hub, sink)
    f = c.build("stream_data", data_body())
    c.send_raw(f)
    sink.wait("frame")
    c.send_raw(f)  # same seq again
    refused(c, "malformed")
    assert len(sink.wait("frame")) == 1
    sink.wait("detach")  # the link is gone, its streams with it


def test_stream_frame_replayed_on_a_new_connection_refused(mk):
    sink = Sink()
    hub = mk(sink)
    c, _ = live(hub, sink)
    f = c.build("stream_data", data_body())
    c.send_raw(f)
    sink.wait("frame")
    b = hub.open()
    b.send_raw(f)
    refused(b, "bad_signature")
    assert len(sink.wait("frame")) == 1


def test_stream_seq_skip_closes_link(mk):
    sink = Sink()
    c, _ = live(mk(sink), sink)
    c.send_raw(c.build("stream_data", data_body(), seq=5))
    refused(c, "malformed")
    assert "frame" not in sink.kinds()


# --- attach / detach and sink faults ----------------------------------------------------------------------------------


def test_stream_attach_once_per_link_and_detach_at_end(mk):
    sink = Sink()
    hub = mk(sink)
    c, port = live(hub, sink)
    c.send_raw(c.build("stream_close", {"stream_id": STREAM, "reason": "closed"}))
    c.send_raw(c.build("stream_close", {"stream_id": STREAM, "reason": "closed"}))
    sink.wait("frame", 2)
    assert sink.kinds().count("attach") == 1  # the stream link attached once; the plain join link never did
    c.close()
    ev = sink.wait("detach")
    assert port in [e[2] for e in ev]


def test_stream_sink_fault_does_not_kill_the_link(mk):
    sink = Sink()
    sink.raise_on_frame = True
    c, _ = live(mk(sink), sink)
    c.send_raw(c.build("stream_data", data_body()))
    ping(c)
    assert sink.kinds().count("frame") == 1


# --- ALPN dispatch and the remote pools -------------------------------------------------------------------------------


def _dial(hub: Hub, alpn=(tls.REMOTE_ALPN,)):
    return tls.connect_pinned("127.0.0.1", hub.port, hub.identity.spki_sha256, timeout=5, alpn=alpn)


def test_stream_alpn_remote_never_reaches_link_auth(mk):
    ep = Endpoint()
    hub = mk(Sink(), ep)
    conn = _dial(hub)
    try:
        assert conn.alpn() == b"orch-remote/1"
        # the endpoint's marker is the first byte; a link would have sent a framed challenge (first byte 0)
        assert conn.recv_exact(1, time.monotonic() + 5) == b"R"
        ep.wait()
        assert ep.alpns == [b"orch-remote/1"]
    finally:
        conn.close()


def test_stream_no_alpn_is_link(mk):
    ep = Endpoint()
    hub = mk(Sink(), ep)
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    assert ep.conns == []


def test_stream_unknown_alpn_is_link(mk):
    ep = Endpoint()
    hub = mk(Sink(), ep)
    conn = _dial(hub, alpn=(b"h2",))
    try:
        assert conn.alpn() == b""
        ch = framing.read_frame(conn, first_timeout=5, rest_timeout=5)
        assert ch["t"] == "challenge"
    finally:
        conn.close()
    assert ep.conns == []


def test_stream_alpn_without_endpoint_is_link(mk):
    hub = mk(Sink(), None)
    conn = _dial(hub)
    try:
        assert conn.alpn() == b""  # not offered by the server
        assert framing.read_frame(conn, first_timeout=5, rest_timeout=5)["t"] == "challenge"
    finally:
        conn.close()


def test_stream_endpoint_fault_closes_only_that_connection(mk):
    ep = Endpoint(boom=True)
    hub = mk(Sink(), ep)
    conn = _dial(hub)
    try:
        assert conn.recv_exact(1, time.monotonic() + 5) == b"R"
        with pytest.raises(tls.LinkClosed):
            conn.recv_exact(1, time.monotonic() + 5)
    finally:
        conn.close()
    joined(hub)  # the link still works


def test_stream_remote_connections_have_their_own_pools(mk):
    """Two link slots in total (a remote handshake needs one for an instant, before the ALPN shows), and remote
    clients holding their connections never keep one: a host link can always take a slot."""
    ep = Endpoint(hold=True)
    hub = mk(Sink(), ep, max_connections=2, max_unauthenticated=2, max_remote_connections=2)
    r1 = _dial(hub)
    try:
        ep.wait()
        c = hub.open(first=True)  # r1 is held: it keeps no link slot
        c.request("join", hub.join_body())
        r2 = _dial(hub)
        try:
            ep.wait(2)
            r3 = _dial(hub)  # the third remote connection: pool of 2 is full
            try:
                with pytest.raises(tls.LinkClosed):
                    r3.recv_exact(1, time.monotonic() + 5)
            finally:
                r3.close()
        finally:
            r2.close()
    finally:
        ep.release.set()
        r1.close()


def test_stream_remote_unsigned_pool_is_separate_and_freed_by_signed_in(mk):
    ep = Endpoint(hold=True)
    hub = mk(Sink(), ep, max_remote_unauthenticated=1, max_remote_connections=4, remote_signin_timeout=60)
    r1 = _dial(hub)
    try:
        ep.wait()
        r2 = _dial(hub)  # the one "not yet signed in" slot is held by r1
        try:
            with pytest.raises(tls.LinkClosed):
                r2.recv_exact(1, time.monotonic() + 5)
        finally:
            r2.close()
        ep.conns[0].signed_in()
        ep.conns[0].signed_in()  # idempotent
        r3 = _dial(hub)
        try:
            ep.wait(2)
            assert r3.recv_exact(1, time.monotonic() + 5) == b"R"
        finally:
            r3.close()
    finally:
        ep.release.set()
        r1.close()


def test_stream_remote_unsigned_closed_at_deadline_and_slot_freed(mk):
    ep = Endpoint(hold=True)
    hub = mk(Sink(), ep, max_remote_unauthenticated=1, remote_signin_timeout=0.3)
    r1 = _dial(hub)
    try:
        ep.wait()
        assert r1.recv_exact(1, time.monotonic() + 5) == b"R"
        with pytest.raises(tls.LinkClosed):  # fail closed: the deadline closes the connection itself
            r1.recv_exact(1, time.monotonic() + 5)
        r2 = _dial(hub)
        try:
            ep.wait(2)
            assert r2.recv_exact(1, time.monotonic() + 5) == b"R"
        finally:
            r2.close()
    finally:
        ep.release.set()
        r1.close()


# --- the dedicated stream link (ALPN orch-link-streams/1; protocol 10.2c) ----------------------------------------------


def stream_link(hub: Hub):
    return hub.open(alpn=(tls.STREAM_LINK_ALPN,))


def test_stream_alpn_port_attaches_only_to_streams(mk):
    sink = Sink()
    hub = mk(sink)
    joined(hub).close()
    c = stream_link(hub)
    (ev,) = sink.wait("attach")
    port = ev[2]
    assert port.stream_link and port.streams_attached and port.live is None and port.delivery is None
    ping(c)
    assert sink.kinds() == ["attach"]


def test_plain_port_never_gets_stream_open(mk):
    sink = Sink()
    hub = mk(sink)
    joined(hub).close()
    c = hub.open()
    for _ in range(3):
        ping(c)
    c.send_raw(c.build("stream_data", data_body()))  # a stream frame on a plain link is dropped, not forwarded
    ping(c)
    assert sink.kinds() == []  # never attached: the router cannot route a stream_open to it


def test_short_plain_link_does_not_detach_stream_port(mk):
    """G1b: any host link used to steal the stream route (newest attach replaces) and its end detached the pump's."""
    sink = Sink()
    hub = mk(sink)
    joined(hub).close()
    c = stream_link(hub)
    (ev,) = sink.wait("attach")
    for _ in range(3):
        short = hub.open()
        assert "snapshot" in short.request("snapshot_get", {})
        short.close()
    ping(c)
    time.sleep(0.2)
    assert sink.kinds() == ["attach"]  # no detach, no second attach
    c.send_raw(c.build("stream_data", data_body()))
    assert len(sink.wait("frame")) == 1 and sink.wait("frame")[0][2] is ev[2]


@pytest.mark.parametrize("op", ["snapshot_get"])
def test_stream_port_refuses_other_ops(mk, op):
    sink = Sink()
    hub = mk(sink)
    joined(hub).close()
    c = stream_link(hub)
    body = {
        "snapshot_get": {},
    }[op]
    c.send_raw(c.build(op, body))
    refused(c, "unknown_op")
    sink.wait("detach")  # the link is over, its streams with it


def test_stream_port_allows_hub_where(mk):
    sink = Sink()
    hub = mk(sink, where=lambda: {"addresses": [], "name": "hub"})
    joined(hub).close()
    c = stream_link(hub)
    sink.wait("attach")
    for _ in range(2):  # the keepalive repeats on the same link
        c.send_raw(c.build("hub_where", {}))
        assert c.recv_reply()["op"] == "hub_where"
    assert "detach" not in sink.kinds()


def test_stream_port_unpinned_refused(mk):
    sink = Sink()
    hub = mk(sink)
    c = hub.open(first=True, alpn=(tls.STREAM_LINK_ALPN,))  # nothing joined: no join on a stream link
    refused(c, "not_bound")
    assert sink.kinds() == [] and hub.registry.pin(G.WS_A) is None


def test_stream_alpn_without_sink_is_not_offered(mk):
    hub = mk(None)
    conn = _dial(hub, alpn=(tls.STREAM_LINK_ALPN,))
    try:
        assert conn.alpn() == b""  # the host sees no negotiated protocol and must not use the link as a stream link
    finally:
        conn.close()


def test_new_stream_port_replaces_old_closes_streams(mk):
    from orch_hub.remote import StreamRouter

    router = StreamRouter(hub_id=G.HUB_ID, allowed=lambda *a: True, count_op=lambda *a: None).start()
    try:
        hub = mk(router)
        joined(hub).close()
        c1 = stream_link(hub)
        deadline = time.monotonic() + 5
        while G.WS_A not in router._ports and time.monotonic() < deadline:
            time.sleep(0.02)
        first = router._ports[G.WS_A].port
        c2 = stream_link(hub)
        while router._ports.get(G.WS_A) is None or router._ports[G.WS_A].port is first:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        second = router._ports[G.WS_A].port
        c1.close()  # the replaced link ends: it must not detach its successor
        time.sleep(0.3)
        assert router._ports[G.WS_A].port is second
        ping(c2)
    finally:
        router.close_all()

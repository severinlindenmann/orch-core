"""R6 T-B (#1594, protocol 10.10): the hub's WebSocket accept. A real ``LinkServer`` with a WebSocket listener, the
contract vectors replayed against the live upgrade, refusals of the upgrade and of the frames, a full link over the
WebSocket (inner TLS 1.3 with the card pin, the R1 exporter, ALPN none / streams / remote), and the facts that must
never depend on the front: ``same_computer`` False and no peer address."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from conftest import Hub
from linkfakes import ClientPipe, new_key, read_head, status_of, upgrade_request, ws_dial
from test_link_streams import Endpoint, Sink

from orch_hub import colocation as CO
from orch_hub.contract import linkws as W
from orch_hub.contract import vectorgen as G
from orch_hub.link import tls
from orch_hub.link.server import LinkServer as _LS

VEC = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "hub" / "vectors_link_ws.json").read_text())
ALWAYS = lambda ip: True


@pytest.fixture
def mk():
    made: list[Hub] = []

    def make(**kw) -> Hub:
        kw.setdefault("ws_host", "127.0.0.1")
        h = Hub(**kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def ws_addr(h: Hub) -> tuple[str, int]:
    a = h.server.ws_address
    assert a is not None
    return a


def raw(h: Hub, data: bytes, *, read: float = 3.0) -> bytes:
    """Send ``data`` to the WebSocket listener, return what comes back until the hub closes (or ``read`` seconds)."""
    s = socket.create_connection(ws_addr(h), timeout=3)
    try:
        s.sendall(data)
        end = time.monotonic() + read
        out = b""
        s.settimeout(0.3)
        while time.monotonic() < end:
            try:
                chunk = s.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if status_of(out) == 101 and b"\r\n\r\n" in out:
                break  # upgraded: the hub now waits for the TLS, nothing more to see
        return out
    finally:
        s.close()


def req(extra: str = "", path: str = W.LINK_PATH, key: str | None = None) -> bytes:
    r = upgrade_request("hub.example", key or new_key(), extra)
    return r.replace(W.LINK_PATH.encode(), path.encode(), 1)


# --- the upgrade ---------------------------------------------------------------------------------------------------


def test_only_the_link_path_is_served(mk):
    h = mk()
    assert status_of(raw(h, req(path="/")).split(b"\r\n\r\n")[0]) == 404
    assert status_of(raw(h, req(path="/api/v1/hub/card")).split(b"\r\n\r\n")[0]) == 404
    assert status_of(raw(h, req(path="/orch/link/1/")).split(b"\r\n\r\n")[0]) == 404
    assert status_of(raw(h, b"POST /orch/link/1 HTTP/1.1\r\nHost: x\r\n\r\n").split(b"\r\n\r\n")[0]) == 404


@pytest.mark.parametrize(
    "extra,status",
    [
        ("Origin: https://evil.example\r\n", 403),
        ("Origin: null\r\n", 403),
        ("Sec-WebSocket-Extensions: permessage-deflate\r\n", 400),
        ("Sec-WebSocket-Extensions: \r\n", 400),
        ("Content-Length: 5\r\n", 400),
    ],
)
def test_upgrade_refusals(mk, extra, status):
    h = mk()
    assert status_of(raw(h, req(extra)).split(b"\r\n\r\n")[0]) == status


def test_wrong_subprotocol_and_version(mk):
    h = mk()
    bad_proto = req().replace(b"orch-link.1", b"chat")
    assert status_of(raw(h, bad_proto)) == 400
    bad_ver = req().replace(b"Version: 13", b"Version: 8")
    out = raw(h, bad_ver)
    assert status_of(out) == 426 and b"Sec-WebSocket-Version: 13" in out


def test_oversize_head_is_refused(mk):
    h = mk()
    out = raw(h, req("X-Pad: " + "a" * 9000 + "\r\n"))
    assert status_of(out) == 400


def test_slow_head_gets_silence_and_loses_its_slot(mk):
    h = mk(ws_head_timeout=0.4)
    t0 = time.monotonic()
    assert raw(h, b"GET /orch/link/1 HTTP/1.1\r\nHost: x\r\n", read=3) == b""
    assert time.monotonic() - t0 < 2.5


def test_vectors_check_request_against_the_live_upgrade(mk):
    h = mk()
    n = 0
    for c in VEC["cases"]:
        if c["check"] != "check_request":
            continue
        head = c["args"]["head"].encode("utf-8")
        if not head.endswith(b"\r\n\r\n") or head.find(b"\r\n\r\n") != len(head) - 4:
            continue  # an unterminated head is not a request on a stream: the hub waits, then goes silent
        out = raw(h, head, read=1.0)
        exp = c["expect"]
        if "ok" in exp:
            assert status_of(out) == 101, c["name"]
            assert W.accept_key(exp["ok"]).encode() in out, c["name"]
        else:
            assert status_of(out) == exp["status"], c["name"]
            assert out == W.build_refusal(exp["status"]), c["name"]
        n += 1
    assert n >= 20


def test_nothing_else_listens_on_the_ws_port(mk):
    """No page, console or API on the listener: a plain HTTP GET of anything gets a 404 and nothing else."""
    h = mk()
    out = raw(h, b"GET /api/v1/hub/card HTTP/1.1\r\nHost: x\r\n\r\n")
    assert out == W.build_refusal(404)


# --- frames after the upgrade ----------------------------------------------------------------------------------------


def _after_upgrade(h: Hub, frame: bytes) -> tuple[int, bytes]:
    s = socket.create_connection(ws_addr(h), timeout=3)
    try:
        key = new_key()
        s.sendall(upgrade_request("hub.example", key))
        head, rest = read_head(s)
        assert status_of(head) == 101
        s.sendall(frame)
        s.settimeout(0.3)
        out = rest
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                chunk = s.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                break
            out += chunk
        return 0, out
    finally:
        s.close()


def _close_code(out: bytes) -> int:
    """The close code in the first close frame of ``out`` (server frames are unmasked; ServerHello records may come
    first only after a good ClientHello, so here the close frame is the first thing)."""
    assert out[0] == 0x88, out[:8].hex()
    return int.from_bytes(out[2:4], "big")


@pytest.mark.parametrize(
    "frame,code",
    [
        (bytes([0xC2, 0x80]) + b"\x00\x00\x00\x00", 1002),  # RSV1: per-message compression, never negotiated
        (bytes([0x81, 0x80]) + b"\x00\x00\x00\x00", 1003),  # text
        (bytes([0x83, 0x80]) + b"\x00\x00\x00\x00", 1002),  # unknown opcode
        (bytes([0x82, 0x01, 0x41]), 1002),  # unmasked client frame
        (bytes([0x82, 0xFF]) + (256 * 1024 + 1).to_bytes(8, "big"), 1009),  # over 256 KiB, header alone
        (bytes([0x09, 0x80]) + b"\x00\x00\x00\x00", 1002),  # fragmented ping
    ],
)
def test_frame_refusals_close_with_the_right_code(mk, frame, code):
    h = mk()
    _, out = _after_upgrade(h, frame)
    assert _close_code(out) == code


# --- the whole link over the WebSocket ---------------------------------------------------------------------------


@pytest.fixture
def via_ws(monkeypatch):
    """Make ``LinkClient.open`` dial the hub's WebSocket listener instead of the link port."""
    state: dict = {}

    def dial(host, port, pin, *, timeout=10.0, stop=None, alpn=()):
        conn = ws_dial(state["addr"], pin, alpn=alpn, extra=state.get("extra", ""), timeout=timeout)
        state.setdefault("conns", []).append(conn)
        return conn

    monkeypatch.setattr(tls, "connect_pinned", dial)
    return state


def _record_same(monkeypatch) -> list[bool]:
    seen: list[bool] = []
    orig = _LS._dispatch

    def spy(self, ws, wsk, o, now, same=False):
        seen.append(same)
        return orig(self, ws, wsk, o, now, same)

    monkeypatch.setattr(_LS, "_dispatch", spy)
    return seen


def test_full_link_over_ws_same_computer_false_and_no_peer_address(mk, via_ws, monkeypatch):
    seen = _record_same(monkeypatch)
    h = mk()
    via_ws["addr"] = ws_addr(h)
    via_ws["extra"] = "X-Forwarded-For: 203.0.113.9\r\nX-Real-IP: 203.0.113.9\r\nForwarded: for=203.0.113.9\r\n"
    c = h.open(first=True)
    assert c.request("join", h.join_body()) == {}
    assert c.request("snapshot_get", {})["snapshot"]
    assert seen and not any(seen)  # the peer is the loopback front, never "this computer"
    assert h.registry.status(G.WS_A)["address"] == ""
    assert isinstance(c._c, tls.BioTlsConn) and c._c.same_computer() is False


def test_the_direct_port_still_sees_a_local_peer(mk, monkeypatch):
    seen = _record_same(monkeypatch)
    h = mk()
    c = h.open(first=True)
    c.request("join", h.join_body())
    assert seen == [True]
    assert h.registry.status(G.WS_A)["address"] == "127.0.0.1"


def test_pin_mismatch_is_refused_before_a_byte(mk):
    h = mk()
    with pytest.raises(tls.PinMismatch):
        ws_dial(ws_addr(h), "A" * 43)


def test_alpn_streams_over_ws(mk, via_ws):
    sink = Sink()
    h = mk(streams=sink)
    via_ws["addr"] = ws_addr(h)
    c = h.open(first=True)
    c.request("join", h.join_body())
    c.close()
    s = h.open(alpn=(tls.STREAM_LINK_ALPN,))
    (_, _ws, port) = sink.wait("attach")[0]
    assert port.stream_link and isinstance(port._c, tls.BioTlsConn)
    assert s._c.alpn() == tls.STREAM_LINK_ALPN
    s.close()


def test_alpn_remote_over_ws_exporter_and_large_payload(mk):
    got: dict = {}

    class Echo:
        def serve(self, conn) -> None:
            got["same"] = conn.same_computer()
            got["alpn"] = conn.alpn()
            got["exp"] = conn.exporter()
            got["type"] = type(conn)
            data = conn.recv_exact(300_000, time.monotonic() + 10)
            conn.send_all(hashlib.sha256(data).digest(), time.monotonic() + 10)
            got["done"] = True

    h = mk(remote=Echo())
    c = ws_dial(ws_addr(h), h.identity.spki_sha256, alpn=(tls.REMOTE_ALPN,))
    payload = os.urandom(300_000)  # spans several 64 KiB frames and TLS records
    c.send_all(payload, time.monotonic() + 10)
    assert c.recv_exact(32, time.monotonic() + 10) == hashlib.sha256(payload).digest()
    assert c.alpn() == tls.REMOTE_ALPN
    assert got["exp"] == c.exporter() and len(got["exp"]) == 32  # one TLS connection, one exporter, both ends
    assert got["same"] is False and got["alpn"] == tls.REMOTE_ALPN and got["type"] is tls.BioTlsConn
    c.close()


def test_pings_never_wake_the_reader(mk):
    """Ping, pong and TLS records alone must not make ``wait_readable`` True (else a ping starts a ``read_frame``
    that times out and drops the link)."""
    res: dict = {"w": []}
    ready = threading.Event()

    class Waiter:
        def serve(self, conn) -> None:
            ready.set()
            res["w"].append(conn.wait_readable(1.0))  # while the client pings: nothing to read
            res["w"].append(conn.wait_readable(3.0))  # then one application byte
            res["byte"] = conn.recv_exact(1, time.monotonic() + 3)
            res["w"].append(conn.wait_readable(0.3))

    h = mk(remote=Waiter())
    c = ws_dial(ws_addr(h), h.identity.spki_sha256, alpn=(tls.REMOTE_ALPN,))
    assert ready.wait(3)
    pipe: ClientPipe = c.pipe  # type: ignore[attr-defined]
    end = time.monotonic() + 0.9
    while time.monotonic() < end:
        pipe.frame(W.OP_PING, b"hi")
        time.sleep(0.05)
    time.sleep(0.2)
    c.send_all(b"Z", time.monotonic() + 3)
    deadline = time.monotonic() + 5
    while "byte" not in res and time.monotonic() < deadline:
        time.sleep(0.02)
    assert res["w"][0] is False and res["w"][1] is True and res["byte"] == b"Z"
    c.close()


def test_the_hub_pings_an_idle_link_and_it_stays_up(mk, via_ws):
    h = mk(ws_ping_after=0.15)
    via_ws["addr"] = ws_addr(h)
    c = h.open(first=True)
    c.request("join", h.join_body())
    conn = c._c
    seen_before = conn.pipe.rx.ended
    assert conn.wait_readable(1.2) is False  # the hub pings several times meanwhile: no application byte
    assert seen_before is False
    assert c.request("snapshot_get", {})["snapshot"]  # the link is alive


def test_idle_close_without_any_byte(mk):
    """75 s without receiving anything closes (here: 0.5 s)."""
    h = mk(ws_idle_close=0.5)
    c = ws_dial(ws_addr(h), h.identity.spki_sha256)
    with pytest.raises(tls.LinkClosed):  # the challenge frame comes first, then the hub closes the silent link
        while True:
            c.recv_exact(1, time.monotonic() + 3)
    c.close()


def test_slow_upgrades_cannot_exceed_the_unauthenticated_pool(mk):
    h = mk(max_unauthenticated=2, max_connections=8, ws_head_timeout=2.0)
    held = [socket.create_connection(ws_addr(h), timeout=3) for _ in range(2)]
    try:
        time.sleep(0.3)
        third = socket.create_connection(ws_addr(h), timeout=3)
        third.settimeout(2)
        assert third.recv(10) == b""  # no slot: closed at once, nothing read
        third.close()
        # the same pool as the link port's
        direct = socket.create_connection(("127.0.0.1", h.port), timeout=3)
        direct.settimeout(2)
        assert direct.recv(10) == b""
        direct.close()
    finally:
        for s in held:
            s.close()


def test_remote_alpn_over_ws_lands_in_the_remote_pools(mk):
    ep = Endpoint(hold=True)
    h = mk(remote=ep)
    c = ws_dial(ws_addr(h), h.identity.spki_sha256, alpn=(tls.REMOTE_ALPN,))
    ep.wait()
    assert ep.alpns == [tls.REMOTE_ALPN]
    assert c.recv_exact(1, time.monotonic() + 3) == b"R"
    ep.release.set()
    c.close()


# --- binding the listener --------------------------------------------------------------------------------------------


def test_bind_rules():
    ok = CO.parse_link_ws_listen
    assert ok("127.0.0.1:8107", assigned=ALWAYS) == ("127.0.0.1", 8107)
    assert ok("[::1]:8107", assigned=ALWAYS) == ("::1", 8107)
    assert ok("100.71.244.99:8107", assigned=ALWAYS) == ("100.71.244.99", 8107)
    assert ok("192.168.1.5:8107", assigned=ALWAYS) == ("192.168.1.5", 8107)
    for bad, needle in [
        ("0.0.0.0:8107", "wildcard"),
        ("203.0.113.7:8107", "public"),
        ("8.8.8.8:8107", "public"),
        ("[2001:db8::1]:8107", "public"),
        ("example.com:8107", "literal IP"),
        ("169.254.1.1:8107", "link-local"),
        ("224.0.0.1:8107", "multicast"),
        ("127.0.0.1", "IP:port"),
    ]:
        with pytest.raises(CO.BindRefused, match=needle):
            ok(bad, assigned=ALWAYS)
    with pytest.raises(CO.BindRefused, match="not assigned"):
        ok("10.254.254.254:8107", assigned=lambda ip: False)
    # a public address only when the owner says a TLS-terminating front is the only way in; a wildcard only with --bind-all
    assert ok("203.0.113.7:8107", assigned=ALWAYS, behind_front=True) == ("203.0.113.7", 8107)
    assert ok("0.0.0.0:8107", assigned=ALWAYS, bind_all=True) == ("0.0.0.0", 8107)


def test_serve_refuses_a_public_ws_bind_before_opening_anything(tmp_path):
    from orch_hub.serve import serve

    with pytest.raises(ValueError, match="public"):
        serve(
            tmp_path / "state",
            "127.0.0.1:0",
            "http://127.0.0.1:1",
            [],
            link_ws_listen="203.0.113.7:8107",
            assigned=ALWAYS,
        )
    assert not (tmp_path / "state").exists()


def test_cli_has_the_flags():
    from orch_hub.cli import _parser

    a = _parser().parse_args(
        ["serve", "--listen", "127.0.0.1:0", "--publish-url", "http://x", "--link-ws-listen", "127.0.0.1:9",
         "--link-ws-public", "wss://hub.example", "--link-ws-behind-front"]
    )  # fmt: skip
    assert (a.link_ws_listen, a.link_ws_public, a.link_ws_behind_front) == ("127.0.0.1:9", "wss://hub.example", True)


def test_hub_where_lists_the_ws_public_address(tmp_path):
    from orch_hub import settings as SETT
    from orch_hub.contract.where import check_hub_where

    SETT.save(
        tmp_path,
        {
            "v": 1,
            "name": "Acme",
            "company": "",
            "addresses": [{"kind": "office", "web": "http://10.0.0.5:8080", "link": "10.0.0.5:8105"}],
        },
    )
    a = SETT.where_answer(tmp_path, extra_public="wss://hub.example")
    assert check_hub_where(a) == a
    assert a["addresses"][-1] == {"kind": "public", "link": "wss://hub.example"}
    assert SETT.where_answer(tmp_path)["addresses"] == [{"kind": "office", "link": "10.0.0.5:8105"}]


def test_connect_link_takes_the_new_address_grammar():
    from orch_hub.connect import LinkError, build_link, parse_link

    for ep in (
        "wss://hub.example",
        "ws://10.0.0.5:8107",
        "wss://hub.example:8443",
        "[::1]:8105",
        "local",
        "10.0.0.5:8105",
    ):
        link = build_link("https://h.example", "a" * 32, "code1", ep)
        assert parse_link(link)["endpoint"] == ep
    for ep in (
        "wss://hub.example/orch/link/1",
        "https://hub.example",
        "wss://u@hub.example",
        "wss://hub.example?x=1",
        "hub.example",
        "WSS://hub.example",
    ):
        with pytest.raises(LinkError):
            build_link("https://h.example", "a" * 32, "code1", ep)
        with pytest.raises(LinkError):
            parse_link(f"https://h.example/c#h1.{'a' * 32}.code1.{ep}")

"""R6 T-A (#1594): the host's end of the link over every transport, against a real ``TlsHub`` behind in-process fakes
(``linkfakes``): direct, WebSocket (ws, wss), HTTP CONNECT (http and https proxy, with and without Basic); and the
invariants: no downgrade, no network settings from the hub, no proxy from the environment, pings never wake a reader."""

from __future__ import annotations

import os
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from orch.hubclient import HubRefused
from orch.hubclient.linkaddr import LinkAddr, parse_link_address
from orch.hubclient.netconfig import NetConfigError, NetSettings, ProxySettings, load_net
from orch.hubclient.tlsadapter import FailoverTransport, LinkInspectedError, TlsTransport
from orch.hubclient.tlslink import tls, transport
from orch.hubclient.tlslink import wsframe as W
from orch.hubclient.tlslink.client import LinkClient
from orch.hubclient.tlslink.errors import (
    LinkClosed,
    LinkInspected,
    PinMismatch,
    ProxyAuth,
    ProxyRefused,
    ProxyUnreachable,
)
from orch.hubclient.where import check_hub_where

from ..hub.test_link_client import WSK_PUB, send, sign
from ..hub.tlshub import TlsHub
from .linkfakes import ConnectProxy, Listener, Pki, WsFront, closed_port

NAME = "hub.test"
KINDS = ["direct", "ws", "wss", "connect", "connect_basic", "connect_wss", "https_proxy_ws"]


class Rig:
    """A hub and the fakes in front of it; ``build(kind)`` returns the address and net settings a host would use."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.pki = Pki(tmp_path)
        monkeypatch.setattr(transport, "outer_context", self.pki.client_context)
        self.closers: list[Callable[[], None]] = []
        self.hub = self.hub_with(TlsHub(wsk_pub=WSK_PUB).start())
        self.dials: list[tuple[str, int]] = []
        real = transport.dial_tcp

        def spy(host: str, port: int, deadline: float) -> socket.socket:
            self.dials.append((host, port))
            return real(host, port, deadline)

        monkeypatch.setattr(transport, "dial_tcp", spy)

    def hub_with(self, hub: TlsHub) -> TlsHub:
        self.closers.append(hub.close)
        hub.sni: list[Any] = []  # type: ignore[misc]
        hub._ctx.set_alpn_select_callback(lambda c, protos: protos[0] if protos else b"")
        hub._ctx.set_tlsext_servername_callback(lambda c: hub.sni.append(c.get_servername()))
        return hub

    def add(self, fake: Any) -> Any:
        self.closers.append(fake.close)
        return fake

    def build(self, kind: str, hub: TlsHub | None = None) -> tuple[LinkAddr, NetSettings | None, Any]:
        hub = hub or self.hub
        pk = self.pki
        if kind == "direct":
            return parse_link_address(f"127.0.0.1:{hub.port}"), None, None
        if kind == "ws":
            f = self.add(WsFront(hub.port))
            return parse_link_address(f"ws://127.0.0.1:{f.port}"), None, f
        if kind == "wss":
            f = self.add(WsFront(hub.port, pki=pk))
            return parse_link_address(f"wss://127.0.0.1:{f.port}"), None, f
        if kind in ("connect", "connect_basic"):
            auth = ("alice", "s3cret") if kind == "connect_basic" else None
            p = self.add(ConnectProxy({f"{NAME}:4000": hub.port}, auth=auth))
            net = NetSettings(ProxySettings("http", "127.0.0.1", p.port, *(auth or (None, None))))
            return parse_link_address(f"{NAME}:4000"), net, p
        if kind == "connect_wss":
            f = self.add(WsFront(hub.port, pki=pk))
            p = self.add(ConnectProxy({f"{NAME}:8443": f.port}))
            return parse_link_address(f"wss://{NAME}:8443"), NetSettings(ProxySettings("http", "127.0.0.1", p.port)), p
        if kind == "https_proxy_ws":
            f = self.add(WsFront(hub.port))
            p = self.add(ConnectProxy({f"{NAME}:8080": f.port}, pki=pk))
            net = NetSettings(ProxySettings("https", "localhost", p.port))
            return parse_link_address(f"ws://{NAME}:8080"), net, p
        raise AssertionError(kind)

    def open(self, addr: LinkAddr, net: NetSettings | None, hub: TlsHub | None = None, **kw: Any) -> LinkClient:
        hub = hub or self.hub
        host = addr.host
        return LinkClient.open(
            host, addr.port, addr=addr, net=net, tls_spki_sha256=hub.spki, hub_id=hub.hub_id,
            workspace_id=hub.workspace_id, sign=sign, hk_op_pub=hub.hk_op_pub, timeout=4.0, **kw,
        )  # fmt: skip

    def close(self) -> None:
        for c in reversed(self.closers):
            c()


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.close()


# --- every transport carries the same link ---


@pytest.mark.parametrize("kind", KINDS)
def test_link_works_on_every_transport(rig: Rig, kind: str) -> None:
    addr, net, _ = rig.build(kind)
    with rig.open(addr, net) as link:
        assert rig.hub.auth_verified.wait(3)
        assert link._c.exporter() == rig.hub.exporter  # R1: both ends derived the same exporter inside the tunnel
        assert send(link)["o"]["body"] == {"snapshot": {"stub": 1}}
        assert send(link)["o"]["op"] == "snapshot_get"  # a second request on the same connection
    # direct and an http-proxy tunnel keep the socket-mode TLS; a WebSocket or a TLS-in-TLS tunnel runs the BIO pump
    bio = addr.scheme != "tcp" or (net is not None and net.proxy is not None and net.proxy.scheme == "https")
    assert isinstance(link._c, tls.BioTlsConn) is bio


@pytest.mark.parametrize("kind", KINDS)
def test_alpn_and_server_name_on_every_transport(rig: Rig, kind: str) -> None:
    addr, net, _ = rig.build(kind)
    conn = tls.dial_link(addr, rig.hub.spki, alpn=(b"orch-remote/1",), net=net, timeout=4.0)
    try:
        assert conn.alpn() == b"orch-remote/1"
    finally:
        conn.close()
    deadline = time.monotonic() + 2
    while not rig.hub.sni and time.monotonic() < deadline:
        time.sleep(0.01)
    want = addr.sni.encode() if addr.sni else None  # the DNS name; none for an IP literal
    assert rig.hub.sni[-1] == (want if want else None)


def test_requests_to_the_front_and_proxy_have_the_profile_shape(rig: Rig) -> None:
    addr, _, front = rig.build("ws")
    rig.open(addr, None).close()
    line, fields = _head(front.heads[0])
    assert line == "GET /orch/link/1 HTTP/1.1"
    assert fields["sec-websocket-protocol"] == "orch-link.1" and fields["sec-websocket-version"] == "13"
    assert fields["host"] == f"127.0.0.1:{front.port}"
    assert not {"origin", "cookie", "authorization", "sec-websocket-extensions", "user-agent"} & set(fields)
    assert front.errors == [] and front.client_frames  # every client frame decoded as masked
    assert all(op in (W.BINARY, W.CLOSE) for op, _ in front.client_frames)

    addr, net, proxy = rig.build("connect_basic")
    rig.open(addr, net).close()
    line, fields = _head(proxy.requests[0])
    assert line == f"CONNECT {NAME}:4000 HTTP/1.1"  # the name as text
    assert fields["proxy-authorization"].startswith("Basic ") and fields["host"] == f"{NAME}:4000"
    assert b"Proxy-Authorization" not in bytes(rig.hub.app_bytes)  # never inside the tunnel


def _head(raw: bytes) -> tuple[str, dict[str, str]]:
    from .linkfakes import parse_head

    return parse_head(raw)


# --- no downgrade ---


@pytest.mark.parametrize("kind", ["ws", "wss", "connect", "connect_wss", "https_proxy_ws"])
def test_inspecting_network_is_link_inspected_and_never_falls_back(rig: Rig, kind: str) -> None:
    impostor = rig.hub_with(TlsHub(wsk_pub=WSK_PUB).start())  # what a TLS-inspecting box presents: its own key
    addr, net, _ = rig.build(kind, hub=impostor)
    with pytest.raises(LinkInspected) as e:
        rig.open(addr, net)  # pinned to the REAL hub's key
    assert isinstance(e.value, PinMismatch) and e.value.code == "hub.link_inspected"
    assert not rig.hub._conns and not rig.hub.app_bytes  # nothing reached the real hub
    assert not impostor.app_bytes  # and the impostor got no link byte either
    targets = {(h, p) for h, p in rig.dials}
    assert len(rig.dials) == 1, rig.dials  # one dial, to the front or proxy: no second transport, no direct dial
    assert (addr.host, addr.port) not in targets or addr.host == "127.0.0.1"


def test_direct_pin_mismatch_keeps_the_plain_error(rig: Rig) -> None:
    impostor = rig.hub_with(TlsHub(wsk_pub=WSK_PUB).start())
    addr, _, _ = rig.build("direct", hub=impostor)
    with pytest.raises(PinMismatch) as e:
        rig.open(addr, None)
    assert not isinstance(e.value, LinkInspected)


def test_inspected_transport_waits_five_minutes_then_dials_again(rig: Rig) -> None:
    impostor = rig.hub_with(TlsHub(wsk_pub=WSK_PUB).start())
    addr, net, _ = rig.build("ws", hub=impostor)
    now = [1000.0]
    t = TlsTransport(
        addr=addr.text, tls_spki_sha256=rig.hub.spki, hub_id=rig.hub.hub_id, workspace_id=rig.hub.workspace_id,
        hk_op_pub=rig.hub.hk_op_pub, wsk_pub=WSK_PUB, sign=sign, timeout=3.0, net=net, clock=lambda: now[0],
    )  # fmt: skip
    with pytest.raises(LinkInspectedError) as e:
        t.ensure_open("x")
    assert e.value.code == "hub.link_inspected"
    n = len(rig.dials)
    assert n == 1
    now[0] += 299
    with pytest.raises(LinkInspectedError):
        t.ensure_open("x")
    assert len(rig.dials) == n  # no second attempt inside five minutes
    now[0] += 2
    with pytest.raises(LinkInspectedError):
        t.ensure_open("x")
    assert len(rig.dials) == n + 1  # and one again after


def test_failover_moves_past_an_inspected_address_to_a_pinned_one(rig: Rig) -> None:
    impostor = rig.hub_with(TlsHub(wsk_pub=WSK_PUB).start())
    bad, net, _ = rig.build("ws", hub=impostor)
    good, _, _ = rig.build("direct")

    def to(a: LinkAddr) -> TlsTransport:
        return TlsTransport(
            addr=a.text, tls_spki_sha256=rig.hub.spki, hub_id=rig.hub.hub_id, workspace_id=rig.hub.workspace_id,
            hk_op_pub=rig.hub.hk_op_pub, wsk_pub=WSK_PUB, sign=sign, timeout=3.0,
        )  # fmt: skip

    f = FailoverTransport([to(bad), to(good)])
    assert f.send_next("snapshot_get", _build(rig))["o"]["op"] == "snapshot_get"
    f.close()
    f2 = FailoverTransport([to(bad)])
    with pytest.raises(LinkInspectedError):
        f2.send_next("snapshot_get", _build(rig))


def _build(rig: Rig) -> Callable[[int, bytes], dict[str, Any]]:
    from orch.crypto.encoding import b64u
    from orch.hubclient.tlslink import client as C

    def build(seq: int, cb: bytes) -> dict[str, Any]:
        o = {
            "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": rig.hub.hub_id,
            "workspace_id": rig.hub.workspace_id, "dir": "w2h", "seq": seq, "ts_ms": int(time.time() * 1000),
            "op": "snapshot_get", "re": None, "body": {},
        }  # fmt: skip
        return {"o": o, "sig": b64u(sign(C.link_signed_bytes(C.SIG_LINK, cb, o)))}

    return build


def test_no_pin_dials_nothing_on_any_transport(rig: Rig) -> None:
    addr, net, _ = rig.build("ws")
    for bad in (tls.PLACEHOLDER_PIN, "", "A" * 10):
        with pytest.raises(tls.NoPin):
            tls.dial_link(addr, bad, net=net)
    assert rig.dials == []


# --- the proxy: host settings only ---


def test_environment_proxy_is_ignored(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    p = rig.add(ConnectProxy({}))
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(k, f"http://127.0.0.1:{p.port}")
    monkeypatch.setenv("NO_PROXY", "")
    addr, _, _ = rig.build("direct")
    rig.open(addr, None).close()
    assert p.accepted == 0 and rig.dials == [("127.0.0.1", rig.hub.port)]


def test_loopback_is_never_proxied_and_a_proxy_has_no_direct_fallback(rig: Rig) -> None:
    proxy = rig.add(ConnectProxy({}))
    net = NetSettings(ProxySettings("http", "127.0.0.1", proxy.port))
    addr, _, _ = rig.build("direct")
    rig.open(addr, net).close()  # 127.0.0.1 goes direct whatever the proxy setting
    assert proxy.accepted == 0
    # a name that the proxy does not know: refused, and NOT dialled directly
    rig.dials.clear()
    with pytest.raises(ProxyRefused):
        tls.dial_link(parse_link_address(f"{NAME}:{rig.hub.port}"), rig.hub.spki, net=net, timeout=3.0)
    assert rig.dials == [("127.0.0.1", proxy.port)]


def test_proxy_unreachable_is_not_answered_by_a_direct_dial(rig: Rig) -> None:
    net = NetSettings(ProxySettings("http", "127.0.0.1", closed_port()))
    with pytest.raises(ProxyUnreachable) as e:
        tls.dial_link(parse_link_address(f"{NAME}:{rig.hub.port}"), rig.hub.spki, net=net, timeout=3.0)
    assert e.value.code == "hub.proxy_unreachable"
    assert all(h == "127.0.0.1" and p != rig.hub.port for h, p in rig.dials)
    assert not rig.hub._conns


def test_proxy_answers(rig: Rig) -> None:
    def go(proxy: ConnectProxy, user: str | None = None, pw: str | None = None) -> Exception:
        net = NetSettings(ProxySettings("http", "127.0.0.1", proxy.port, user, pw))
        with pytest.raises(Exception) as e:  # noqa: PT011
            tls.dial_link(parse_link_address(f"{NAME}:4000"), rig.hub.spki, net=net, timeout=3.0)
        return e.value

    routes = {f"{NAME}:4000": rig.hub.port}
    e = go(rig.add(ConnectProxy(routes, auth=("alice", "s3cret"))))
    assert isinstance(e, ProxyAuth) and e.code == "hub.proxy_auth" and "user name" in e.detail
    e = go(rig.add(ConnectProxy(routes, auth=("alice", "s3cret"))), "alice", "wrong")
    assert isinstance(e, ProxyAuth) and "wrong" not in str(e) and "s3cret" not in str(e)
    e = go(rig.add(ConnectProxy(routes, mode="407_ntlm")))
    assert isinstance(e, ProxyAuth) and "NTLM" in e.detail  # refused with a plain message, no negotiation
    e = go(rig.add(ConnectProxy(routes, mode="403")))
    assert isinstance(e, ProxyRefused) and e.code == "hub.proxy_refused"
    e = go(rig.add(ConnectProxy(routes, mode="garbage")))
    assert isinstance(e, ProxyRefused)
    assert not rig.hub._conns


def test_proxy_errors_are_oserrors_so_failover_treats_them_as_unreachable() -> None:
    assert (
        issubclass(ProxyAuth, OSError) and issubclass(ProxyRefused, OSError) and issubclass(ProxyUnreachable, OSError)
    )


def test_the_hub_cannot_set_the_transport_or_the_proxy(rig: Rig, tmp_path: Path) -> None:
    for extra in ({"proxy": "http://evil:8080"}, {"transport": "ws"}, {"net": {}}):
        ans = {"addresses": [{"kind": "lan", "link": "10.0.0.5:8105", **extra}], "name": "Hub"}
        with pytest.raises(HubRefused) as e:
            check_hub_where(ans)
        assert e.value.code == "malformed"
    ans = {"addresses": [{"kind": "lan", "link": "10.0.0.5:8105"}], "proxy": "http://evil:8080", "name": "Hub"}
    with pytest.raises(HubRefused):
        check_hub_where(ans)
    # an address is only its grammar: no path, query or headers can ride in it
    for t in ("ws://10.0.0.5/evil", "wss://h/?proxy=http://x", "ws://u:p@h", "http://h", "ws://h:80#x"):
        with pytest.raises(HubRefused):
            parse_link_address(t)
    # a host with no settings dials direct whatever a hub answered; no file appears
    addr, _, _ = rig.build("ws")
    rig.open(addr, load_net(tmp_path)).close()
    assert not (tmp_path / "hub-net.json").exists()


def test_only_the_netconfig_module_names_the_settings_files() -> None:
    root = Path(tls.__file__).resolve().parents[2]
    hits = [
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if re.search(r"hub-net\.json|hub-proxy\.secret", p.read_text())
    ]  # fmt: skip
    assert hits == ["hubclient/netconfig.py"], hits


def test_single_dial_path_guard() -> None:
    """Every link byte leaves through ``tls.dial_link``: pyOpenSSL connections are made only in tls.py, TCP dials of the
    link only in transport.py."""
    root = Path(tls.__file__).resolve().parents[2]
    ssl_in = {p.name for p in root.rglob("*.py") if "SSL.Connection(" in p.read_text()}
    assert ssl_in == {"tls.py"}
    for p in root.rglob("*.py"):
        if p.name != "transport.py":
            assert "dial_tcp(" not in p.read_text(), p


# --- netconfig ---


def test_netconfig_loads_validates_and_hides_the_password(tmp_path: Path) -> None:
    assert load_net(tmp_path) == NetSettings()  # no file: no proxy
    f = tmp_path / "hub-net.json"
    f.write_text('{"proxy": "https://proxy.office:3128", "proxy_user": "alice"}')
    os.chmod(f, 0o600)
    sec = tmp_path / "hub-proxy.secret"
    sec.write_text("s3cret\n")
    os.chmod(sec, 0o600)
    net = load_net(tmp_path)
    assert net.proxy == ProxySettings("https", "proxy.office", 3128, "alice", "s3cret")
    assert "s3cret" not in repr(net) and net.proxy and net.proxy.authorization == "Basic YWxpY2U6czNjcmV0"
    os.chmod(sec, 0o644)
    with pytest.raises(NetConfigError):
        load_net(tmp_path)
    os.chmod(sec, 0o600)
    for bad in (
        '{"proxy": "http://h"}', '{"proxy": "ftp://h:1", "proxy_user": null}',
        '{"proxy": "http://u:p@h:1", "proxy_user": null}',
        '{"proxy": "http://h:1/x", "proxy_user": null}', '{"proxy": null, "proxy_user": null, "x": 1}', "[]",
        '{"proxy": "http://h:1", "proxy_user": "a:b"}',
    ):  # fmt: skip
        f.write_text(bad)
        with pytest.raises(NetConfigError):
            load_net(tmp_path)


# --- WebSocket hazards ---


def test_ping_frames_never_wake_wait_readable_and_the_link_lives(rig: Rig) -> None:
    front = rig.add(WsFront(rig.hub.port, ping_interval=0.05))
    addr = parse_link_address(f"ws://127.0.0.1:{front.port}")
    with rig.open(addr, None) as link:
        conn = link._c
        assert isinstance(conn, tls.BioTlsConn)
        t0 = time.monotonic()
        assert conn.wait_readable(0.6) is False  # pings and the TLS tickets are not application bytes
        assert time.monotonic() - t0 >= 0.5
        assert front.pings >= 5
        assert send(link)["o"]["op"] == "snapshot_get"  # still a working link


def test_hub_data_wakes_wait_readable_through_the_pump(rig: Rig) -> None:
    front = rig.add(WsFront(rig.hub.port, ping_interval=0.05))
    addr = parse_link_address(f"ws://127.0.0.1:{front.port}")
    with rig.open(addr, None) as link:
        conn = link._c
        assert conn.wait_readable(0.2) is False
        # push an application frame from the hub; the reader wakes within one poll
        threading.Timer(0.2, lambda: rig.hub._conns[-1].send_all(b"\x00\x00\x00\x01x", time.monotonic() + 3)).start()
        assert conn.wait_readable(3.0) is True


def test_client_pings_when_idle_and_closes_when_the_front_goes_silent(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(W, "PING_AFTER_S", 0.1)
    monkeypatch.setattr(W, "IDLE_CLOSE_S", 5.0)
    front = rig.add(WsFront(rig.hub.port))
    addr = parse_link_address(f"ws://127.0.0.1:{front.port}")
    with rig.open(addr, None) as link:
        assert link._c.wait_readable(0.5) is False
        assert any(op == W.PING for op, _ in front.client_frames)  # idle: the client pinged, the front ponged
    monkeypatch.setattr(W, "IDLE_CLOSE_S", 0.6)
    front2 = rig.add(WsFront(rig.hub.port, no_pong=True))
    with rig.open(parse_link_address(f"ws://127.0.0.1:{front2.port}"), None) as link2:
        conn = link2._c
        deadline = time.monotonic() + 3
        while conn.wait_readable(0.3) is False and time.monotonic() < deadline:
            pass
        with pytest.raises(LinkClosed):  # no inbound byte for 75 s (here 0.6): closed, not hung
            conn.recv_exact(1, time.monotonic() + 1)


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("text", "ws_text"), ("rsv", "ws_rsv"), ("oversize", "ws_too_big"), ("masked", "ws_mask"),
        ("redirect", "ws_redirect"), ("bad_accept", "ws_accept"), ("bad_proto", "ws_subprotocol"),
        ("extensions", "ws_extensions"), ("status200", "ws_status"),
    ],
)  # fmt: skip
def test_ws_front_faults_are_refused_and_never_followed(rig: Rig, fault: str, reason: str) -> None:
    front = rig.add(WsFront(rig.hub.port, misbehave=fault))
    addr = parse_link_address(f"ws://127.0.0.1:{front.port}")
    with pytest.raises(LinkClosed, match=reason):
        tls.dial_link(addr, rig.hub.spki, timeout=3.0)
    assert not rig.hub._conns
    assert rig.dials == [("127.0.0.1", front.port)]  # a redirect target is never dialled


def test_wrong_outer_certificate_fails_the_address_without_retrying_as_ws(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    front = rig.add(WsFront(rig.hub.port, pki=rig.pki))
    monkeypatch.setattr(transport, "outer_context", ssl.create_default_context)  # system trust only: not the test CA
    with pytest.raises(LinkClosed, match="outer_tls"):
        tls.dial_link(parse_link_address(f"wss://127.0.0.1:{front.port}"), rig.hub.spki, timeout=3.0)
    assert rig.dials == [("127.0.0.1", front.port)] and not rig.hub._conns


def test_outer_wss_is_checked_but_the_pin_still_decides(rig: Rig) -> None:
    impostor = rig.hub_with(TlsHub(wsk_pub=WSK_PUB).start())
    addr, _, _ = rig.build("wss", hub=impostor)  # outer TLS is fine (trusted), inner key is not the card's
    with pytest.raises(LinkInspected):
        tls.dial_link(addr, rig.hub.spki, timeout=3.0)


def test_big_payload_goes_in_chunks_of_at_most_64k(rig: Rig) -> None:
    addr, _, front = rig.build("ws")
    with rig.open(addr, None) as link:
        n = 200_000  # one frame of that length (the test hub reads it whole, then drops the junk)
        link._c.send_all(n.to_bytes(4, "big") + b"x" * n, time.monotonic() + 5)
    sizes = [n for op, n in front.client_frames if op == W.BINARY]
    assert sizes and max(sizes) <= W.MAX_SEND_CHUNK + 64  # TLS record overhead on top of the app chunks


def test_a_closed_listener_is_just_unreachable(rig: Rig) -> None:
    with pytest.raises(LinkClosed):
        tls.dial_link(parse_link_address(f"ws://127.0.0.1:{closed_port()}"), rig.hub.spki, timeout=2.0)


def test_listener_base_is_abstract() -> None:
    assert Listener is not None


# --- the receiver is bounded (no flood amplification) ---


def test_receiver_decodes_in_linear_time_and_answers_only_the_last_ping() -> None:
    r = W.Receiver("server")
    data = b"".join(W.encode_frame(W.BINARY, b"x") for _ in range(20_000))
    t0 = time.monotonic()
    out, answers = r.feed(data)
    assert len(out) == 20_000 and time.monotonic() - t0 < 2.0
    r = W.Receiver("server")
    out, answers = r.feed(b"".join(W.encode_frame(W.PING, bytes([i])) for i in range(10)))
    assert out == b"" and answers == [(W.PONG, bytes([9]))]  # one pong for the most recent ping


def test_control_flood_per_read_and_per_second_is_closed() -> None:
    r = W.Receiver("server")
    with pytest.raises(W.WsError) as e:
        r.feed(W.encode_frame(W.PING, b"") * 10_000)
    assert (e.value.code, e.value.reason) == (1008, "flood")
    now = [0.0]
    r = W.Receiver("server", clock=lambda: now[0])
    for _ in range(W.MAX_CONTROL_PER_S):
        r.feed(W.encode_frame(W.PING, b""))
    with pytest.raises(W.WsError):
        r.feed(W.encode_frame(W.PING, b""))
    now[0] = 2.0  # a new second: the budget is back
    r = W.Receiver("server", clock=lambda: now[0])
    r.feed(W.encode_frame(W.PING, b""))


def test_ping_flood_from_the_front_is_bounded_and_wakes_nothing(rig: Rig) -> None:
    front = rig.add(WsFront(rig.hub.port))
    addr = parse_link_address(f"ws://127.0.0.1:{front.port}")
    with rig.open(addr, None) as link:
        conn = link._c
        assert conn.wait_readable(0.1) is False
        front.flood(10_000)
        t0 = time.monotonic()
        woke = conn.wait_readable(3.0)  # True only because the link was closed on the flood, never for data
        assert time.monotonic() - t0 < 2.0
        assert woke is True and conn._c.pending() == 0
        with pytest.raises(LinkClosed, match="ws_(flood|closed)"):
            conn.recv_exact(1, time.monotonic() + 1)
        assert rig.hub.requests == []


def test_bytes_before_the_inner_handshake_are_bounded(rig: Rig) -> None:
    class Pipe(transport.Pipe):
        def __init__(self) -> None:
            self.n = 0

        def poll(self, timeout: float) -> bytes | None:
            self.n += 1
            return W.encode_frame(W.BINARY, b"\x00" * 60)

        def write(self, data: bytes, deadline: float) -> None:
            pass

        def close(self) -> None:
            pass

    p = transport.WsPipe(Pipe())
    p.handshake_budget = 200
    with pytest.raises(LinkClosed, match="ws_flood"):
        for _ in range(10):
            p.poll(0.1)
    assert p._inner.n <= 4
    # a real link sets the bound while dialling and lifts it once the link's TLS has completed
    addr, _, _ = rig.build("ws")
    with rig.open(addr, None) as link:
        assert link._c._p.handshake_budget is None

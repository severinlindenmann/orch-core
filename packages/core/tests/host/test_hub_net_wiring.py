"""R6 T-C (#1594): the host's network settings reach every place that dials a hub, the inspected block outlives a
transport, and the four network codes reach the person as host codes in plain words."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.host import hubaddr, hubnet, hubops
from orch.hubclient import HubLink, HubRefused
from orch.hubclient.errors import HubRefused as _HR
from orch.hubclient.linkaddr import parse_link_address
from orch.hubclient.netconfig import NetConfigError, NetSettings, ProxySettings, load_net
from orch.hubclient.tlsadapter import InspectedBlock, LinkInspectedError, TlsTransport
from orch.hubclient.tlslink import tls
from orch.hubclient.tlslink.errors import ProxyAuth, ProxyRefused, ProxyUnreachable

SRC = Path(tls.__file__).resolve().parents[2]
PROXY = NetSettings(ProxySettings("http", "proxy.office", 3128))
DIAL_NAMES = {"connect_pinned", "dial_link", "TlsTransport", "open"}  # "open": LinkClient.open, checked by receiver


#: ``host/client.py:connect_pinned`` is the LOCAL host socket (peer UID check), not a hub link
NOT_A_HUB_DIAL = {"host/client.py"}


def _calls() -> list[tuple[str, int, ast.Call, str]]:
    out = []
    for p in sorted(SRC.rglob("*.py")):
        if str(p.relative_to(SRC)) in NOT_A_HUB_DIAL:
            continue
        tree = ast.parse(p.read_text())
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            recv = ast.unparse(f.value) if isinstance(f, ast.Attribute) else ""
            if name in ("connect_pinned", "dial_link", "TlsTransport") or (name == "open" and recv == "LinkClient"):
                out.append((str(p.relative_to(SRC)), n.lineno, n, name))
    return out


def test_every_dial_site_passes_the_hosts_net() -> None:
    """Walk the tree: a call that dials a hub link without ``net=`` would be a direct dial around the proxy."""
    calls = _calls()
    assert len(calls) >= 6, "the walk found too few dial sites; is it looking at the right names?"
    missing = [f"{f}:{line} {name}" for f, line, node, name in calls if "net" not in {k.arg for k in node.keywords}]
    assert not missing, missing


def test_the_known_host_dial_sites_are_in_the_walk() -> None:
    files = {f for f, _, _, _ in _calls()}
    for want in (
        "host/hubops.py",
        "host/peerops.py",
        "host/hubaddr.py",
        "remoteclient/client.py",
        "remoteclient/hubconn.py",
        "hubclient/tlsadapter.py",
    ):
        assert want in files, want


def test_the_stream_link_runner_dials_through_the_peerops_site() -> None:
    text = (SRC / "host/remote/linkrun.py").read_text()
    assert "peerops._open_link(" in text and "LinkClient" not in text and "dial_link" not in text


# -- the inspected block survives a transport ----------------------------------------------------------------------


def test_the_block_is_shared_by_address_and_survives_a_new_transport(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hubnet, "_BLOCKS", {})
    a = hubnet.inspected(tmp_path, "hub.example:7443")
    assert hubnet.inspected(tmp_path, "hub.example:7443") is a
    assert hubnet.inspected(tmp_path, "other.example:7443") is not a
    assert hubnet.inspected(tmp_path / "x", "hub.example:7443") is not a

    def make() -> TlsTransport:
        return TlsTransport(
            addr="hub.example:7443", tls_spki_sha256="A" * 43, hub_id="h", workspace_id="w", hk_op_pub=b"",
            wsk_pub=b"", sign=lambda b: b, inspected=hubnet.inspected(tmp_path, "hub.example:7443"),
        )  # fmt: skip

    t1 = make()
    a.start()
    t1.close()
    t2 = make()  # a brand-new transport: the five minutes still count, and nothing is dialled
    dialled = []
    monkeypatch.setattr("orch.hubclient.tlsadapter.LinkClient.open", lambda *a, **k: dialled.append(1))
    with pytest.raises(LinkInspectedError):
        t2.send_next("x", lambda s, e: {})
    assert not dialled


def test_the_block_ends_after_five_minutes() -> None:
    now = [100.0]
    b = InspectedBlock(clock=lambda: now[0])
    assert not b.active()
    b.start()
    now[0] += 299
    assert b.active()
    now[0] += 2
    assert not b.active()


def test_a_new_proxy_forgets_the_blocks(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hubnet, "_BLOCKS", {})
    (tmp_path / "hosts").mkdir(mode=0o700)
    b = hubnet.inspected(tmp_path, "h:1")
    b.start()
    hubnet.set_proxy(tmp_path, "http://proxy.office:3128", None, None)
    assert not hubnet.inspected(tmp_path, "h:1").active()


# -- the settings: load, set, clear, never the password ------------------------------------------------------------


def test_set_view_clear_and_the_password_is_never_returned(tmp_path) -> None:
    (tmp_path / "hosts").mkdir(mode=0o700)
    assert hubnet.view(tmp_path) == {"proxy": None, "user": None, "has_password": False, "broken": False}
    hubnet.set_proxy(tmp_path, "https://proxy.office:8443", "anna", "s3cret pass")
    v = hubnet.view(tmp_path)
    assert v == {"proxy": "https://proxy.office:8443", "user": "anna", "has_password": True, "broken": False}
    assert "s3cret" not in repr(v)
    d = hubnet.net_dir(tmp_path)
    for name in ("hub-net.json", "hub-proxy.secret"):
        assert (d / name).stat().st_mode & 0o777 == 0o600
    assert "s3cret" not in (d / "hub-net.json").read_text()
    assert load_net(d).proxy.password == "s3cret pass"
    hubnet.set_proxy(tmp_path, "https://proxy.office:8443", "anna", None)  # no new password: the old one stays
    assert load_net(d).proxy.password == "s3cret pass"
    hubnet.set_proxy(tmp_path, "http://p.office:3128", None, None)  # no user: the password goes
    assert not (d / "hub-proxy.secret").exists()
    hubnet.clear_proxy(tmp_path)
    assert hubnet.view(tmp_path)["proxy"] is None and not (d / "hub-net.json").exists()


@pytest.mark.parametrize(
    "proxy,user,password",
    [("ftp://p:1", None, None), ("http://p", None, None), ("http://u:pw@p:3128", None, None),
     ("http://p:3128", "a:b", "x"), ("http://p:3128", None, "pw"), ("http://p:3128", "a", "x\ny")],
)  # fmt: skip
def test_bad_settings_are_refused_and_change_nothing(tmp_path, proxy, user, password) -> None:
    (tmp_path / "hosts").mkdir(mode=0o700)
    with pytest.raises(NetConfigError):
        hubnet.set_proxy(tmp_path, proxy, user, password)
    assert not (hubnet.net_dir(tmp_path) / "hub-net.json").exists()


def test_loose_files_make_load_fail_instead_of_dialling_direct(tmp_path) -> None:
    (tmp_path / "hosts").mkdir(mode=0o700)
    hubnet.set_proxy(tmp_path, "http://p.office:3128", None, None)
    os.chmod(hubnet.net_dir(tmp_path) / "hub-net.json", 0o644)
    with pytest.raises(ProxyRefused):
        hubnet.load(tmp_path)
    assert hubnet.view(tmp_path)["broken"] is True
    link = {"addr": "hub.example:7443", "spki": "A" * 43, "hk_op": b"", "wsk_pub": b"", "sign": lambda b: b}
    svc = SimpleNamespace(state_dir=tmp_path, workspace_id="w", clock=lambda: 1.0)
    t = hubops._transport(svc, "h", link)
    with pytest.raises(ProxyRefused):
        t.send_next("x", lambda s, e: {})


def test_the_host_transport_gets_the_net_and_the_shared_block(tmp_path, monkeypatch) -> None:
    (tmp_path / "hosts").mkdir(mode=0o700)
    hubnet.set_proxy(tmp_path, "http://p.office:3128", "u", "pw")
    link = {"addr": "hub.example:7443", "spki": "A" * 43, "hk_op": b"", "wsk_pub": b"", "sign": lambda b: b}
    svc = SimpleNamespace(state_dir=tmp_path, workspace_id="w", clock=lambda: 1.0)
    t = hubops._transport(svc, "h", link)
    assert isinstance(t, TlsTransport)
    assert t._net.proxy.host == "p.office" and t._net.proxy.user == "u"  # noqa: SLF001
    assert t._inspected is hubnet.inspected(tmp_path, "hub.example:7443")  # noqa: SLF001


# -- the probe -----------------------------------------------------------------------------------------------------


def test_the_probe_dials_ws_addresses_with_dial_link_and_the_net(monkeypatch) -> None:
    seen = []

    class Conn:
        def close(self) -> None: ...

    def fake(addr, spki, **kw):
        seen.append((addr, kw))
        return Conn()

    monkeypatch.setattr(hubaddr, "PROBE", None)
    monkeypatch.setattr(tls, "dial_link", fake)
    monkeypatch.setattr(hubaddr, "checked_ip", lambda host, allow_loopback: "203.0.113.9")
    assert hubaddr.handshake_ok("wss://hub.example", "A" * 43, net=NetSettings()) is True
    addr, kw = seen[-1]
    assert addr.scheme == "wss" and kw["connect_host"] == "203.0.113.9" and kw["net"] == NetSettings()
    # behind a proxy a NAME is the proxy's to resolve: no local lookup at all
    monkeypatch.setattr(hubaddr, "checked_ip", lambda *a, **k: pytest.fail("resolved locally behind a proxy"))
    assert hubaddr.handshake_ok("wss://hub.example", "A" * 43, net=PROXY) is True
    assert seen[-1][1]["connect_host"] == "hub.example" and seen[-1][1]["net"] is PROXY
    # an IP literal keeps the policy check (and is dialled as that IP)
    monkeypatch.setattr(hubaddr, "checked_ip", lambda host, allow_loopback: host)
    assert hubaddr.handshake_ok("203.0.113.9:7443", "A" * 43, net=PROXY) is True
    assert seen[-1][1]["connect_host"] == "203.0.113.9"


def test_status_does_not_probe_directly_behind_a_proxy(monkeypatch) -> None:
    from orch.host import hublinkprobe

    hublinkprobe.clear()
    monkeypatch.setattr(
        "socket.create_connection", lambda *a, **k: pytest.fail("a direct connect went around the proxy")
    )
    assert hublinkprobe.reachable("s", "hub.example:7443", net=PROXY) is None
    assert parse_link_address("hub.example:7443").host == "hub.example"


# -- the four codes ------------------------------------------------------------------------------------------------


class _Down:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def send_next(self, op, build):
        raise self.exc


@pytest.mark.parametrize(
    "exc,wire,host",
    [
        (LinkInspectedError("x"), "link_inspected", "hub.link_inspected"),
        (ProxyAuth("x"), "proxy_auth", "hub.proxy_auth"),
        (ProxyRefused("x"), "proxy_refused", "hub.proxy_refused"),
        (ProxyUnreachable("x"), "proxy_unreachable", "hub.proxy_unreachable"),
    ],
)
def test_the_network_codes_reach_the_host_in_plain_words(exc, wire, host) -> None:
    link = HubLink.__new__(HubLink)
    link._t, link.mode = _Down(exc), "selective"  # noqa: SLF001
    link._sign = lambda b: b  # noqa: SLF001
    link._hk_op, link.hub_id, link.workspace_id, link._now = b"", "h", "w", lambda: 1  # noqa: SLF001
    with pytest.raises(_HR) as e:
        link.call("hub_where", {})
    assert e.value.code == wire
    he = hubops._refused(e.value.code)
    assert he.code == host and he.hint
    text = (he.message + " " + he.hint).lower()
    assert "netbird" not in text and "tls" not in text
    from orch.host.errors import HOST_CODES

    assert host in HOST_CODES
    assert HubRefused is _HR


def test_an_ordinary_oserror_is_still_just_unreachable() -> None:
    link = HubLink.__new__(HubLink)
    link._t, link.mode = _Down(ConnectionRefusedError()), "selective"  # noqa: SLF001
    with pytest.raises(_HR) as e:
        link.call("hub_where", {})
    assert e.value.code == "hub_unreachable"

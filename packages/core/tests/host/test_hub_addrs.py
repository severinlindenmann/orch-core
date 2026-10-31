"""M4 S3 (#1461): the host side of hub addresses. ``addrs`` next to ``addr`` in ``hub.json``, learned from the hub's
``hub_where`` on a link session, and the owner's "move to this address" from a connect link of the same hub.

Opus conditions 1-4 of the M4 design check: an address is stored only after a TLS handshake AT it passed the
pin the host holds (1); the address in use stays until a replacement passed (2); a move from a link is a person's
click, never uses the invite and never replaces the card (3); the hub's name is the hub's own words and the
answer is validated (4)."""

from __future__ import annotations

import copy

import pytest

from orch.crypto import suite
from orch.host import hubaddr, hubops
from orch.hubclient import HubLink, HubRefused, HubState, connectlink, wire
from orch.hubclient.netconfig import NetSettings
from orch.hubclient.tlsadapter import FailoverTransport, TlsTransport
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.hub.fake import FakeHub
from tests.hub.tlshub import TlsHub
from tests.hub.vec import V

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
CARD = V["objects"]["card"]
HUB_ID = V["ids"]["hub"]
PIN = CARD["o"]["tls_spki_sha256"]


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


class Probe:
    """The handshake seam: ``live`` is the set of (host:port) that present the held pin; every call is recorded."""

    def __init__(self, live=()):
        self.live, self.calls = set(live), []

    def __call__(self, host, port, pin):
        self.calls.append((f"{host}:{port}", pin))
        if f"{host}:{port}" not in self.live or pin != PIN:
            raise ConnectionRefusedError


@pytest.fixture
def probe(monkeypatch):
    hubaddr.clear()
    p = Probe()
    monkeypatch.setattr(hubaddr, "PROBE", p)
    return p


def _service(served):
    return next(iter(served.h.host.services.values()))


def _state(served) -> HubState:
    return hubops._load_state(_service(served))  # noqa: SLF001


def bound(served, human, monkeypatch, addr="127.0.0.1:9") -> FakeHub:
    hub = FakeHub(now_ms=int(served.lab.ws.clock[0] * 1000))
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: hub)
    card = copy.deepcopy(CARD)
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    ok(served.client().req("hub.connect", {"invite": "invite-1", "hub_card": card, "addr": addr}, idem=True))
    return hub


def where(*links, name="Acme hub"):
    kinds = ("private", "office", "server", "public")
    return {"addresses": [{"kind": kinds[i], "link": link} for i, link in enumerate(links)], "name": name}


def refresh(served):
    got = ok(served.client().req("hub.refresh", {}, idem=True))["data"]
    hubaddr.wait_idle()  # the probes run after the link is closed, off the refresh
    return got


def status(served):
    return ok(served.client().req("hub.status", {}, idem=False))["data"]


# --- the state ----------


def test_addrs_are_part_of_the_strict_state_shape(human, served, monkeypatch):
    bound(served, human, monkeypatch)
    doc = _state(served).to_json()
    assert doc["addrs"] == [] and doc["hub_name"] is None
    for bad in (["nope"], ["10.0.0.1:1"] * 2, [f"10.0.0.{i}:8105" for i in range(1, 6)], "10.0.0.1:1"):
        with pytest.raises(HubRefused):
            HubState.from_json({**doc, "addrs": bad})
    with pytest.raises(HubRefused):
        HubState.from_json({**doc, "hub_name": "x" * 61})
    for gone in ("addrs", "hub_name"):  # no old shape: a hub.json without them is refused (nothing in production)
        with pytest.raises(HubRefused):
            HubState.from_json({k: v for k, v in doc.items() if k != gone})


# --- learning from hub_where (cond 1, 2, 4) ----------


def test_where_addresses_are_stored_only_after_the_pinned_handshake_at_that_address(human, served, monkeypatch, probe):
    hub = bound(served, human, monkeypatch)
    hub.where_answer = where("10.0.0.5:8105", "192.168.1.5:8105", "127.0.0.1:9")
    probe.live = {"10.0.0.5:8105"}
    assert refresh(served)["hub_id"] == HUB_ID
    st = _state(served)
    assert st.addr == "127.0.0.1:9" and st.addrs == ["10.0.0.5:8105"]  # the dead one was advertised, never stored
    # every probe was a handshake AT the advertised address against the pin the host already holds
    assert sorted(probe.calls) == [("10.0.0.5:8105", PIN), ("192.168.1.5:8105", PIN)]
    assert status(served)["addrs"] == ["127.0.0.1:9", "10.0.0.5:8105"]


def test_a_hub_that_presents_another_certificate_at_the_address_is_not_stored(human, served, monkeypatch):
    bound(served, human, monkeypatch).where_answer = where("10.0.0.5:8105")
    seen = []

    def other_cert(host, port, pin):
        seen.append(pin)
        raise hubaddr_pin_mismatch()

    def hubaddr_pin_mismatch():
        from orch.hubclient.tlslink.tls import PinMismatch

        return PinMismatch()

    monkeypatch.setattr(hubaddr, "PROBE", other_cert)
    refresh(served)
    assert seen == [PIN] and _state(served).addrs == []


def test_a_card_fetched_from_a_link_is_never_evidence_for_where(human, served, monkeypatch, probe):
    """The pin used for the probe is the held one even if a hub_where-carrying session saw a different card."""
    hub = bound(served, human, monkeypatch)
    hub.where_answer = where("10.0.0.5:8105")
    probe.live = {"10.0.0.5:8105"}
    refresh(served)
    assert {pin for _, pin in probe.calls} == {_state(served).tls_spki}


def test_the_hub_advertising_only_dead_addresses_changes_nothing_and_the_old_one_stays(
    human, served, monkeypatch, probe
):
    hub = bound(served, human, monkeypatch)
    hub.where_answer = where("10.9.9.9:8105", "10.9.9.8:8105")
    before = _state(served)
    assert refresh(served)["hub_id"] == HUB_ID  # the refresh itself still works
    after = _state(served)
    assert (after.addr, after.addrs) == (before.addr, before.addrs) == ("127.0.0.1:9", [])
    assert len(probe.calls) == 2


def test_at_most_four_probes_per_refresh_and_four_addresses_kept(human, served, monkeypatch, probe):
    hub = bound(served, human, monkeypatch)
    links = [f"10.0.0.{i}:8105" for i in range(1, 5)]  # a hub answer holds at most 4 entries (the validator checks)
    hub.where_answer = where(*links)
    probe.live = set(links)
    refresh(served)
    st = _state(served)
    assert st.addrs == links and len(probe.calls) == 4
    hub.where_answer = where("10.0.0.99:8105", name="Acme hub")  # a fifth address: only 4 are kept
    probe.live.add("10.0.0.99:8105")
    refresh(served)
    st = _state(served)
    assert len(st.addrs) == 4 and st.addrs[0] == "10.0.0.99:8105"


def test_a_malformed_answer_keeps_what_the_host_holds(human, served, monkeypatch, probe):
    hub = bound(served, human, monkeypatch)
    probe.live = {"10.0.0.5:8105"}
    for bad in (
        {"addresses": [], "name": "x"},
        {"addresses": [{"kind": "lan", "link": "10.0.0.5:8105"}], "name": "x"},
        {"addresses": [{"kind": "private", "link": "http://10.0.0.5:8105"}], "name": "x"},
        {"addresses": [{"kind": "private", "link": "10.0.0.5:8105"}], "name": "‮"},
        {"addresses": [{"kind": "private", "link": "10.0.0.5:8105"}], "name": "x", "extra": 1},
    ):
        hub.where_answer = bad
        refresh(served)
        assert _state(served).addrs == [] and probe.calls == []


def test_the_name_is_carried_as_the_hubs_own_words_and_cleaned(human, served, monkeypatch, probe):
    hub = bound(served, human, monkeypatch)
    hub.where_answer = where("10.0.0.5:8105", name="‮Acme\x00 hub​")
    probe.live = {"10.0.0.5:8105"}
    refresh(served)
    assert _state(served).hub_name == "Acme hub"
    assert status(served)["hub_says"] == {"name": "Acme hub", "from": "hub"}  # labelled; no workspace-owned field


def test_a_hub_without_hub_where_still_refreshes(human, served, monkeypatch, probe):
    bound(served, human, monkeypatch)  # the fake knows no such op: unknown_op, swallowed
    assert refresh(served)["hub_id"] == HUB_ID and _state(served).addrs == []


# --- dialling (cond 2) ----------


def test_dial_order_is_current_first_then_stored_addresses(human, served, monkeypatch):
    bound(served, human, monkeypatch)
    service = _service(served)
    st = _state(served)
    st.addrs = ["10.0.0.5:8105", "10.0.0.6:8105"]
    assert [hubops._addr({}, st, False), *hubops._more_addrs(st, False)] == [  # noqa: SLF001
        "127.0.0.1:9", "10.0.0.5:8105", "10.0.0.6:8105",
    ]  # fmt: skip
    monkeypatch.setenv("ORCH_HUB_ADDR", "127.0.0.1:7")  # a dev override names the one address
    assert hubops._more_addrs(st, True) == [] and hubops._more_addrs(st, False) == st.addrs  # noqa: SLF001
    assert service is not None


def test_a_workspace_connects_on_a_stored_address_when_the_current_one_is_dead():
    wsk = suite.generate_private_key()
    hub = TlsHub(wsk_pub=suite.public_bytes(wsk)).start()
    try:
        kw = dict(
            tls_spki_sha256=hub.spki, hub_id=hub.hub_id, workspace_id=hub.workspace_id,
            hk_op_pub=hub.hk_op_pub, wsk_pub=suite.public_bytes(wsk), sign=lambda m: suite.sign(wsk, m), timeout=3.0,
        )  # fmt: skip
        dead = TlsTransport(addr="127.0.0.1:1", **kw, net=NetSettings())
        live = TlsTransport(addr=f"127.0.0.1:{hub.port}", **kw, net=NetSettings())
        link = HubLink(
            FailoverTransport([dead, live]), hub_id=hub.hub_id, workspace_id=hub.workspace_id,
            hk_op_pub=hub.hk_op_pub, sign=lambda m: suite.sign(wsk, m), mode="selective", now_ms=lambda: _now(),
        )  # fmt: skip
        assert link.call("status_put", {"version": "1"}) == {}
        assert len(hub.requests) == 1  # it went to the live address, the dead one was skipped
    finally:
        hub.close()


def _now() -> int:
    import time

    return int(time.time() * 1000)


def test_the_real_handshake_probe_needs_the_held_pin():
    wsk = suite.generate_private_key()
    hub = TlsHub(wsk_pub=suite.public_bytes(wsk)).start()
    try:
        addr = f"127.0.0.1:{hub.port}"
        assert hubaddr.handshake_ok(addr, hub.spki, allow_loopback=True, net=NetSettings()) is True
        other = TlsHub(wsk_pub=suite.public_bytes(wsk)).start()
        try:
            assert (
                hubaddr.handshake_ok(addr, other.spki, allow_loopback=True, net=NetSettings()) is False
            )  # another certificate than the pin
        finally:
            other.close()
        assert (
            hubaddr.handshake_ok(addr, None, net=NetSettings()) is False
            and hubaddr.handshake_ok("127.0.0.1:1", hub.spki, allow_loopback=True, net=NetSettings()) is False
        )
        assert hubaddr.handshake_ok("not an address", hub.spki, net=NetSettings()) is False
    finally:
        hub.close()


# --- the move from a connect link (cond 1, 3) ----------


def link_text(endpoint="10.0.0.9:8105", hub_id=HUB_ID):
    tail = "" if endpoint is None else f".{endpoint}"
    return f"http://127.0.0.1:1/c#h1.{hub_id}.Code_1{tail}"


@pytest.fixture
def card_served(monkeypatch):
    box = {"card": copy.deepcopy(CARD)}
    monkeypatch.setattr(connectlink, "fetch_card", lambda link: box["card"])
    return box


def move(served, link=None, **kw):
    return served.client().req("hub.address", {"link": link or link_text()}, idem=True, **kw)


def test_moving_to_the_address_of_a_link_of_the_same_hub(human, served, monkeypatch, probe, card_served):
    hub = bound(served, human, monkeypatch)
    probe.live = {"10.0.0.9:8105"}
    calls, card, seq = list(hub.calls), copy.deepcopy(_state(served).card), _state(served).binding_seq
    got = ok(move(served))["data"]
    st = _state(served)
    assert got["addr"] == "10.0.0.9:8105" and st.addr == "10.0.0.9:8105" and st.addrs == ["127.0.0.1:9"]
    assert probe.calls == [("10.0.0.9:8105", PIN)]  # a handshake at the new address against the held pin
    # cond 3: nothing was joined or sent (the invite is not used), the card and the binding stay
    assert hub.calls == calls and st.card == card and st.binding_seq == seq and st.status == "bound"


def test_a_link_to_a_dead_address_changes_nothing(human, served, monkeypatch, probe, card_served):
    bound(served, human, monkeypatch)
    assert err(move(served)) == "hub.unreachable"
    st = _state(served)
    assert st.addr == "127.0.0.1:9" and st.addrs == []


def test_a_link_of_another_hub_or_another_pin_is_refused(human, served, monkeypatch, probe, card_served):
    bound(served, human, monkeypatch)
    probe.live = {"10.0.0.9:8105"}
    assert err(move(served, link_text(hub_id="0" * 32))) == "hub.refused"
    real = wire.verify_card

    def other_pin(*a, **k):
        out = real(*a, **k)
        return {**out, "card": {**out["card"], "tls_spki_sha256": "A" * 43}}

    monkeypatch.setattr(wire, "verify_card", other_pin)
    e = move(served)
    assert err(e) == "hub.refused" and "not the hub you are connected to" in str(e)
    assert _state(served).addr == "127.0.0.1:9" and probe.calls == []


def test_the_card_of_a_link_is_not_adopted_even_when_it_looks_newer(human, served, monkeypatch, probe, card_served):
    bound(served, human, monkeypatch)
    probe.live = {"10.0.0.9:8105"}
    card_served["card"] = {**copy.deepcopy(CARD), "junk": 1}  # whatever the link serves
    held = copy.deepcopy(_state(served).card)
    move(served)
    assert _state(served).card == held


def test_a_link_without_an_address_is_refused(human, served, monkeypatch, probe, card_served):
    bound(served, human, monkeypatch)
    assert err(move(served, link_text(None))) == "invalid.input"
    assert err(move(served, link_text("local"))) == "invalid.input"


def test_an_agent_cannot_move_the_hub_address(human, served, monkeypatch, probe, card_served):
    bound(served, human, monkeypatch)
    probe.live = {"10.0.0.9:8105"}
    assert err(move(served, grant=served.grant, session=SESSION)) in ("op.not_allowed", "human_only")
    assert _state(served).addr == "127.0.0.1:9" and probe.calls == []


def test_moving_without_a_hub_is_refused(human, served, probe, card_served):
    assert err(move(served)) == "hub.not_joined"


# --- the probe policy: a hub must not aim this computer at internal hosts -----------------------------------------


@pytest.mark.parametrize(
    "ip",
    ["169.254.169.254", "169.254.1.1", "fe80::1", "fd00:ec2::254", "224.0.0.1", "ff02::1", "0.0.0.0", "::",
     "255.255.255.255", "::ffff:169.254.169.254", "127.0.0.1", "::1",
     "2002:a9fe:a9fe::1", "2002:7f00:1::1",  # 6to4 around 169.254.169.254 and 127.0.0.1
     "64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1",  # NAT64
     "::a9fe:a9fe", "::7f00:1"],  # IPv4-compatible
)  # fmt: skip
def test_the_policy_refuses_internal_targets(ip):
    assert hubaddr.ip_allowed(ip, allow_loopback=False) is False


@pytest.mark.parametrize("ip", ["10.0.0.5", "192.168.1.20", "172.16.3.4", "100.64.0.9", "203.0.113.7", "fd12::5"])
def test_the_policy_allows_lan_and_private_network_addresses(ip):
    assert hubaddr.ip_allowed(ip, allow_loopback=False) is True


def test_loopback_is_allowed_only_next_to_a_loopback_current_address():
    assert hubaddr.ip_allowed("127.0.0.1", allow_loopback=True) is True
    assert hubaddr.ip_allowed("169.254.169.254", allow_loopback=True) is False  # never, whatever the current address


def test_a_hub_cannot_make_the_host_probe_metadata_link_local_or_loopback(human, served, monkeypatch, probe):
    hub = bound(served, human, monkeypatch, addr="10.0.0.1:8105")  # a non-loopback address in use
    hub.where_answer = where("169.254.169.254:80", "[fe80::1]:8105", "127.0.0.1:8105", "10.0.0.7:8105")
    probe.live = {"169.254.169.254:80", "fe80::1:8105", "127.0.0.1:8105", "10.0.0.7:8105"}
    refresh(served)
    assert probe.calls == [("10.0.0.7:8105", PIN)]  # the forbidden ones never reached the dial
    assert _state(served).addrs == ["10.0.0.7:8105"]


def test_loopback_from_the_hub_is_probed_when_the_current_address_is_loopback(human, served, monkeypatch, probe):
    hub = bound(served, human, monkeypatch, addr="127.0.0.1:9")
    hub.where_answer = where("127.0.0.1:8105")
    probe.live = {"127.0.0.1:8105"}
    refresh(served)
    assert _state(served).addrs == ["127.0.0.1:8105"]


def test_a_name_is_resolved_once_and_the_checked_ip_is_the_one_dialled(monkeypatch, probe):
    answers = iter(["10.0.0.7", "169.254.169.254"])  # a second lookup would give the metadata address
    monkeypatch.setattr(hubaddr.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", (next(answers), 0))])
    probe.live = {"10.0.0.7:8105"}
    assert hubaddr.handshake_ok("hub.example:8105", PIN, net=NetSettings()) is True
    assert probe.calls == [("10.0.0.7:8105", PIN)]  # the IP, not the name
    monkeypatch.setattr(hubaddr.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("169.254.169.254", 0))])
    assert (
        hubaddr.handshake_ok("evil.example:8105", PIN, net=NetSettings()) is False and len(probe.calls) == 1
    )  # refused before dialling


def test_a_failed_address_is_not_tried_again_within_an_hour_and_the_hub_learns_nothing(
    human, served, monkeypatch, probe
):
    hub = bound(served, human, monkeypatch)
    hub.where_answer = where("10.9.9.9:8105")
    refresh(served)
    assert len(probe.calls) == 1
    seen = len(hub.calls)
    refresh(served)
    assert len(probe.calls) == 1  # backed off: not dialled again
    ops = [op for _, op, _ in hub.calls[seen:]]
    assert set(ops) <= {
        "snapshot_get",
        "hub_where",
        "status_put",
        "profile_put",
        "error",
    }  # "error": ops this fake lacks
    real = hubaddr.time.monotonic
    monkeypatch.setattr(hubaddr.time, "monotonic", lambda: real() + 3601)
    refresh(served)
    assert len(probe.calls) == 2  # the hour is over


# --- the same policy at the dial (DNS rebinding) and loopback decided by the stored text --------------------------


def test_a_name_that_turns_into_the_metadata_address_after_the_probe_is_refused_at_dial(monkeypatch):
    wsk = suite.generate_private_key()
    hub = TlsHub(wsk_pub=suite.public_bytes(wsk)).start()
    try:
        answers = {"ip": "10.0.0.7"}
        monkeypatch.setattr(hubaddr.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", (answers["ip"], 0))])
        check = hubaddr.dial_resolver("10.0.0.1:8105", net=NetSettings())
        assert check("hub.example") == "10.0.0.7"  # at probe time the name is fine
        answers["ip"] = "169.254.169.254"  # ... and later it is not
        kw = dict(
            tls_spki_sha256=hub.spki, hub_id=hub.hub_id, workspace_id=hub.workspace_id, hk_op_pub=hub.hk_op_pub,
            wsk_pub=suite.public_bytes(wsk), sign=lambda m: suite.sign(wsk, m), timeout=3.0,
        )  # fmt: skip
        t = TlsTransport(addr="hub.example:8105", resolve=check, **kw, net=NetSettings())
        with pytest.raises(OSError):
            t.ensure_open("status_put")  # refused before any connection
        assert hub.auth_frames == []
    finally:
        hub.close()


def test_a_name_resolving_to_loopback_does_not_grant_loopback(monkeypatch):
    monkeypatch.setattr(hubaddr.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 0))])
    assert hubaddr.is_loopback("hub.example:8105") is False  # the stored text decides, not DNS
    assert hubaddr.is_loopback("127.0.0.1:8105") is True and hubaddr.is_loopback("localhost:8105") is True
    assert hubaddr.is_loopback("[::1]:8105") is True and hubaddr.is_loopback("10.0.0.1:8105") is False
    with pytest.raises(OSError):
        hubaddr.checked_ip("hub.example", allow_loopback=hubaddr.is_loopback("hub.example:8105"))


# --- review fixes: no probe timing for the hub, the policy on the current address, pruning -------------------------


def test_probes_run_after_the_link_is_closed_and_the_refresh_never_waits_for_them(human, served, monkeypatch):
    import threading

    hub = bound(served, human, monkeypatch)
    state = {"closed": False}

    class Wrapped:
        send_next = staticmethod(hub.send_next)

        def close(self):
            state["closed"] = True

    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: Wrapped())
    hub.where_answer = where("10.0.0.5:8105")
    started, release, seen = threading.Event(), threading.Event(), []

    def slow_probe(host, port, pin):
        seen.append(state["closed"])  # the hub's link is already shut when the first probe starts
        started.set()
        release.wait(10)

    monkeypatch.setattr(hubaddr, "PROBE", slow_probe)
    hubaddr.clear()
    got = ok(served.client().req("hub.refresh", {}, idem=True))["data"]  # returns while the probe is still blocked
    assert got["hub_id"] == HUB_ID and not release.is_set()
    assert started.wait(10) and seen == [True]
    release.set()
    hubaddr.wait_idle()
    assert _state(served).addrs == ["10.0.0.5:8105"]


def _no_dial(monkeypatch):
    """Any real dial fails the test loudly: a refusal must happen before it."""
    from orch.hubclient.tlslink.client import LinkClient

    def dialled(*a, **k):
        raise AssertionError("dialled")

    monkeypatch.setattr(LinkClient, "open", staticmethod(dialled))
    monkeypatch.setattr(hubaddr.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("169.254.169.254", 0))])


def test_the_policy_applies_to_the_current_address_at_every_dial(monkeypatch):
    from types import SimpleNamespace

    _no_dial(monkeypatch)
    service = SimpleNamespace(workspace_id="w" * 32, clock=lambda: 0.0, state_dir="/nonexistent-state")
    link = {"addr": "hub.example:8105", "spki": PIN, "hk_op": b"", "wsk_pub": b"", "sign": lambda m: b""}
    t = hubops._transport(service, HUB_ID, link)  # noqa: SLF001
    with pytest.raises(OSError, match="refused by policy"):
        t.ensure_open("status_put")


def test_the_peer_link_applies_the_policy_to_the_current_address_too(monkeypatch):
    from types import SimpleNamespace

    from orch.host import peerops

    _no_dial(monkeypatch)
    state = SimpleNamespace(
        binding={"o": {"dev": False}}, addr="hub.example:8105", addrs=[], tls_spki=PIN, hub_id=HUB_ID, mode="selective",
        hk_op_pub=CARD["op_cert"]["o"]["hk_op_pub"],
    )  # fmt: skip
    monkeypatch.setattr(peerops, "_hub_state", lambda service, full: state)
    monkeypatch.setattr(hubops, "_wsk_sign", lambda service: (b"", lambda m: b""))
    service = SimpleNamespace(workspace_id="w" * 32, clock=lambda: 0.0, state_dir="/nonexistent-state")
    with pytest.raises(OSError, match="refused by policy"):
        peerops._open_link(service)  # noqa: SLF001


def test_old_failures_are_forgotten(probe):
    st = SimpleNamespace_state()
    hubaddr._FAILED[("s", "10.9.9.9:1")] = hubaddr.time.monotonic() - hubaddr.FAIL_BACKOFF_S - 1  # noqa: SLF001
    hubaddr._probe_new("s", st, [], NetSettings())  # noqa: SLF001
    assert ("s", "10.9.9.9:1") not in hubaddr._FAILED  # noqa: SLF001


def SimpleNamespace_state():
    from types import SimpleNamespace

    return SimpleNamespace(addr="10.0.0.1:8105", tls_spki=PIN)

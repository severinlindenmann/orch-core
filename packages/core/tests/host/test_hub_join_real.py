"""V3 C2a step 3: ``hub.connect`` / ``hub.unbind`` with nothing stubbed on the host side: the workspace has no relay, so
its v2 directory card is built locally; the link is the real TLS link of C5 (against C5's test hub) or an in-process
transport on C1's fake hub, whose checks run on the real card and binding."""

from __future__ import annotations

import copy
import os

import pytest

from orch import crypto
from orch.host import hubops
from orch.hubclient import wire
from orch.hubclient.tlsadapter import parse_addr
from orch.identity import wscard
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err
from tests.hub.fake import FakeHub
from tests.hub.tlshub import TlsHub
from tests.hub.vec import V

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


def genesis(served):
    store = served.lab.ws.other()
    try:
        return store.genesis_event()
    finally:
        store.close()


def now_ms(served) -> int:
    return int(served.lab.ws.clock[0] * 1000)


def test_a_workspace_without_a_relay_builds_its_signed_card_once(human, served):
    g = genesis(served)
    pk = crypto.unb64u(g["owner"]["pk_pub"], 65)
    service = next(iter(served.h.host.services.values())) if hasattr(served.h.host, "services") else None
    if service is None:
        pytest.skip("no service handle in this lab")
    first = hubops.workspace_card(service)
    assert first is not None and hubops.workspace_card(service) == first  # kept: the same bytes every time
    vc = wscard.verify_card(dict(first), pk)  # the relay path's own verifier, under the owner's PK
    assert vc.workspace_id == served.wid and first["delegation"] == g["delegation"] and first["o"]["card_seq"] == 1
    assert os.path.exists(hubops._dir(service) / "card.json")


def fake_for(served) -> FakeHub:
    return FakeHub(now_ms=now_ms(served))


def test_join_against_the_fake_hub_with_no_relay_and_no_stub(human, served, monkeypatch):
    hub = fake_for(served)
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: hub)
    card = copy.deepcopy(V["objects"]["card"])
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    got = ok(served.client().req("hub.connect", {"invite": "invite-1", "hub_card": card}, idem=True))["data"]
    assert got["status"] == "bound"
    ws = hub.workspaces[served.wid]
    assert ws.mode == "selective" and ws.binding["status"] == "bound" and ws.wk_seals == []
    assert ws.owner_person_id == crypto.person_id(crypto.unb64u(genesis(served)["owner"]["pk_pub"], 65)).hex()
    assert hub.published_cards[served.wid]["o"]["workspace_id"] == served.wid
    assert all(op != "wk_seal" for _, op, _ in hub.calls)
    # leave: the fake takes the unbound and refuses the old binding from then on
    human.run("hub.leave", {})
    assert ok(served.client().req("hub.unbind", {}, idem=True))["data"]["delivered"]
    assert hub.workspaces[served.wid].binding["status"] == "unbound"


def make_card(hub: TlsHub, now: int, hxk=None) -> dict:
    """A hub card for the TLS test hub (dev, pinned to its certificate); the hub takes the card's identity."""
    from orch.canon.jcs import dumps as cj
    from orch.crypto import suite

    root = suite.generate_private_key()
    root_pub = suite.public_bytes(root)
    hub.hub_id = wire.hub_id(root_pub)
    op = {
        "v": 2, "suite": 2, "kind": "hub.op_cert", "hv": 0, "hub_id": hub.hub_id, "cert_id": os.urandom(16).hex(),
        "op_seq": 1, "hk_op_pub": crypto.b64u(hub.hk_op_pub), "issued_ms": now - 1000, "expires_ms": now + 10**9,
        "dev": True,
    }  # fmt: skip
    o = {
        "v": 2, "suite": 2, "kind": "hub.card", "hv": 0, "hub_id": hub.hub_id, "hk_root_pub": crypto.b64u(root_pub),
        "hxk_pub": crypto.b64u(suite.public_bytes(hxk or suite.generate_private_key())), "relay_url": None,
        "tls_spki_sha256": hub.spki, "card_seq": 1, "reader": 1, "issued_ms": now - 1000, "dev": True,
    }  # fmt: skip
    return {
        "o": o,
        "op_cert": {"o": op, "sig": crypto.b64u(suite.sign(root, wire.SIG_OP_CERT + cj(op)))},
        "sig": crypto.b64u(suite.sign(hub.hk_op, wire.SIG_CARD + cj(o))),
    }


@pytest.fixture
def tls_hub(served):
    wsk = crypto.unb64u(genesis(served)["wsk_pub"], 65)
    hub = TlsHub(wsk_pub=wsk, on_request=lambda h, o: h.reply(o, ts_ms=o["ts_ms"]))
    hub.workspace_id = served.wid
    hub.start()
    try:
        yield hub
    finally:
        hub.close()


def test_join_and_leave_over_the_real_tls_link(human, served, tls_hub):
    card = make_card(tls_hub, now_ms(served))
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    args = {"invite": "invite-1", "hub_card": card, "addr": f"127.0.0.1:{tls_hub.port}"}
    got = ok(served.client().req("hub.connect", args, idem=True))["data"]
    assert got["status"] == "bound" and got["hub_id"] == tls_hub.hub_id
    assert tls_hub.auth_verified.is_set() and "wsk_pub" in tls_hub.auth_frames[0]  # the first link of a join
    assert [r["o"]["op"] for r in tls_hub.requests] == ["join", "snapshot_get"]  # then the first snapshot
    body = tls_hub.requests[0]["o"]["body"]
    assert body["binding"]["o"]["mode"] == "selective" and body["card"]["o"]["workspace_id"] == served.wid
    status = ok(served.client().req("hub.status", {}, idem=False))["data"]
    assert status["link"] == "bound"
    human.run("hub.leave", {})
    tls_hub.auth_frames.clear()
    out = ok(served.client().req("hub.unbind", {}, idem=True))["data"]  # the address is the one kept in hub.json
    assert out["delivered"] and tls_hub.requests[-1]["o"]["op"] == "binding_put"
    assert tls_hub.auth_frames and "wsk_pub" not in tls_hub.auth_frames[0]  # later links rely on the pinned key
    assert tls_hub.requests[-1]["o"]["body"]["object"]["o"]["kind"] == "hub.unbound"
    assert b"wk_seal" not in bytes(tls_hub.app_bytes)  # no WK material on the wire


def test_a_hub_that_is_not_the_cards_certificate_is_unreachable_not_trusted(human, served, tls_hub):
    card = make_card(tls_hub, now_ms(served))
    card["o"]["tls_spki_sha256"] = crypto.b64u(b"\x01" * 32)  # a pin the hub does not match (card re-signed below)
    from orch.canon.jcs import dumps as cj
    from orch.crypto import suite

    card["sig"] = crypto.b64u(suite.sign(tls_hub.hk_op, wire.SIG_CARD + cj(card["o"])))
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    args = {"invite": "i", "hub_card": card, "addr": f"127.0.0.1:{tls_hub.port}"}
    assert err(served.client().req("hub.connect", args, idem=True)) == "hub.unreachable"
    assert tls_hub.requests == [] and not tls_hub.auth_verified.is_set()  # nothing was sent to it


def test_no_address_means_unreachable_and_a_retry_with_one_resumes(human, served, tls_hub):
    card = make_card(tls_hub, now_ms(served))
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    base = {"invite": "i", "hub_card": card}
    assert err(served.client().req("hub.connect", base, idem=True)) == "hub.unreachable"
    got = ok(served.client().req("hub.connect", {**base, "addr": f"127.0.0.1:{tls_hub.port}"}, idem=True))["data"]
    assert got["status"] == "bound" and hub_binding_seq(served) == 1


def hub_binding_seq(served) -> int:
    store = served.lab.ws.other()
    try:
        return store.state.workspace.hub["binding_seq"]
    finally:
        store.close()


def test_the_env_address_is_a_dev_workspace_override_only(monkeypatch):
    monkeypatch.setenv("ORCH_HUB_ADDR", "10.0.0.9:9")
    assert hubops._addr({"addr": "a.example:1"}, None, True) == "10.0.0.9:9"
    assert hubops._addr({"addr": "a.example:1"}, None, False) == "a.example:1"
    assert hubops._addr({}, None, False) is None


@pytest.mark.parametrize("bad", ["", "nohost", "h:0", "h:65536", "h:x", "a b:1", "h:1/path", "[::1:1", None, 5])
def test_a_bad_address_is_malformed(bad):
    from orch.hubclient import HubRefused

    with pytest.raises(HubRefused):
        parse_addr(bad)
    assert parse_addr("hub.dev.example:8443") == ("hub.dev.example", 8443)
    assert parse_addr("[::1]:9") == ("::1", 9)


# --- C3: the group snapshot is fetched, pinned and readable on the host ---------------------------------------------


def _service(served):
    return next(iter(served.h.host.services.values()))


def test_snapshot_is_fetched_on_connect_refreshed_on_demand_and_roles_are_readable(human, served, monkeypatch):
    hub = fake_for(served)
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: hub)
    card = copy.deepcopy(V["objects"]["card"])
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    got = ok(served.client().req("hub.connect", {"invite": "invite-1", "hub_card": card}, idem=True))["data"]
    assert got["snapshot_version"] == 1
    status = ok(served.client().req("hub.status", {}, idem=False))["data"]
    assert status["snapshot"] == {"version": 1, "roles": {}}
    admin, reader = "11" * 16, "22" * 16
    hub.set_snapshot(served.wid, {admin: "admin", reader: "read"})
    r = ok(served.client().req("hub.refresh", {}, idem=True))["data"]
    assert r["changed"] is True and r["snapshot"]["version"] == 2
    status = ok(served.client().req("hub.status", {}, idem=False))["data"]
    assert status["snapshot"] == {"version": 2, "roles": {admin: "admin", reader: "read"}}
    service = _service(served)
    assert [hubops.hub_role(service, u) for u in (admin, reader, "33" * 16)] == ["admin", "read", None]
    # a replay of the older snapshot is refused and the pin stays
    hub.workspaces[served.wid].snapshot = hub._snapshot(served.wid, 1, {reader: "admin"}, [])  # noqa: SLF001
    assert err(served.client().req("hub.refresh", {}, idem=True)) == "hub.refused"
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["snapshot"]["version"] == 2
    assert hubops.hub_role(service, reader) == "read"


def test_refresh_before_a_connect_is_refused(human, served, monkeypatch):
    hub = fake_for(served)
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: hub)
    assert err(served.client().req("hub.refresh", {}, idem=True)) == "hub.not_joined"
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["snapshot"] is None


def test_a_snapshot_that_does_not_verify_does_not_undo_the_connect(human, served, monkeypatch):
    hub = fake_for(served)
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: hub)
    real = hub._op_snapshot_get  # noqa: SLF001
    monkeypatch.setattr(hub, "_op_snapshot_get", lambda body, ws_id, ws: {"snapshot": {"o": {}, "sig": ""}})
    card = copy.deepcopy(V["objects"]["card"])
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    got = ok(served.client().req("hub.connect", {"invite": "invite-1", "hub_card": card}, idem=True))["data"]
    assert got["status"] == "bound" and got["snapshot_version"] is None
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["snapshot"] is None
    monkeypatch.setattr(hub, "_op_snapshot_get", real)
    assert ok(served.client().req("hub.refresh", {}, idem=True))["data"]["snapshot"]["version"] == 1


def _race_rig(human, served, monkeypatch):
    from orch.hubclient import HubState, snapshot

    hub = fake_for(served)
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: hub)
    card = copy.deepcopy(V["objects"]["card"])
    human.run("hub.join", {"hub_card": card, "mode": "selective"})
    ok(served.client().req("hub.connect", {"invite": "invite-1", "hub_card": card}, idem=True))
    service = _service(served)
    wsk_pub, sign = hubops._wsk_sign(service)  # noqa: SLF001

    def fetch(copy_of_state):
        """One refresh that started from ``copy_of_state`` (loaded earlier), saving through the host's merge."""
        link, transport = hubops._snapshot_link(service, copy_of_state, wsk_pub, sign, True)  # noqa: SLF001
        try:
            return snapshot.refresh(
                state=copy_of_state, link=link, workspace_id=service.workspace_id, dev_mode=True,
                now_ms=lambda: int(service.clock() * 1000),
                save=lambda f: hubops._save_snapshot(service, f),  # noqa: SLF001
            )  # fmt: skip
        finally:
            transport.close() if hasattr(transport, "close") else None

    def started_now():
        return HubState.from_json(hubops._load_state(service).to_json())  # noqa: SLF001

    return hub, service, fetch, started_now


def test_two_racing_refreshes_never_roll_the_pin_back(human, served, monkeypatch):
    hub, service, fetch, started_now = _race_rig(human, served, monkeypatch)
    a, b = started_now(), started_now()  # both read hub.json before either fetched
    hub.set_snapshot(served.wid, {"11" * 16: "admin"})
    fetch(a)  # the slow one's rival finishes with version 2 ...
    hub.set_snapshot(served.wid, {"11" * 16: "admin", "22" * 16: "read"})
    fetch(b)  # ... and this one with version 3
    assert hubops._load_state(service).snapshot["o"]["version"] == 3  # noqa: SLF001
    # now a refresh that fetched version 2 earlier but saves last: it must not win
    hub.workspaces[served.wid].snapshot = hub._snapshot(served.wid, 2, {"11" * 16: "admin"}, [])  # noqa: SLF001
    stale = started_now()
    stale.snapshot = None  # as if it started before any pin
    fetch(stale)  # its own view says "newer" (no pin), but the merge under the lock sees version 3 on disk: no write
    assert hubops._load_state(service).snapshot["o"]["version"] == 3  # noqa: SLF001
    assert hubops.hub_role(service, "22" * 16) == "read"


def test_a_leave_during_a_refresh_is_not_undone(human, served, monkeypatch):
    from orch.hubclient import HubRefused

    hub, service, fetch, started_now = _race_rig(human, served, monkeypatch)
    started = started_now()  # the refresh read a bound state ...
    hub.set_snapshot(served.wid, {"11" * 16: "admin"})
    human.run("hub.leave", {})  # ... and the owner leaves while it is on the network
    assert ok(served.client().req("hub.unbind", {}, idem=True))["data"]["delivered"]
    assert hubops._load_state(service).status == "unbound"  # noqa: SLF001
    with pytest.raises(HubRefused):  # the hub itself says not_bound; if it did not, the merge says not_joined
        fetch(started)
    started.snapshot = hub._snapshot(served.wid, 2, {"11" * 16: "admin"}, [])  # noqa: SLF001
    with pytest.raises(HubRefused):  # the merge alone, as if the hub had still answered
        hubops._save_snapshot(service, started)  # noqa: SLF001
    assert hubops._load_state(service).status == "unbound"  # noqa: SLF001
    assert hubops._load_state(service).snapshot["o"]["version"] == 1  # noqa: SLF001 - nothing from the late refresh
    assert hubops.hub_role(service, "11" * 16) is None  # an unbound workspace has no hub roles


def test_an_equal_card_with_other_content_does_not_overwrite_the_disk(human, served, monkeypatch):
    from orch.hubclient import HubRefused
    from tests.hub.test_snapshot import pin, rotated_card

    hub, service, fetch, started_now = _race_rig(human, served, monkeypatch)
    a, b = started_now(), started_now()
    a.card = rotated_card(spki=pin(2))
    a.hk_op_pub, a.tls_spki = a.card["op_cert"]["o"]["hk_op_pub"], pin(2)
    hubops._save_snapshot(service, a)  # noqa: SLF001
    assert hubops._load_state(service).tls_spki == pin(2)  # noqa: SLF001 - a newer card (op_seq 2) is adopted
    b.card = rotated_card(spki=pin(3))  # same op_seq and card_seq, other bytes
    with pytest.raises(HubRefused) as e:
        hubops._save_snapshot(service, b)  # noqa: SLF001
    assert e.value.code == "stale_card" and hubops._load_state(service).tls_spki == pin(2)  # noqa: SLF001

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


def make_card(hub: TlsHub, now: int) -> dict:
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
        "hxk_pub": crypto.b64u(suite.public_bytes(suite.generate_private_key())), "relay_url": None,
        "tls_spki_sha256": hub.spki, "card_seq": 1, "issued_ms": now - 1000, "dev": True,
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
    assert [r["o"]["op"] for r in tls_hub.requests] == ["join"]
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

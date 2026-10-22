"""V3 C12: the host's peer ops (ws->ws through the hub) on a served workspace joined to a TLS test hub: the card, the
pin, send (a route_out on the wire), the status, and who may do what."""

from __future__ import annotations

import time

import pytest

from orch import crypto
from orch.host import hubops, peerops
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.test_hub_join_real import genesis, make_card, now_ms
from tests.hub.tlshub import TlsHub
from tests.relay.corpus import IDS, priv, pub

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

PEER_WS = IDS["workspace_b"]
PEER_OWNER_PK = crypto.b64u(pub("person_alice.sig"))


def _peer_card() -> dict:
    """Workspace B's v2 card, delegated and signed by the vector keys (owner: person alice)."""
    from orch.crypto import suite
    from orch.hubclient import localcard
    from orch.identity import certs
    from orch.relay.r6.firstcard import default_part

    alice, wsk = priv("person_alice.sig"), priv("wsk_b.sig")
    sign_w = lambda m: suite.sign(wsk, m)  # noqa: E731
    deleg = certs.make_delegation(
        pub("person_alice.sig"), lambda m: suite.sign(alice, m), workspace_id=PEER_WS,
        wsk_pub=pub("wsk_b.sig"), client_hosted=False, issued_ms=1_790_000_000_000,
    )  # fmt: skip
    return localcard.build_card(
        delegation=deleg, owner_pk_pub=pub("person_alice.sig"), workspace_id=PEER_WS, wsk_pub=pub("wsk_b.sig"),
        sign_wsk=sign_w, wxk_pub=suite.public_bytes(priv("wxk_b_1.kx")), wxk_version=1, ck=b"\x01" * 32, card_seq=1,
        issued_ms=1_790_000_000_000, part=default_part("peer"),
    )  # fmt: skip


PEER_CARD = _peer_card()


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


def reply(h, o):
    body = {"state": "queued"} if o["op"] == "route_out" else {}
    return h.reply(o, body=body, ts_ms=o["ts_ms"])


@pytest.fixture
def joined(human, served):
    wsk = crypto.unb64u(genesis(served)["wsk_pub"], 65)
    hub = TlsHub(wsk_pub=wsk, on_request=reply)
    hub.workspace_id = served.wid
    hub.start()
    try:
        card = make_card(hub, now_ms(served))
        human.run("hub.join", {"hub_card": card, "mode": "selective"})
        args = {"invite": "invite-1", "hub_card": card, "addr": f"127.0.0.1:{hub.port}"}
        assert ok(served.client().req("hub.connect", args, idem=True))["data"]["status"] == "bound"
        yield hub
    finally:
        for svc in list(served.h.host.services.values()) if hasattr(served.h.host, "services") else []:
            peerops.stop(svc)
        hub.close()


def req(served, op, args=None, **kw):
    return served.client().req(op, args or {}, idem=op in peerops.NETWORK_OPS or None, **kw)


def test_the_ops_are_declared_and_split_reads_from_writes():
    assert set(peerops.PEER_OPS) >= {"peer.card", "peer.add", "peer.send", "peer.inbox", "peer.accept", "peer.reply"}
    assert peerops.PEER_READS == {"peer.card", "peer.list", "peer.inbox", "peer.status"}


def test_without_a_hub_every_peer_op_is_hub_not_joined(served):
    for op in ("peer.card", "peer.inbox"):
        assert err(req(served, op)) == "hub.not_joined"


def test_pin_send_and_status_over_the_link(served, joined):
    card = ok(req(served, "peer.card"))["data"]["card"]
    assert card["o"]["workspace_id"] == served.wid
    assert err(req(served, "peer.add", {"card": card})) == "invalid.input"  # that is this workspace
    assert err(req(served, "peer.add", {"card": PEER_CARD})) == "invalid.input"  # wrong owner key: does not verify
    pinned = ok(req(served, "peer.add", {"card": PEER_CARD, "owner_pk": PEER_OWNER_PK}))["data"]
    assert pinned["pinned"] and ok(req(served, "peer.list"))["data"]["peers"] == [
        {"workspace_id": pinned["workspace_id"]}
    ]
    sent = ok(req(served, "peer.send", {"to": pinned["workspace_id"], "title": "hello", "body": "b"}))["data"]
    assert sent["state"] == "queued" and len(sent["envelope_sha256"]) == 64
    wire = [r["o"] for r in joined.requests if r["o"]["op"] == "route_out"]
    assert len(wire) == 1 and set(wire[0]["body"]) == {"envelope"}  # the exact signed bytes, nothing else
    st = ok(req(served, "peer.status", {"envelope_id": sent["envelope_id"]}))["data"]
    assert st["status"] == "waiting_on_peer"
    assert err(req(served, "peer.send", {"to": "ee" * 16, "title": "x"})) == "invalid.input"  # not pinned: nothing sent
    assert len([r for r in joined.requests if r["o"]["op"] == "route_out"]) == 1


def test_an_agent_reads_the_inbox_but_never_pins_sends_or_answers(served, joined, human):
    assert ok(req(served, "peer.inbox", grant=served.grant, session=SESSION))["data"] == {"items": []}
    for op, args in (
        ("peer.add", {"card": PEER_CARD, "owner_pk": PEER_OWNER_PK}),
        ("peer.send", {"to": "ee" * 16, "title": "x"}),
        ("peer.accept", {"id": "aa" * 16 + "/" + "bb" * 16}),
        ("peer.reply", {"id": "aa" * 16 + "/" + "bb" * 16, "summary": "x"}),
    ):
        assert err(served.client().req(op, args, grant=served.grant, session=SESSION, idem=True)) == "op.not_allowed"


def test_a_pinned_peer_survives_a_restart_of_the_runtime(served, joined):
    ok(req(served, "peer.add", {"card": PEER_CARD, "owner_pk": PEER_OWNER_PK}))
    for svc in list(served.h.host.services.values()) if hasattr(served.h.host, "services") else []:
        peerops.stop(svc)
    time.sleep(0.1)
    assert len(ok(req(served, "peer.list"))["data"]["peers"]) == 1  # reloaded from hub/mailbox.json (0600)

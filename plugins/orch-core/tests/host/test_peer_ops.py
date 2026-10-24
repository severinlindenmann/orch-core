"""V3 C12: the host's peer ops (ws->ws through the hub) on a served workspace joined to a TLS test hub: the card, the
pin, send (a route_out on the wire), the status, and who may do what."""

from __future__ import annotations

import pytest

from orch import crypto
from orch.host import hubops, peerops
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.test_hub_join_real import genesis, make_card, now_ms
from tests.host.test_hub_ops import keystore  # noqa: F401
from tests.hub.tlshub import TlsHub
from tests.wsproto.corpus import IDS, priv, pub

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

PEER_WS = IDS["workspace_b"]
PEER_OWNER_PK = crypto.b64u(pub("person_alice.sig"))


def _peer_card(wsk_name: str = "wsk_b.sig", wxk_name: str = "wxk_b_1.kx", seq: int = 1) -> dict:
    """Workspace B's v2 card, delegated and signed by the vector keys (owner: person alice)."""
    from orch.crypto import suite
    from orch.hubclient import localcard
    from orch.identity import certs
    from orch.identity.epochs import default_part

    alice, wsk = priv("person_alice.sig"), priv(wsk_name)
    sign_w = lambda m: suite.sign(wsk, m)  # noqa: E731
    deleg = certs.make_delegation(
        pub("person_alice.sig"), lambda m: suite.sign(alice, m), workspace_id=PEER_WS,
        wsk_pub=pub(wsk_name), client_hosted=False, issued_ms=1_790_000_000_000,
    )  # fmt: skip
    return localcard.build_card(
        delegation=deleg, owner_pk_pub=pub("person_alice.sig"), workspace_id=PEER_WS, wsk_pub=pub(wsk_name),
        sign_wsk=sign_w, wxk_pub=suite.public_bytes(priv(wxk_name)), wxk_version=seq, ck=b"\x01" * 32, card_seq=seq,
        issued_ms=1_790_000_000_000, part=default_part("peer"),
    )  # fmt: skip


PEER_CARD = _peer_card()


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)
    human_helpers.dev_hub_stores(monkeypatch)  # #1198 C9: the proof's dev marker vs the store's own


def reply(h, o):
    body = {"state": "queued"} if o["op"] == "route_out" else {}
    return h.reply(o, body=body, ts_ms=o["ts_ms"])


def _hub(served, human, mode):
    wsk = crypto.unb64u(genesis(served)["wsk_pub"], 65)
    hub = TlsHub(wsk_pub=wsk, on_request=reply)
    hub.workspace_id = served.wid
    hub.start()
    card = make_card(hub, now_ms(served))
    human.run("hub.join", {"hub_card": card, "mode": mode})
    args = {"invite": "invite-1", "hub_card": card, "addr": f"127.0.0.1:{hub.port}"}
    assert ok(served.client().req("hub.connect", args, idem=True))["data"]["status"] == "bound"
    return hub


def _stop_all(served, hub):
    for svc in list(served.h.host.services.values()) if hasattr(served.h.host, "services") else []:
        peerops.stop(svc)
    hub.close()


@pytest.fixture
def joined(human, served, keystore):  # noqa: F811
    """Bound in FULL mode (the only mode peers route in)."""
    hub = _hub(served, human, "full")
    try:
        yield hub
    finally:
        _stop_all(served, hub)


@pytest.fixture
def joined_selective(human, served):
    hub = _hub(served, human, "selective")
    try:
        yield hub
    finally:
        _stop_all(served, hub)


def test_selective_workspaces_do_not_route_ws_to_ws(served, joined_selective):
    for op, args in (
        ("peer.inbox", {}),
        ("peer.send", {"to": PEER_WS, "title": "x"}),
        ("peer.accept", {"id": "aa" * 16 + "/" + "bb" * 16}),
    ):
        res = req(served, op, args)
        assert err(res) == "hub.not_joined" and "needs full mode" in res["error"]["message"]
    assert peerops._RUNTIMES == {} and [r for r in joined_selective.requests if r["o"]["op"] == "route_out"] == []


def test_a_selective_workspace_can_still_pin_peers_for_shared_spaces(served, joined_selective):
    assert ok(req(served, "peer.card"))["data"]["card"]["o"]["workspace_id"] == served.wid
    assert ok(_add(served, PEER_CARD))["data"]["how"] == "new"
    assert ok(req(served, "peer.list"))["data"]["peers"] == [{"workspace_id": PEER_WS}]
    assert peerops._RUNTIMES == {}  # pins only: no receive thread, no ws->ws link


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
    assert len(ok(req(served, "peer.list"))["data"]["peers"]) == 1  # reloaded from hub/mailbox.json (0600)


def _rt(served):
    return peerops._RUNTIMES.get((str(served.service.state_dir), str(served.service.workspace_id)))


def test_after_unbind_the_runtime_and_thread_are_gone_and_sends_are_refused(served, joined, human):
    ok(req(served, "peer.add", {"card": PEER_CARD, "owner_pk": PEER_OWNER_PK}))
    rt = _rt(served)
    assert rt is not None and rt.thread.is_alive()
    human.run("hub.leave", {})
    ok(req(served, "hub.unbind"))
    assert _rt(served) is None and not rt.thread.is_alive() and rt.svc._link is None
    assert err(req(served, "peer.send", {"to": PEER_WS, "title": "x"})) == "hub.not_joined"
    assert err(req(served, "peer.inbox")) == "hub.not_joined"
    assert [r for r in joined.requests if r["o"]["op"] == "route_out"] == []


def test_a_rebinding_builds_a_new_runtime_and_closes_the_old_link(served, joined, monkeypatch):
    ok(req(served, "peer.list"))
    old = _rt(served)
    old.svc._ensure()  # the host's one link (shared with the receive thread, never a second dial)
    assert old.svc._link is not None
    real = peerops._hub_state

    def other_hub(service, **kw):
        st = real(service, **kw)
        st.binding_seq += 1
        return st

    monkeypatch.setattr(peerops, "_hub_state", other_hub)
    ok(req(served, "peer.list"))
    new = _rt(served)
    assert new is not old and new.binding_seq == old.binding_seq + 1
    old.thread.join(5)  # the old thread may be the one that retires itself
    assert old.stop.is_set() and not old.thread.is_alive() and old.svc._link is None


def _add(served, card, **kw):
    return req(served, "peer.add", {"card": card, "owner_pk": PEER_OWNER_PK, **kw})


def _pin(served):
    from orch.host import peerops as P

    return dict(_rt(served).svc.mb.peers[PEER_WS]) if _rt(served) else {}, P


def test_a_pin_is_never_replaced_silently(served, joined):
    assert ok(_add(served, PEER_CARD))["data"]["how"] == "new"
    first = dict(_rt(served).svc.mb.peers[PEER_WS])
    assert ok(_add(served, PEER_CARD))["data"]["how"] == "same"  # the identical card again is a no-op
    other_wsk = _peer_card(wsk_name="wsk_intruder.sig", seq=2)
    res = _add(served, other_wsk)  # same owner, another WSK, even with a newer card: conflict
    assert err(res) == "invalid.input" and "--replace" in res["error"]["message"]
    assert _rt(served).svc.mb.peers[PEER_WS] == first
    assert err(_add(served, _peer_card(wxk_name="wxk_a_2.kx", seq=1))) == "invalid.input"  # same seq, other WXK
    assert _rt(served).svc.mb.peers[PEER_WS] == first
    # a newer card of the same keys' owner and WSK rotates the WXK
    assert ok(_add(served, _peer_card(wxk_name="wxk_a_2.kx", seq=2)))["data"]["how"] == "updated"
    assert _rt(served).svc.mb.peers[PEER_WS]["card_seq"] == 2
    assert err(_add(served, PEER_CARD)) == "invalid.input"  # an older card is refused
    assert _rt(served).svc.mb.peers[PEER_WS]["card_seq"] == 2


def test_replace_is_explicit_and_human_only(served, joined):
    ok(_add(served, PEER_CARD))
    changed = _peer_card(wsk_name="wsk_intruder.sig", seq=2)
    assert ok(_add(served, changed, replace=True))["data"]["how"] == "replaced"
    assert _rt(served).svc.mb.peers[PEER_WS]["card_seq"] == 2
    res = served.client().req(
        "peer.add",
        {"card": PEER_CARD, "owner_pk": PEER_OWNER_PK, "replace": True},
        grant=served.grant,
        session=SESSION,
        idem=True,
    )
    assert err(res) == "op.not_allowed"

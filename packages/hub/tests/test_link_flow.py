"""The positive path over a real TLS link: auth, join (TOFU pin), card_put, binding_put, snapshot, leave."""

from __future__ import annotations

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.link import LinkError

WSK_B64 = G.K.b("wsk_a")


def test_join_card_binding_snapshot_leave(hub):
    c = hub.open(first=True)
    assert c.request("join", hub.join_body()) == {}
    pin = hub.registry.pin(G.WS_A)
    assert pin["wsk_pub"] == WSK_B64 and pin["owner_person_id"] == G.P_ALICE
    assert hub.registry.mode(G.WS_A) == "selective"
    assert "inv1" not in hub.invites.codes  # consumed

    snap = c.request("snapshot_get", {})["snapshot"]
    H.verify_snapshot(
        snap,
        H.verify_card(G.card(), now_ms=NOW_, dev_mode=True, pinned_root=G.K.pub["hk_root"]),
        workspace_id=G.WS_A,
        dev_mode=True,
        pinned=None,
    )

    card = G.v2_card()
    assert c.request("card_put", {"card": card, "pk_pub": b64u(G.K.pub["pk_alice"])}) == {}
    # a newer card replaces, an older one is stale
    card2 = G.v2_card(card_seq=2)
    assert c.request("card_put", {"card": card2, "pk_pub": b64u(G.K.pub["pk_alice"])}) == {}
    with pytest.raises(LinkError) as e:
        c.request("card_put", {"card": card, "pk_pub": b64u(G.K.pub["pk_alice"])})
    assert e.value.code == "stale_card"

    # binding_put: a mode switch with a higher seq
    assert c.request("binding_put", {"object": G.binding(seq=2, mode="full")}) == {}
    assert hub.registry.mode(G.WS_A) == "full"

    # leave: hub.unbound ends it; the binding never resurrects; snapshots stop
    assert c.request("binding_put", {"object": G.unbound(seq=2)}) == {}
    assert hub.registry.binding_state(G.WS_A)["status"] == "unbound"
    for seq in (1, 2):
        with pytest.raises(LinkError) as e:
            c.request("binding_put", {"object": G.binding(seq=seq)})
        assert e.value.code == "unbound"
    with pytest.raises(LinkError) as e:
        c.request("snapshot_get", {})
    assert e.value.code == "not_bound"


NOW_ = G.NOW


def test_second_link_uses_pinned_wsk(hub):
    hub.open(first=True).request("join", hub.join_body())
    c2 = hub.open()  # no wsk_pub: the hub takes the pinned key
    assert "snapshot" in c2.request("snapshot_get", {})


def test_rejoin_after_leave_with_higher_seq(hub):
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    c.request("binding_put", {"object": G.unbound(seq=1)})
    with pytest.raises(LinkError) as e:
        c.request("join", hub.join_body("inv2", seq=1))
    assert e.value.code == "unbound"
    assert c.request("join", hub.join_body("inv3", seq=2)) == {}
    assert hub.registry.binding_state(G.WS_A)["status"] == "bound"


def test_publish_out_goes_to_forwarder(make_hub):
    seen = []

    class Fwd:
        def forward(self, ws, request):
            seen.append((ws, request))
            return {"status": 200, "body_hex": ""}

    h = make_hub(publish=Fwd())
    c = h.open(first=True)
    c.request("join", h.join_body())
    req = {"method": "PUT", "host": "pub.dev.severin.io", "target": "/x", "headers": {}, "body_hex": ""}
    assert c.request("publish_out", {"request": req}) == {"status": 200, "body_hex": ""}
    assert seen == [(G.WS_A, req)]
    c.request("binding_put", {"object": G.unbound()})
    with pytest.raises(LinkError) as e:
        c.request("publish_out", {"request": req})
    assert e.value.code == "not_bound"


def test_publish_out_without_forwarder_is_unavailable(hub):
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    req = {"method": "PUT", "host": "h", "target": "/x", "headers": {}, "body_hex": ""}
    with pytest.raises(LinkError) as e:
        c.request("publish_out", {"request": req})
    assert e.value.code == "unavailable"

"""The positive path over a real TLS link: auth, join (TOFU pin), card_put, binding_put, snapshot, leave."""

from __future__ import annotations

import pytest

from orch_hub import settings as SETT
from orch_hub.contract import hub as H
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.contract.where import check_hub_where
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


# --- M4 S2 (#1461): hub_where (protocol 10.9) --------------------------------------------------------------------------


WHERE_FILE = {
    "v": 1,
    "name": "Acme Hub",
    "company": "Acme AG",
    "addresses": [
        {"kind": "private", "web": "http://100.64.1.2:8100", "link": "100.64.1.2:8105"},
        {"kind": "server", "web": "http://127.0.0.1:8100", "link": "127.0.0.1:8105"},
        {"kind": "office", "web": "http://10.0.0.7:8100", "link": "10.0.0.7:8105"},
        {"kind": "public", "web": "https://hub.example.com:8100", "link": None},
    ],
}


def _where_hub(make_hub, tmp_path, settings=WHERE_FILE):
    if settings is not None:
        SETT.save(tmp_path, settings)
    return make_hub(where=lambda: SETT.where_answer(tmp_path))


def test_hub_where_answers_the_validated_shape_to_a_bound_workspace(make_hub, tmp_path):
    h = _where_hub(make_hub, tmp_path)
    c = h.open(first=True)
    c.request("join", h.join_body())
    got = c.request("hub_where", {})
    assert set(got) == {"addresses", "name"} and got["name"] == "Acme Hub"
    assert got["addresses"] == [  # only entries that have a link address, file order, kind + link and nothing else
        {"kind": "private", "link": "100.64.1.2:8105"},
        {"kind": "server", "link": "127.0.0.1:8105"},
        {"kind": "office", "link": "10.0.0.7:8105"},
    ]
    assert check_hub_where(got) == got and len(got["addresses"]) <= 4
    c.request("binding_put", {"object": G.binding(seq=2, mode="full")})  # both modes are answered
    assert c.request("hub_where", {}) == got


def test_hub_where_reads_the_file_each_time_and_takes_no_request_member(make_hub, tmp_path):
    h = _where_hub(make_hub, tmp_path)
    c = h.open(first=True)
    c.request("join", h.join_body())
    SETT.save(tmp_path, {**WHERE_FILE, "name": "Beta", "addresses": WHERE_FILE["addresses"][:1]})
    assert c.request("hub_where", {}) == {"addresses": [{"kind": "private", "link": "100.64.1.2:8105"}], "name": "Beta"}
    with pytest.raises(LinkError) as e:  # the request is exactly `{}`: nothing a peer sends reaches the answer
        c.request("hub_where", {"addresses": ["6.6.6.6:1"]})
    assert e.value.code == "malformed"


def test_hub_where_is_for_bound_workspaces_only(make_hub, tmp_path):
    h = _where_hub(make_hub, tmp_path)
    c = h.open(first=True)
    with pytest.raises(LinkError) as e:  # a link that has not joined may only join
        c.request("hub_where", {})
    assert e.value.code == "unknown_workspace"
    h2 = _where_hub(make_hub, tmp_path)
    c2 = h2.open(first=True)
    c2.request("join", h2.join_body())
    c2.request("binding_put", {"object": G.unbound(seq=1)})  # left the hub
    with pytest.raises(LinkError) as e:
        c2.request("hub_where", {})
    assert e.value.code == "not_bound"


def test_hub_where_is_unavailable_without_a_settings_file_or_a_provider(make_hub, tmp_path):
    for hub in (_where_hub(make_hub, tmp_path, None), make_hub()):
        c = hub.open(first=True)
        c.request("join", hub.join_body())
        with pytest.raises(LinkError) as e:
            c.request("hub_where", {})
        assert e.value.code == "unavailable"


def test_hub_where_is_a_cheap_read_under_the_link_caps(make_hub, tmp_path):
    # like the other read ops there is no per-op budget: the hub-wide caps (64 links, 16 unauthenticated, frame size and
    # idle timeouts) bound it, and one answer is a file read plus a check of at most 4 entries
    h = _where_hub(make_hub, tmp_path)
    c = h.open(first=True)
    c.request("join", h.join_body())
    assert all(c.request("hub_where", {})["name"] == "Acme Hub" for _ in range(50))
    assert len(c.request("hub_where", {})["addresses"]) <= 4

"""#1198 C9, protocol 9.6 "At the hub": a link ``join`` and a ``binding_put`` that is new or switches the mode carry an
owner proof signed by the owner's ``PK``; the hub refuses without it, whatever the host (or a taken-over dashboard)
sends. The conftest client adds a proof when a test names none; these tests name theirs."""

from __future__ import annotations

import pytest
from conftest import _orig_request

from orch_hub.contract import vectorgen as G
from orch_hub.link import LinkError

PAY = {**G.JOINED_PAYLOAD}
FULL2 = {**PAY, "mode": "full", "binding_seq": 2, "aliases": []}


def refused(c, op, body, code):
    with pytest.raises(LinkError) as e:
        c.request(op, body)
    assert e.value.code == code


def body(hub, proof, **kw):
    return {**hub.join_body(**kw), "owner_proof": proof}


def test_join_with_the_owners_proof(hub):
    c = hub.open(first=True)
    assert c.request("join", body(hub, G.owner_proof())) == {}


def test_join_without_a_proof_is_proof_missing(hub):
    c = hub.open(first=True)
    refused(c, "join", body(hub, None), "proof_missing")
    assert hub.registry.pin(G.WS_A) is None  # nothing pinned, the invite is not consumed
    assert "inv1" in hub.invites.codes


def test_join_without_the_member_is_malformed(hub):
    c = hub.open(first=True)
    with pytest.raises(LinkError) as e:  # the original client call: the conftest adds no proof
        _orig_request(c, "join", hub.join_body())
    assert e.value.code == "malformed"


@pytest.mark.parametrize(
    "proof",
    [
        G.owner_proof(pk="pk_mallory"),  # another person's key
        G.owner_proof(workspace_id=G.WS_B),  # another workspace
        G.owner_proof("hub.mode", {**PAY, "mode": "full"}),  # another op
        G.owner_proof(payload={**PAY, "binding_seq": 2}),  # another binding_seq
        G.owner_proof(label="sig_hub_binding"),  # another label
    ],
)
def test_join_with_a_wrong_proof_is_proof_invalid(hub, proof):
    c = hub.open(first=True)
    refused(c, "join", body(hub, proof), "proof_invalid")
    assert hub.registry.pin(G.WS_A) is None


def test_join_with_an_expired_proof(hub):
    c = hub.open(first=True)
    refused(c, "join", body(hub, G.owner_proof(issued_ms=G.NOW - 700_000, expires_ms=G.NOW - 100_000)), "proof_expired")


def test_mode_switch_needs_the_proof(hub):
    c = hub.open(first=True)
    c.request("join", body(hub, G.owner_proof()))
    sw = {"object": G.binding(seq=2, mode="full")}
    refused(c, "binding_put", {**sw, "owner_proof": None}, "proof_missing")
    assert hub.registry.mode(G.WS_A) == "selective"
    refused(c, "binding_put", {**sw, "owner_proof": G.owner_proof("hub.join", FULL2)}, "proof_invalid")  # wrong op
    assert c.request("binding_put", {**sw, "owner_proof": G.owner_proof("hub.mode", FULL2)}) == {}
    assert hub.registry.mode(G.WS_A) == "full"


def test_a_rejoin_after_leave_through_binding_put_needs_the_proof(hub):
    c = hub.open(first=True)
    c.request("join", body(hub, G.owner_proof()))
    assert c.request("binding_put", {"object": G.unbound(seq=1), "owner_proof": None}) == {}  # a leave takes none
    rejoin = {**PAY, "binding_seq": 2}
    obj = G.binding(seq=2)
    refused(c, "binding_put", {"object": obj, "owner_proof": None}, "proof_missing")
    assert c.request("binding_put", {"object": obj, "owner_proof": G.owner_proof(payload=rejoin)}) == {}


def test_an_identical_binding_re_sent_takes_no_proof(hub):
    c = hub.open(first=True)
    c.request("join", body(hub, G.owner_proof()))
    assert c.request("binding_put", {"object": G.binding(seq=1), "owner_proof": None}) == {}

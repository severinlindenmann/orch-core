"""The in-process fake hub (``fake.py``): a round trip for every op it serves, and the negative cases.

Replies are checked the way the host must check them: signed by the card's ``HK_op`` under ``orch/v2/hub-route|``.
"""

from __future__ import annotations

import base64
import json

import pytest

from orch.crypto import suite as S
from orch.crypto.encoding import b64u, unb64u

from . import contract as H
from .fake import TEST_EXPORTER, FakeHub, HostLink, HubError, owner_proof_for, sign_with, vector_join_body
from .vec import V, private_key, pub

WS = V["ids"]["workspace_a"]
NOW = V["now_ms"]


def binding(seq: int, mode: str = "selective", **over):
    o = {**V["objects"]["binding_a_1"]["o"], "binding_seq": seq, "mode": mode, **over}
    return sign_with(private_key("wsk_a"), "sig_hub_binding", o)


def unbound(seq: int, **over):
    o = {**V["objects"]["unbound_a_1"]["o"], "binding_seq": seq, **over}
    return sign_with(private_key("wsk_a"), "sig_hub_unbound", o)


def code(link: HostLink, op: str, body, **kw) -> str:
    with pytest.raises(HubError) as e:
        link.call(op, body, **kw)
    return e.value.code


# --- the hub card and replies -------------------------------------------------------------------------------------


def test_the_hub_card_is_the_vector_card_and_verifies_for_a_dev_host(hub: FakeHub):
    held = H.verify_card(hub.card, now_ms=NOW, dev_mode=True, pinned_root=pub("hk_root"))
    assert held["card"]["hub_id"] == hub.hub_id == V["ids"]["hub"]


def test_every_reply_passes_the_host_side_link_check(joined: HostLink, hub: FakeHub):
    msg = joined.message("snapshot_get", {})
    reply = hub.send(msg)
    o = H.verify_link(
        reply,
        direction="h2w",
        hub_id_=hub.hub_id,
        workspace_id=WS,
        mode="selective",
        pub=hub.hk_op_pub,
        now_ms=NOW,
        exporter=TEST_EXPORTER,
    )
    assert o["re"] == msg["o"]["seq"] and o["op"] == "snapshot_get"


def test_a_reply_signed_by_another_key_is_refused_by_the_host(joined: HostLink, hub: FakeHub):
    msg = joined.message("snapshot_get", {})
    joined.hk_op_pub = pub("hk_op_rogue")
    with pytest.raises(H.Refused) as e:
        joined.open_reply(hub.send(msg), msg)
    assert e.value.code == "bad_signature"


# --- join (section 9.5) -------------------------------------------------------------------------------------------


def test_join_pins_the_workspace_and_binds_it(hub: FakeHub, link: HostLink):
    assert link.join() == {}
    ws = hub.workspaces[WS]
    assert ws.mode == "selective" and ws.binding["status"] == "bound" and ws.binding["binding_seq"] == 1
    assert hub.pins[WS]["owner_person_id"] == V["ids"]["person_alice"]
    assert hub.published_cards[WS] == V["objects"]["v2_card_a"]


def test_join_is_repeatable_for_the_same_binding(joined: HostLink):
    assert joined.join() == {}


def test_join_without_card_or_pk_is_unknown_workspace(link: HostLink):
    body = vector_join_body()
    del body["card"]
    assert code(link, "join", body) == "unknown_workspace"
    body = vector_join_body()
    del body["pk_pub"]
    assert code(link, "join", body) == "malformed"  # the table's exact field set (step 6) comes first for pk_pub


def test_join_with_another_owners_pk_is_other_person(link: HostLink):
    body = {**vector_join_body(), "pk_pub": V["keys"]["pk_mallory"]["pub"]}
    assert code(link, "join", body) == "other_person"


def test_join_with_a_tampered_binding_is_bad_signature(link: HostLink):
    b = json.loads(json.dumps(V["objects"]["binding_a_1"]))
    b["o"]["valid_from_ms"] += 1
    assert code(link, "join", {**vector_join_body(), "binding": b}) == "bad_signature"


def test_join_binding_for_another_hub_is_refused(link: HostLink):
    o = {**V["objects"]["binding_a_1"]["o"]}
    o["hk_root_pub"] = V["keys"]["hk_root_rogue"]["pub"]
    o["hub_id"] = H.hub_id(pub("hk_root_rogue"))
    body = {**vector_join_body(), "binding": sign_with(private_key("wsk_a"), "sig_hub_binding", o)}
    assert code(link, "join", body) == "other_hub"


def test_join_with_a_real_binding_is_dev_mismatch(link: HostLink):
    b = binding(1, dev=False)
    assert code(link, "join", {**vector_join_body(), "binding": b}) == "dev_mismatch"


def test_a_real_hub_refuses_a_dev_workspace(link: HostLink):
    real = FakeHub(dev_mode=False)
    assert code(HostLink(real), "join", vector_join_body()) == "dev_refused"


def test_invites_bound_to_another_workspace_are_refused(link: HostLink):
    hub = FakeHub(invites={"inv": V["ids"]["workspace_b"], "any": None})
    host = HostLink(hub)
    assert code(host, "join", vector_join_body("nope")) == "bad_invite"
    assert code(host, "join", vector_join_body("inv")) == "other_workspace"
    assert host.join("any") == {}


def test_first_come_pin_refuses_a_second_owner_and_a_second_wsk(joined: HostLink, hub: FakeHub):
    # another owner's PK against the pinned workspace (the vector hub_join cases cover a full squat join)
    squat = hub.pins[WS]
    assert squat["owner_person_id"] == V["ids"]["person_alice"]
    body = {**vector_join_body(), "pk_pub": V["keys"]["pk_mallory"]["pub"]}
    assert code(joined, "join", body) == "other_person"
    assert hub.pins[WS] == squat


def test_join_with_stale_timestamp_is_refused_and_pins_nothing(hub: FakeHub, link: HostLink):
    assert code(link, "join", vector_join_body(), ts_ms=NOW - 121_000) == "stale_timestamp"
    assert hub.pins == {}


# --- link rules (section 10.2) --------------------------------------------------------------------------------------


def test_unknown_workspace_before_join(link: HostLink):
    assert code(link, "snapshot_get", {}) == "unknown_workspace"


def test_unknown_op_and_hub_to_workspace_ops_from_the_workspace(joined: HostLink):
    assert code(joined, "sign_something", {}) == "unknown_op"
    assert joined.request("sign_something", {})["op"] == "error"
    assert code(joined, "route_in", {"envelope": "AA"}) == "unknown_op"
    assert code(joined, "event_deliver", {"log": "workspace", "event": {}}) == "unknown_op"


def test_wk_seal_is_wrong_mode_in_selective(joined: HostLink, hub: FakeHub):
    assert code(joined, "wk_seal", {"epoch": 1, "wrap": "AA"}) == "wrong_mode"
    assert hub.workspaces[WS].wk_seals == []


def test_share_ops_work_in_both_modes(joined: HostLink, hub: FakeHub):
    item = "11" * 16
    assert joined.call("share_put", {"item_id": item, "sealed": "AAAA"}) == {}
    assert hub.workspaces[WS].shares == {item: "AAAA"}
    assert joined.call("share_revoke", {"item_id": item}) == {}
    assert hub.workspaces[WS].shares == {}
    assert joined.call("binding_put", {"object": binding(2, "full")}) == {}
    assert joined.call("share_put", {"item_id": item, "sealed": "AAAA"}) == {}


def test_full_mode_takes_wk_seal_and_audit_ack(joined: HostLink, hub: FakeHub):
    joined.call("binding_put", {"object": binding(2, "full")})
    assert hub.workspaces[WS].mode == "full"
    from orch import crypto

    wrap = crypto.seal(pub("hxk"), bytes.fromhex(V["ids"]["hub"]), "wk", bytes.fromhex(WS), 1, bytes(32))
    assert joined.call("wk_seal", {"epoch": 1, "wrap": b64u(wrap)}) == {}
    assert hub.workspaces[WS].wk == {1: bytes(32)}  # the hub opened it with HXK
    assert joined.call("audit_ack", {"seq": 3, "head": b64u(bytes(32))}) == {}
    assert hub.workspaces[WS].wk_seals[0]["epoch"] == 1 and hub.workspaces[WS].audit[0]["seq"] == 3


def test_route_out_is_recorded(joined: HostLink, hub: FakeHub):
    assert joined.call("route_out", {"envelope": "AAAA"}) == {}
    assert hub.workspaces[WS].routed == ["AAAA"]


@pytest.mark.parametrize(
    ("body", "want"),
    [({"x": 1}, "malformed"), ({"item_id": "11" * 16}, "malformed")],
)
def test_bodies_must_match_the_table_exactly(joined: HostLink, body, want):
    assert code(joined, "share_put", body) == want


def test_stale_and_future_timestamps_are_refused(joined: HostLink):
    assert code(joined, "snapshot_get", {}, ts_ms=NOW - 121_000) == "stale_timestamp"
    assert code(joined, "snapshot_get", {}, ts_ms=NOW + 121_000) == "stale_timestamp"


def test_message_for_another_hub_or_workspace(joined: HostLink, hub: FakeHub):
    msg = joined.message("snapshot_get", {})
    o = {**msg["o"], "hub_id": V["ids"]["hub_rogue"]}
    assert hub.send(sign_with(private_key("wsk_a"), "sig_hub_link", o))["o"]["body"] == {"error": "other_hub"}
    o = {**msg["o"], "workspace_id": V["ids"]["workspace_b"]}
    assert hub.send(sign_with(private_key("wsk_a"), "sig_hub_link", o))["o"]["body"] == {"error": "unknown_workspace"}


def test_a_message_signed_by_another_wsk_is_bad_signature(joined: HostLink, hub: FakeHub):
    msg = joined.message("snapshot_get", {})
    forged = sign_with(private_key("wsk_b"), "sig_hub_link", msg["o"])
    assert hub.send(forged)["o"]["body"] == {"error": "bad_signature"}


def test_a_message_under_the_route_label_is_bad_signature(joined: HostLink, hub: FakeHub):
    msg = joined.message("snapshot_get", {})
    wrong = sign_with(private_key("wsk_a"), "sig_hub_route", msg["o"])
    assert hub.send(wrong)["o"]["body"] == {"error": "bad_signature"}


def test_non_canonical_text_is_malformed(hub: FakeHub):
    reply = hub.send(b'{"o": {"a": 1, "a": 2}, "sig": ""}')
    assert reply["o"]["body"] == {"error": "malformed"}
    assert hub.send({"nope": 1})["o"]["body"] == {"error": "malformed"}


def test_link_auth_under_the_pinned_wsk(joined: HostLink, hub: FakeHub):
    a = V["link_auth"]
    hub.authenticate(WS, bytes.fromhex(a["challenge_hex"]), bytes.fromhex(a["exporter_hex"]), a["sig"])
    with pytest.raises(H.Refused) as e:
        hub.authenticate(WS, bytes.fromhex(a["challenge_hex"]), bytes(32), a["sig"])
    assert e.value.code == "bad_signature"
    with pytest.raises(H.Refused) as e:
        hub.authenticate(V["ids"]["workspace_b"], bytes(32), bytes(32), a["sig"])
    assert e.value.code == "unknown_workspace"
    assert len(hub.challenge()) == 32


def test_hub_pushes_signed_h2w_requests(joined: HostLink, hub: FakeHub):
    msg = hub.push(WS, "route_in", {"envelope": "AAAA"})
    o = H.verify_link(
        msg,
        direction="h2w",
        hub_id_=hub.hub_id,
        workspace_id=WS,
        mode="selective",
        pub=hub.hk_op_pub,
        now_ms=NOW,
        exporter=TEST_EXPORTER,
    )
    assert o["re"] is None and o["seq"] == 1
    assert hub.push(WS, "route_in", {"envelope": "AAAA"})["o"]["seq"] == 2


# --- snapshot_get -------------------------------------------------------------------------------------------------


def test_snapshot_get_verifies_on_the_host_and_versions_are_monotonic(joined: HostLink, hub: FakeHub):
    held = H.verify_card(hub.card, now_ms=NOW, dev_mode=True)
    first = joined.call("snapshot_get", {})["snapshot"]
    o1 = H.verify_snapshot(first, held, workspace_id=WS, dev_mode=True, pinned=None)
    assert o1["version"] == 1 and o1["roles"] == {}
    hub.set_snapshot(WS, {V["ids"]["user_alice"]: "admin"}, [V["ids"]["channel_alice_phone"]])
    second = joined.call("snapshot_get", {})["snapshot"]
    o2 = H.verify_snapshot(second, held, workspace_id=WS, dev_mode=True, pinned=o1)
    assert o2["version"] == 2 and o2["roles"] == {V["ids"]["user_alice"]: "admin"}
    with pytest.raises(H.Refused) as e:  # a replayed older snapshot cannot bring a removed user back
        H.verify_snapshot(first, held, workspace_id=WS, dev_mode=True, pinned=o2)
    assert e.value.code == "stale_snapshot"
    with pytest.raises(H.Refused) as e:
        H.verify_snapshot(second, held, workspace_id=V["ids"]["workspace_b"], dev_mode=True, pinned=None)
    assert e.value.code == "other_workspace"


# --- card_put -----------------------------------------------------------------------------------------------------


def next_card(seq: int, **over):
    """A v2 card_seq N+1 re-signed by WSK (the sealed part stays, so sealed_hash still matches)."""
    c = json.loads(json.dumps(V["objects"]["v2_card_a"]))
    c["o"] = {**c["o"], "card_seq": seq, **over}
    c["sig"] = b64u(S.sign(private_key("wsk_a"), H.L["sig_card_wsk"] + H.cj(c["o"])))
    return c


def test_card_put_replaces_the_card_for_a_higher_seq_only(joined: HostLink, hub: FakeHub):
    pk = V["keys"]["pk_alice"]["pub"]
    c2 = next_card(2)
    assert joined.call("card_put", {"card": c2, "pk_pub": pk}) == {}
    assert hub.published_cards[WS] == c2
    assert code(joined, "card_put", {"card": V["objects"]["v2_card_a"], "pk_pub": pk}) == "stale_card"
    assert code(joined, "card_put", {"card": next_card(2, relay_url=None), "pk_pub": pk}) == "stale_card"
    assert joined.call("card_put", {"card": c2, "pk_pub": pk}) == {}  # identical re-send is a no-op


def test_card_put_checks_owner_and_signatures(joined: HostLink, hub: FakeHub):
    c = V["objects"]["v2_card_a"]
    assert code(joined, "card_put", {"card": c, "pk_pub": V["keys"]["pk_mallory"]["pub"]}) == "other_person"
    bad = json.loads(json.dumps(c))
    bad["o"]["card_seq"] = 5
    assert code(joined, "card_put", {"card": bad, "pk_pub": V["keys"]["pk_alice"]["pub"]}) == "bad_signature"
    assert hub.published_cards[WS] == c


def test_card_put_before_join_is_unknown_workspace(link: HostLink):
    assert code(link, "card_put", {"card": next_card(2), "pk_pub": V["keys"]["pk_alice"]["pub"]}) == "unknown_workspace"


# --- binding_put: modes, leave, never resurrect --------------------------------------------------------------------


def test_binding_put_rebind_conflict_and_stale(joined: HostLink, hub: FakeHub):
    assert joined.call("binding_put", {"object": binding(1)}) == {}  # identical: no-op
    assert code(joined, "binding_put", {"object": binding(1, valid_from_ms=1)}) == "binding_conflict"
    assert joined.call("binding_put", {"object": binding(3, "full")}) == {}
    assert hub.workspaces[WS].mode == "full"
    assert code(joined, "binding_put", {"object": binding(2)}) == "stale_binding"
    assert [o["o"]["binding_seq"] for _, o in hub.forwarded_bindings] == [1, 3]


def test_binding_put_checks_signer_workspace_hub_and_kind(joined: HostLink):
    other_key = sign_with(private_key("wsk_b"), "sig_hub_binding", binding(2)["o"])
    assert code(joined, "binding_put", {"object": other_key}) == "bad_signature"
    other_ws = binding(2, workspace_id=V["ids"]["workspace_b"])
    assert code(joined, "binding_put", {"object": other_ws}) == "other_workspace"
    rogue = binding(2, hub_id=H.hub_id(pub("hk_root_rogue")), hk_root_pub=V["keys"]["hk_root_rogue"]["pub"])
    assert code(joined, "binding_put", {"object": rogue}) == "other_hub"
    assert code(joined, "binding_put", {"object": V["objects"]["card"]}) == "malformed"
    assert code(joined, "binding_put", {"object": {"o": 1, "sig": ""}}) == "malformed"


def test_a_hub_signed_unbound_is_refused(joined: HostLink, hub: FakeHub):
    forged = sign_with(private_key("hk_op"), "sig_hub_unbound", unbound(1)["o"])
    assert code(joined, "binding_put", {"object": forged}) == "bad_signature"
    assert hub.workspaces[WS].binding["status"] == "bound"


def test_unbound_ends_the_binding_and_it_never_comes_back(joined: HostLink, hub: FakeHub):
    assert joined.call("binding_put", {"object": unbound(1)}) == {}
    assert hub.workspaces[WS].binding["status"] == "unbound"
    assert code(joined, "binding_put", {"object": binding(1)}) == "unbound"  # replayed binding after leave
    assert code(joined, "snapshot_get", {}) == "not_bound"
    assert code(joined, "publish_out", {"request": {}}) == "not_bound"
    assert code(joined, "join", vector_join_body()) == "unbound"  # a replayed join too
    assert joined.call("binding_put", {"object": binding(2)}) == {}  # a rejoin at a higher seq
    assert joined.call("snapshot_get", {})["snapshot"]["o"]["version"] == 1


def test_unbound_for_another_hub_and_stale(joined: HostLink):
    assert code(joined, "binding_put", {"object": unbound(1, hub_id=V["ids"]["hub_rogue"])}) == "other_hub"
    joined.call("binding_put", {"object": binding(4)})
    assert code(joined, "binding_put", {"object": unbound(2)}) == "stale_binding"


# --- publish_out --------------------------------------------------------------------------------------------------


def vector_publish_request(**hdr):
    r = json.loads(json.dumps(V["publish"]["request"]))
    r.pop("access")  # route metadata of orch-publish, not a member of the section 10.2 request
    # the host's header set only (11.1): the vector is the forwarded request, so the hub's two headers come off
    r["headers"] = {k: v for k, v in r["headers"].items() if k not in ("Orch-Hub", "Orch-Hub-Signature")} | hdr
    return r


def test_publish_out_forwards_unchanged_plus_a_verifiable_hub_signature(joined: HostLink, hub: FakeHub):
    req = vector_publish_request()
    assert joined.call("publish_out", {"request": req}) == {"status": 200, "body_hex": ""}
    (fwd,) = hub.forwarded
    assert fwd.workspace_id == WS and fwd.at_ms == NOW
    sent = {k: v for k, v in fwd.request["headers"].items() if k not in ("Orch-Hub", "Orch-Hub-Signature")}
    assert fwd.request["headers"]["Orch-Hub"] == hub.hub_id
    assert sent == req["headers"] and fwd.request["body_hex"] == req["body_hex"]
    # what orch-publish checks (section 11.2 step 7): the hub signature over the vector's hub-path WSK bytes
    wsk_bytes = bytes.fromhex(V["publish"]["wsk_signed_hex"])
    msg = H.hub_publish_bytes(hub.hub_id, wsk_bytes)
    assert msg == bytes.fromhex(V["publish"]["hub_signed_hex"])
    assert S.verify(hub.hk_op_pub, unb64u(fwd.request["headers"]["Orch-Hub-Signature"], 64), msg)


def test_publish_out_uses_the_pluggable_responder(joined: HostLink, hub: FakeHub):
    seen = []

    def responder(fwd):
        seen.append(fwd.request["target"])
        return {"status": 403, "body_hex": b"no".hex()}

    hub.responder = responder
    assert joined.call("publish_out", {"request": vector_publish_request()}) == {"status": 403, "body_hex": "6e6f"}
    assert seen == [V["publish"]["request"]["target"]]
    hub.responder = lambda fwd: {"status": "200"}
    assert code(joined, "publish_out", {"request": vector_publish_request()}) == "malformed"


def test_publish_out_refusals(joined: HostLink, hub: FakeHub):
    assert code(joined, "publish_out", {"request": {"method": "PUT"}}) == "malformed"
    bad = vector_publish_request()
    del bad["headers"]["Orch-Nonce"]
    assert code(joined, "publish_out", {"request": bad}) == "malformed"
    for hub_header in ("Orch-Hub", "Orch-Hub-Signature"):  # the hub's headers are the hub's alone (#981)
        host_sent = vector_publish_request(**{hub_header: V["ids"]["hub"]})
        assert code(joined, "publish_out", {"request": host_sent}) == "malformed"
    assert code(joined, "publish_out", {"request": vector_publish_request(**{"Orch-Workspace": "00" * 16})}) == (
        "other_workspace"
    )
    tampered = vector_publish_request()
    tampered["target"] += "x"
    assert code(joined, "publish_out", {"request": tampered}) == "bad_signature"
    stripped = vector_publish_request()
    del stripped["headers"]["Orch-Owner-Approval"]  # a middle party stripping the approval breaks the WSK signature
    assert code(joined, "publish_out", {"request": stripped}) == "bad_signature"
    stale = vector_publish_request(**{"Orch-Ts": str(NOW - 500_000)})
    assert code(joined, "publish_out", {"request": stale}) == "stale_timestamp"
    assert hub.forwarded == []


def test_publish_out_for_a_clock_that_moved_is_stale(joined: HostLink, hub: FakeHub):
    hub.now_ms += 121_000
    assert code(joined, "publish_out", {"request": vector_publish_request()}, ts_ms=hub.now_ms) == "stale_timestamp"


def test_call_log_records_every_op_and_result(joined: HostLink, hub: FakeHub):
    joined.call("snapshot_get", {})
    code(joined, "wk_seal", {"epoch": 1, "wrap": "AA"})
    assert hub.calls[-2:] == [(WS, "snapshot_get", "ok"), (WS, "wk_seal", "wrong_mode")]
    assert base64.urlsafe_b64decode("AAAA") == b"\0\0\0"


# --- tightening: invites, request shape, card order on re-join, authenticate, error ops -------------------------------


def test_invites_are_single_use(hub: FakeHub):
    hub = FakeHub(invites={"once": None})
    host = HostLink(hub)
    assert host.join("once") == {}
    assert code(host, "join", vector_join_body("once")) == "bad_invite"
    assert hub.invites == {}


def test_a_failed_join_does_not_consume_the_invite():
    hub = FakeHub(invites={"once": None})
    host = HostLink(hub)
    assert code(host, "join", {**vector_join_body("once"), "pk_pub": V["keys"]["pk_mallory"]["pub"]}) == "other_person"
    assert "once" in hub.invites


def test_publish_out_request_has_exactly_the_table_fields(joined: HostLink, hub: FakeHub):
    req = vector_publish_request()
    assert code(joined, "publish_out", {"request": {**req, "access": "public"}}) == "malformed"
    assert code(joined, "publish_out", {"request": {**req, "extra": 1}}) == "malformed"
    short = dict(req)
    del short["body_hex"]
    assert code(joined, "publish_out", {"request": short}) == "malformed"
    assert hub.forwarded == []


def test_header_names_are_case_insensitively_unique(joined: HostLink, hub: FakeHub):
    req = vector_publish_request()
    req["headers"]["orch-nonce"] = req["headers"]["Orch-Nonce"]
    assert code(joined, "publish_out", {"request": req}) == "malformed"
    assert hub.forwarded == []


def test_rejoin_with_an_older_or_conflicting_card_is_refused(joined: HostLink, hub: FakeHub):
    pk = V["keys"]["pk_alice"]["pub"]
    base = vector_join_body()
    joined.call("card_put", {"card": next_card(2), "pk_pub": pk})
    assert code(joined, "join", base) == "stale_card"  # card_seq 1 after 2
    same_seq = {**base, "card": next_card(2, relay_url=None)}
    assert code(joined, "join", same_seq) == "stale_card"
    assert hub.published_cards[WS] == next_card(2) or hub.published_cards[WS]["o"]["card_seq"] == 2
    b2 = binding(2)
    proof = owner_proof_for(b2["o"], WS, "hub.join", hub.now_ms)
    assert joined.call("join", {**base, "card": next_card(3), "binding": b2, "owner_proof": proof}) == {}
    assert hub.published_cards[WS]["o"]["card_seq"] == 3


@pytest.mark.parametrize("sig", ["", "!!", "AA", "A" * 86 + "=", 5, None])
def test_authenticate_refuses_a_malformed_sig_with_a_code(joined: HostLink, hub: FakeHub, sig):
    a = V["link_auth"]
    with pytest.raises(H.Refused) as e:
        hub.authenticate(WS, bytes.fromhex(a["challenge_hex"]), bytes.fromhex(a["exporter_hex"]), sig)
    assert e.value.code == "malformed"


@pytest.mark.parametrize("op", ["sign_something", "<script>", "x" * 5000, 7, None, "join\n"])
def test_error_reply_to_an_unknown_op_never_echoes_it(joined: HostLink, hub: FakeHub, op):
    msg = joined.message("snapshot_get", {})
    o = {**msg["o"], "op": op}
    reply = hub.send(sign_with(private_key("wsk_a"), "sig_hub_link", o))
    assert reply["o"]["op"] == "error" and reply["o"]["re"] == msg["o"]["seq"]
    assert reply["o"]["body"]["error"] in ("unknown_op", "malformed")


def test_known_ops_are_echoed_and_correlated_by_re(joined: HostLink):
    r = joined.request("wk_seal", {"epoch": 1, "wrap": "AA"})
    assert r["op"] == "wk_seal" and r["body"] == {"error": "wrong_mode"} and r["re"] == joined.seq


# --- #1198 C9: the owner proof (protocol 9.6 "At the hub") ------------------------------------------------


def test_join_without_the_owner_proof_is_proof_missing_and_nothing_is_pinned(hub: FakeHub):
    link = HostLink(hub)
    assert code(link, "join", {**vector_join_body(), "owner_proof": None}) == "proof_missing"
    assert hub.workspaces == {} and hub.pins == {}


def test_join_with_a_proof_of_another_person_or_binding_is_proof_invalid(hub: FakeHub):
    link = HostLink(hub)
    body = vector_join_body()
    other_seq = owner_proof_for(binding(2)["o"], WS, "hub.join", hub.now_ms)
    assert code(link, "join", {**body, "owner_proof": other_seq}) == "proof_invalid"  # names binding_seq 2
    full = owner_proof_for({**binding(1)["o"], "mode": "full"}, WS, "hub.join", hub.now_ms)
    assert code(link, "join", {**body, "owner_proof": full}) == "proof_invalid"  # the binding is selective


def test_a_mode_switch_and_a_rejoin_need_the_proof_and_a_re_sent_binding_does_not(joined: HostLink, hub: FakeHub):
    sw = binding(2, "full")
    assert code(joined, "binding_put", {"object": sw, "owner_proof": None}) == "proof_missing"
    assert hub.workspaces[WS].mode == "selective"
    assert code(
        joined, "binding_put", {"object": sw, "owner_proof": owner_proof_for(sw["o"], WS, "hub.join", hub.now_ms)}
    ) == ("proof_invalid")  # a selective-to-full switch is a hub.mode proof
    assert (
        joined.call("binding_put", {"object": sw, "owner_proof": owner_proof_for(sw["o"], WS, "hub.mode", hub.now_ms)})
        == {}
    )
    assert joined.call("binding_put", {"object": sw, "owner_proof": None}) == {}  # identical: no proof
    assert joined.call("binding_put", {"object": unbound(2), "owner_proof": None}) == {}  # a leave: none
    re = binding(3)
    assert code(joined, "binding_put", {"object": re, "owner_proof": None}) == "proof_missing"
    assert joined.call("binding_put", {"object": re}) == {}  # the helper makes the rejoin proof

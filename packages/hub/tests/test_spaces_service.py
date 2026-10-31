# ruff: noqa: F811, RUF059  (the `shub` fixture is imported then named as an argument; two() returns a full tuple)
"""Shared spaces over real links (protocol.md §10.4): hub-side rules, key epochs, wraps, consent, unbind."""

from __future__ import annotations

import pytest
from spaces_helpers import ADMIN, ALICE, BOB, HELD, ITEM_BODY, PUBS, WS_C, Host, shub  # noqa: F401

from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import spaces as SP
from orch_hub.contract import vectorgen as G
from orch_hub.link import LinkError
from orch_hub.spaces import SpaceError

IT1, IT2, IT3 = G.fake16("it1"), G.fake16("it2"), G.fake16("it3")


def two(shub, mode_b="full"):
    h = shub()
    a, b = h.join(G.WS_A), h.join(G.WS_B, mode_b)
    return h, a, b


def refused(c, op, body, code):
    with pytest.raises(LinkError) as e:
        c.request(op, body)
    assert e.value.code == code


def test_create_signs_a_record_and_wraps_every_member(shub):
    h, a, b = two(shub)
    rec = h.svc.create("finance", [G.WS_A, G.WS_B], ADMIN)
    assert rec["epoch"] == 1 and rec["members"] == sorted([G.WS_A, G.WS_B])
    ha, hb = Host(a, G.WS_A), Host(b, G.WS_B)
    for host in (ha, hb):
        host.sync(rec["space_id"])
    assert ha.sk[1] != b"" and ha.sk[1] == hb.sk[1]  # both hosts opened the same SK_e
    assert SP.sk_commit(rec["space_id"], 1, ha.sk[1]) == rec["sk_commit"]
    listed = a.request("space_list", {})
    assert [s["o"]["space_id"] for s in listed["spaces"]] == [rec["space_id"]]
    assert listed["meta"] == {rec["space_id"]: {"name": "finance", "kind": ""}}  # unsigned display data
    assert set(listed) == {"spaces", "meta"} and set(listed["meta"][rec["space_id"]]) == {"name", "kind"}


def test_item_roundtrip_between_two_members(shub):
    h, a, b = two(shub)
    sid = h.svc.create("finance", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    ha, hb = Host(a, G.WS_A), Host(b, G.WS_B)
    ha.sync(sid)
    hb.sync(sid)
    assert a.request("space_put", ha.put_body(sid, IT1)) == {"seq": 1}
    r = hb.sync(sid)
    (item,) = hb.read(sid, r, PUBS)
    assert item["workspace_id"] == G.WS_A and item["body"] == ITEM_BODY


def test_put_rules_over_the_link(shub):
    h, a, b = two(shub)
    c = h.join(WS_C)
    sid = h.svc.create("finance", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    ha = Host(a, G.WS_A)
    ha.sync(sid)
    body = ha.put_body(sid, IT1)
    assert a.request("space_put", body) == {"seq": 1}
    assert a.request("space_put", body) == {"seq": 1}  # the same put again is idempotent
    refused(
        a,
        "space_put",
        {**body, "sealed": ha.put_body(sid, IT1, {**ITEM_BODY, "name": "other.txt"})["sealed"]},
        "replay",
    )
    refused(c, "space_put", body, "not_member")
    for bad in ({k: v for k, v in ITEM_BODY.items() if k != "by"}, {**ITEM_BODY, "by": "root"}):  # `by` is required
        refused(a, "space_put", ha.put_body(sid, IT2, bad), "malformed")
    refused(a, "space_put", {**body, "space_id": G.fake16("nope")}, "not_member")
    refused(a, "space_put", {**body, "item_id": IT2}, "malformed")  # sealed for another item id
    refused(c, "space_get", {"space_id": sid, "since": 0}, "not_member")
    refused(b, "space_revoke", {"space_id": sid, "item_id": IT1}, "other_workspace")
    refused(a, "space_revoke", {"space_id": sid, "item_id": IT2}, "unknown_item")


def test_revoke_is_a_new_entry_and_drops_the_bytes(shub):
    h, a, b = two(shub)
    sid = h.svc.create("finance", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    ha, hb = Host(a, G.WS_A), Host(b, G.WS_B)
    ha.sync(sid)
    hb.sync(sid)
    body1 = ha.put_body(sid, IT1)
    a.request("space_put", body1)
    assert a.request("space_revoke", {"space_id": sid, "item_id": IT1}) == {"seq": 2}
    r = hb.sync(sid, since=1)
    assert [(e["seq"], e["revoked"], "sealed" in e) for e in r["entries"]] == [(2, True, False)]
    assert h.store.sealed(sid, IT1) == ""  # the hub no longer holds the bytes
    assert a.request("space_put", body1) == {"seq": 2}  # the same bytes again: stays revoked
    refused(a, "space_put", ha.put_body(sid, IT1), "replay")  # other bytes under a taken id
    assert hb.sync(sid, since=0)["entries"][0]["revoked"] is True


def test_adding_a_member_rotates_the_key_and_closes_the_old_epoch(shub):
    h, a, b = two(shub)
    c = h.join(WS_C)
    sid = h.svc.create("finance", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    ha, hc = Host(a, G.WS_A), Host(c, WS_C)
    ha.sync(sid)
    a.request("space_put", ha.put_body(sid, IT1))
    rec2 = h.svc.add(sid, WS_C, ADMIN)
    assert rec2["epoch"] == 2 and rec2["members"] == sorted([G.WS_A, G.WS_B, WS_C])
    refused(a, "space_put", ha.put_body(sid, IT2), "stale_epoch")  # sealed for epoch 1
    ha.sync(sid)
    assert ha.sk[2] != ha.sk[1]
    assert a.request("space_put", ha.put_body(sid, IT2)) == {"seq": 2}
    r = hc.sync(sid)
    assert [e["item_id"] for e in r["entries"]] == [IT2]  # no backfill of epoch 1
    assert 1 not in hc.sk
    (item,) = hc.read(sid, r, PUBS)
    assert item["item_id"] == IT2


def test_removing_a_member_ends_its_future_access(shub):
    h, a, b = two(shub)
    sid = h.svc.create("finance", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    hb = Host(b, G.WS_B)
    hb.sync(sid)
    h.svc.remove(sid, G.WS_B, ADMIN)
    refused(b, "space_get", {"space_id": sid, "since": 0}, "not_member")
    assert b.request("space_list", {}) == {"spaces": [], "meta": {}}
    ha = Host(a, G.WS_A)
    r = ha.sync(sid)
    assert r["space"]["o"]["epoch"] == 2 and r["space"]["o"]["members"] == [G.WS_A]
    assert ha.sk[2] != hb.sk[1]


def test_unbind_removes_the_workspace_from_every_space_in_the_same_step(shub):
    h, a, b = two(shub)
    s1 = h.svc.create("one", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    s2 = h.svc.create("two", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    b.request("binding_put", {"object": G.unbound(ws=G.WS_B, wsk="wsk_b", seq=1)})
    for sid in (s1, s2):
        assert h.store.current_epoch(sid) == 2
        assert h.store.epoch_row(sid, 2)[0] == [G.WS_A]
    assert h.store.wrap(s1, 2, G.WS_B) is None
    refused(b, "space_get", {"space_id": s1, "since": 0}, "not_bound")


def test_the_last_member_leaving_ends_the_space(shub):
    h, a, _ = two(shub)
    sid = h.svc.create("solo", [G.WS_A], ADMIN)["space_id"]
    a_host = Host(a, G.WS_A)
    a_host.sync(sid)
    a.request("space_put", a_host.put_body(sid, IT1))
    assert h.svc.remove(sid, G.WS_A, ADMIN) is None
    assert not h.store.exists(sid) and h.store.items(sid) == {}
    refused(a, "space_get", {"space_id": sid, "since": 0}, "not_member")


def test_reconcile_removes_workspaces_that_are_no_longer_bound(shub):
    h, a, b = two(shub)
    sid = h.svc.create("finance", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    h.registry.set_binding_state(G.WS_B, {"binding_seq": 1, "status": "unbound", "binding": None})
    h.svc.reconcile()
    assert h.store.epoch_row(sid, 2)[0] == [G.WS_A]


# --- who may change a space -------------------------------------------------------------------------------------------


def grant(h, user, ws, role):
    g = f"g-{user[:6]}-{ws[:4]}"
    h.access.add_user(user, "u" + user[:4]) if not h.access.has_user(user) else None
    h.access.add_group(g)
    h.access.add_member(g, user)
    h.access.grant(ws, g, role)


def test_admin_in_every_full_member_workspace_may_create(shub):
    h, a, b = two(shub)
    grant(h, ALICE, G.WS_A, "admin")
    with pytest.raises(SpaceError) as e:
        h.svc.create("x", [G.WS_A, G.WS_B], ALICE)
    assert e.value.code == "forbidden_role"
    grant(h, ALICE, G.WS_B, "admin")
    assert h.svc.create("x", [G.WS_A, G.WS_B], ALICE)["epoch"] == 1


@pytest.mark.parametrize("role", ["read", "edit"])
def test_edit_or_read_is_not_enough(shub, role):
    h, a, b = two(shub)
    grant(h, BOB, G.WS_A, role)
    grant(h, BOB, G.WS_B, role)
    with pytest.raises(SpaceError) as e:
        h.svc.create("x", [G.WS_A, G.WS_B], BOB)
    assert e.value.code == "forbidden_role"


def test_a_user_without_any_role_may_not_add_or_remove(shub):
    h, a, b = two(shub)
    sid = h.svc.create("x", [G.WS_A], ADMIN)["space_id"]
    for fn in (lambda: h.svc.add(sid, G.WS_B, BOB), lambda: h.svc.remove(sid, G.WS_A, BOB)):
        with pytest.raises(SpaceError) as e:
            fn()
        assert e.value.code == "forbidden_role"


def test_unknown_workspace_and_double_add_are_refused(shub):
    h, a, b = two(shub)
    with pytest.raises(SpaceError) as e:
        h.svc.create("x", [G.WS_A, WS_C], ADMIN)  # C never joined
    assert e.value.code == "unknown_workspace"
    sid = h.svc.create("x", [G.WS_A], ADMIN)["space_id"]
    with pytest.raises(SpaceError) as e:
        h.svc.add(sid, G.WS_A, ADMIN)
    assert e.value.code == "already_member"


# --- consent: a selective workspace joins only with its own accept ---------------------------------------------------


def test_a_hub_admin_cannot_add_a_selective_workspace(shub):
    h, a, b = two(shub, mode_b="selective")
    with pytest.raises(SpaceError) as e:
        h.svc.create("x", [G.WS_A, G.WS_B], ADMIN)
    assert e.value.code == "not_accepted"
    assert e.value.need["workspaces"] == [G.WS_B] and e.value.need["epoch"] == 1
    assert h.store.space_ids() == []


def test_a_selective_workspace_joins_with_its_accept_for_that_transition(shub):
    h, a, b = two(shub, mode_b="selective")
    members = sorted([G.WS_A, G.WS_B])
    # the accept names the space id of the transition, so the owner signs after the hub tells it: a space that exists
    # (here created with A alone), then an add that is refused with what to sign
    sid = h.svc.create("solo", [G.WS_A], ADMIN)["space_id"]
    with pytest.raises(SpaceError) as e:
        h.svc.add(sid, G.WS_B, ADMIN)
    need = e.value.need
    accept = KS.WskSigner(G.K.priv["wsk_b"]).sign_object(
        {
            "v": 2,
            "suite": 2,
            "kind": "space.accept",
            "hv": 0,
            "hub_id": G.HUB_ID,
            "space_id": need["space_id"],
            "workspace_id": G.WS_B,
            "epoch": need["epoch"],
            "prev_members_hash": need["prev_members_hash"],
            "members_hash": need["members_hash"],
            "dev": True,
        }
    )
    assert b.request("space_accept", {"accept": accept}) == {}
    rec = h.svc.add(sid, G.WS_B, ADMIN)
    assert rec["members"] == members and rec["epoch"] == 2
    # a replay of the same accept at a later epoch (removed, then listed again) is refused
    h.svc.remove(sid, G.WS_B, ADMIN)
    with pytest.raises(SpaceError) as e:
        h.svc.add(sid, G.WS_B, ADMIN)
    assert e.value.code == "not_accepted"


def test_an_accept_of_another_workspace_or_hub_is_refused(shub):
    h, a, b = two(shub, mode_b="selective")
    sid = h.svc.create("solo", [G.WS_A], ADMIN)["space_id"]
    base = {
        "v": 2,
        "suite": 2,
        "kind": "space.accept",
        "hv": 0,
        "hub_id": G.HUB_ID,
        "space_id": sid,
        "workspace_id": G.WS_B,
        "epoch": 2,
        "prev_members_hash": SP.members_hash([G.WS_A]),
        "members_hash": SP.members_hash(sorted([G.WS_A, G.WS_B])),
        "dev": True,
    }
    sign_b = KS.WskSigner(G.K.priv["wsk_b"]).sign_object
    refused(a, "space_accept", {"accept": sign_b(base)}, "bad_signature")  # A's link, B's key... pinned key is A's
    refused(b, "space_accept", {"accept": sign_b({**base, "workspace_id": G.WS_A})}, "other_workspace")
    refused(b, "space_accept", {"accept": sign_b({**base, "hub_id": G.ROGUE_HUB_ID})}, "other_hub")
    refused(b, "space_accept", {"accept": {"o": base}}, "malformed")


# --- when shared spaces are not configured ----------------------------------------------------------------------------


def test_without_a_space_service_the_ops_are_unknown(make_hub):
    h = make_hub()
    c = h.open(first=True)
    c.request("join", h.join_body())
    for op, body in (("space_list", {}), ("space_get", {"space_id": G.SPACE, "since": 0})):
        refused(c, op, body, "unknown_op")


def test_records_and_wraps_verify_with_the_contract_under_the_hub_card(shub):
    h, a, b = two(shub)
    rec = h.svc.create("x", [G.WS_A, G.WS_B], ADMIN)
    row = h.store.epoch_row(rec["space_id"], 1)
    assert H.L["sig_hub_space"]  # label exists
    SP.verify_space(row[2], HELD, dev_mode=True)
    w = h.store.wrap(rec["space_id"], 1, G.WS_B)
    SP.verify_space_wrap(w, HELD, rec, workspace_id=G.WS_B, wxk_priv=G.K.priv["wxk_b"], wxk_version=1, dev_mode=True)


# --- review follow-ups: mode change, strict wrap, unavailable spaces, malformed ids ------------------------------------


def rebind(c, ws, wsk, mode, seq):
    c.request("binding_put", {"object": G.binding(ws=ws, wsk=wsk, mode=mode, seq=seq)})


def test_full_to_selective_rebind_removes_the_workspace_from_every_space(shub):
    h, a, b = two(shub)
    s1 = h.svc.create("one", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    s2 = h.svc.create("two", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    rebind(b, G.WS_B, "wsk_b", "selective", 2)
    for sid in (s1, s2):
        assert h.store.current_epoch(sid) == 2 and h.store.epoch_row(sid, 2)[0] == [G.WS_A]
        assert h.store.wrap(sid, 2, G.WS_B) is None
        refused(b, "space_get", {"space_id": sid, "since": 0}, "not_member")
    # coming back is the add path: a selective workspace needs its owner's accept for that transition
    with pytest.raises(SpaceError) as e:
        h.svc.add(s1, G.WS_B, ADMIN)
    assert e.value.code == "not_accepted"


def test_selective_to_full_rebind_and_same_mode_rebind_keep_memberships(shub):
    h, a, b = two(shub)
    sid = h.svc.create("one", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    rebind(b, G.WS_B, "wsk_b", "full", 2)  # same mode, new binding seq
    assert h.store.current_epoch(sid) == 1
    h2, a2, b2 = two(shub, mode_b="selective")
    sid2 = h2.svc.create("solo", [G.WS_A], ADMIN)["space_id"]
    rebind(b2, G.WS_B, "wsk_b", "full", 2)  # selective -> full is not a loss of consent
    assert h2.store.current_epoch(sid2) == 1


def test_a_selective_member_that_joined_with_its_accept_stays_through_a_reconcile(shub):
    h, a, b = two(shub, mode_b="selective")
    sid = h.svc.create("solo", [G.WS_A], ADMIN)["space_id"]
    with pytest.raises(SpaceError) as e:
        h.svc.add(sid, G.WS_B, ADMIN)
    need = e.value.need
    accept = KS.WskSigner(G.K.priv["wsk_b"]).sign_object(
        {
            "v": 2,
            "suite": 2,
            "kind": "space.accept",
            "hv": 0,
            "hub_id": G.HUB_ID,
            "space_id": sid,
            "workspace_id": G.WS_B,
            "epoch": need["epoch"],
            "prev_members_hash": need["prev_members_hash"],
            "members_hash": need["members_hash"],
            "dev": True,
        }
    )
    b.request("space_accept", {"accept": accept})
    h.svc.add(sid, G.WS_B, ADMIN)
    h.svc.reconcile()
    assert h.store.current_epoch(sid) == 2  # still a member: it has its accept


def test_a_wrap_is_sealed_only_to_the_wxk_of_the_verified_card(shub):
    h, a, b = two(shub)
    good = h.registry.card(G.WS_B)
    pin = h.registry.pin(G.WS_B)
    unsigned = {"wxk_pub": G.K.b("wxk_b"), "wxk_version": 1}  # a bare shape, no signature
    for bad in (
        unsigned,
        {"o": good["o"]},  # signed part without its signature and delegation
        {**good, "o": {**good["o"], "wxk_pub": G.K.b("wxk_a")}},  # WXK swapped after signing
        {**good, "sig": G.b64u(bytes(64))},
        {},
    ):
        h.registry.put_card(G.WS_B, pin["pk_pub"], bad)
        with pytest.raises(SpaceError) as e:
            h.svc.create("x", [G.WS_A, G.WS_B], ADMIN)
        assert e.value.code == "unknown_workspace"
        assert h.store.space_ids() == []
    h.registry.put_card(G.WS_B, pin["pk_pub"], good)
    assert h.svc.create("x", [G.WS_A, G.WS_B], ADMIN)["epoch"] == 1


def test_a_space_that_cannot_rotate_is_unavailable_until_it_can(shub, monkeypatch):
    h, a, b = two(shub)
    sid = h.svc.create("x", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    other = h.svc.create("y", [G.WS_A], ADMIN)["space_id"]
    real = h.store.add_epoch

    def broken(*args, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(h.store, "add_epoch", broken)
    b.request("binding_put", {"object": G.unbound(ws=G.WS_B, wsk="wsk_b", seq=1)})  # the unbind is recorded anyway
    assert h.registry.binding_state(G.WS_B)["status"] == "unbound"
    h.svc.reconcile()  # logs, retries, does not raise: the hub still starts
    ha = Host(a, G.WS_A)
    refused(a, "space_get", {"space_id": sid, "since": 0}, "unavailable")
    refused(a, "space_put", {"space_id": sid, "epoch": 1, "item_id": IT1, "sealed": "AAAA"}, "unavailable")
    assert [s["o"]["space_id"] for s in a.request("space_list", {})["spaces"]] == [other]
    ha.sync(other)  # other spaces are unaffected
    monkeypatch.setattr(h.store, "add_epoch", real)
    r = a.request("space_get", {"space_id": sid, "since": 0})  # the next op retries and reconciles
    assert r["space"]["o"]["epoch"] == 2 and r["space"]["o"]["members"] == [G.WS_A]


def test_ids_are_type_checked_before_any_lookup(shub):
    h, a, b = two(shub)
    for bad in ("zz", "A" * 32, "", 5, None, "0" * 31):
        with pytest.raises(SpaceError) as e:
            h.svc.add(bad, G.WS_A, ADMIN)
        assert e.value.code == "malformed"
        with pytest.raises(H.Refused) as r:
            h.svc.get(G.WS_A, {"space_id": bad, "since": 0})
        assert r.value.code == "malformed"
        with pytest.raises(H.Refused) as r:
            h.svc.put(G.WS_A, {"space_id": bad, "epoch": 1, "item_id": IT1, "sealed": "AA"})
        assert r.value.code == "malformed"

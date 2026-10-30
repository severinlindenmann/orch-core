"""M2-8a: access changes need the workspace owner's approval (#1409), the last-admin guard (#1411), Opus conditions 7-10.

Every binding condition has a test here:

* 7  the approval proof: digest, the owner's channel key, single use, stale snapshot, current owner only, session scoping;
* 8  the owner alias is written at bind / join only (no route and no registry writer but the join path);
* 9  override: reason required, audited, owner notice, the owner protection still holds;
* 10 expiry after 14 days: never applied, swept lazily and by ``sweep()``, ``change.expired`` is recorded;
* the rule itself: increases wait for the owner, reductions apply at once, the owner is never locked out, the last admin
  is not removed by accident.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
import sqlite3
from pathlib import Path

import pytest
from channels_helpers import new_channel
from test_labels import V2
from webapi_helpers import WS, WS_SEL, Web

import orch_hub
from orch_hub.access import AccessError
from orch_hub.activity import ActivityLog
from orch_hub.changes import TTL_MS, ChangeError
from orch_hub.contract.canon import cj, unb64u
from orch_hub.contract.hub import Refused
from orch_hub.contract.labels import ACCESS_CHANGE_KIND, ACCESS_CHANGE_LABEL, HUB_LABELS, V2_REUSED
from orch_hub.custodial.engine import check_channel_proof
from orch_hub.webapi import ROUTES, WebApi

WS2 = "a1" * 16  # a full workspace owned by olive
WS3 = "b2" * 16  # a full workspace owned by omar
WS4 = "c3" * 16
ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    web.activity = ActivityLog(tmp_path / "activity.db", now_ms=web.clock)
    web.api.d.activity = web.changes._activity = web.activity
    e = web.env
    web.olive, web.omar, web.newbie = e.user("olive"), e.user("omar"), e.user("newbie")
    e.acc.add_group("owners")
    e.acc.add_member("owners", web.olive)
    e.acc.add_member("owners", web.omar)
    web.reg.add(WS2, "full", owner=web.olive, name="Orchard")
    web.reg.add(WS3, "full", owner=web.omar, name="Meadow")
    e.acc.grant(WS2, "owners", "admin")
    e.acc.grant(WS3, "owners", "admin")
    web.reg.add(WS_SEL, "selective", owner=web.olive)
    e.acc.add_group("devs")
    e.acc.add_group("readers")
    web.dana, web.dora = (
        e.user("dana"),
        e.user("dora"),
    )  # members: a grant to an EMPTY group adds nobody, so needs no approval
    e.acc.add_member("devs", web.dana)
    e.acc.add_member("readers", web.dora)
    yield web
    web.activity.close()
    web.close()


def acc(w):
    return w.env.acc


def snapshot(w):
    """Everything the access model holds that a refused or pending change must not touch."""
    a = acc(w)
    return (a.grants(), [(g["name"], g["members"]) for g in a.groups()], a.users(), w.env.audit.store.head())


def propose(w, tok, ws, group, role):
    s, o, _ = w.call("PUT", "/admin/grants", {"workspace_id": ws, "group": group, "role": role}, token=tok)
    assert s == 202, o
    assert ULID.fullmatch(o["change_id"])
    return o["change_id"]


def owner_session(w, user):
    """The owner signed in, with this browser's channel key registered. Returns (token, signer, channel_id)."""
    tok = w.login(user)
    signer, ch = w.register_channel(tok, user)
    return tok, signer, ch


def prepare(w, tok, cid, ws=None):
    return w.call("POST", f"/me/changes/{cid}/approve/prepare", {} if ws is None else {"workspace_id": ws}, token=tok)


def approve(w, tok, cid, signer, ch, ws=None, *, proof=None):
    """Prepare, sign the digest with ``signer`` (what the console does) and send the proof."""
    s, prep, _ = prepare(w, tok, cid, ws)
    assert s == 200, prep
    body = {"channel_id": ch, "proof": proof or w.proof(signer, prep["digest"])}
    if ws is not None:
        body["workspace_id"] = ws
    return w.call("POST", f"/me/changes/{cid}/approve", body, token=tok)


def row(w, cid):
    return w.changes._db.change(cid)


def activity(w, typ):
    return [r for r in w.activity.list(limit=200) if r["type"] == typ]


def types(w):
    return sorted(r["type"] for r in w.activity.list(limit=200))


def has_grant(w, ws, group, role):
    return {"workspace_id": ws, "group": group, "role": role} in acc(w).grants()


# --- the rule: increases wait, reductions do not ------------------------------------------------------------------------


def test_increase_by_a_hub_admin_is_pending_and_access_is_untouched(w):
    tok = w.login(w.admin)
    before = snapshot(w)
    s, o, _ = w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "devs", "role": "edit"}, token=tok)
    assert s == 202
    assert o["waiting_for"] == [{"workspace_id": WS2, "owner_name": "olive"}]
    assert set(o) == {"change_id", "waiting_for"}
    assert snapshot(w) == before  # nothing reached access, nothing reached the audit chain
    c = row(w, o["change_id"])
    assert (c["kind"], c["state"], c["proposer"]) == ("grant", "pending", w.admin)
    assert c["expires_ms"] == c["created_ms"] + 14 * 86_400_000 == c["created_ms"] + TTL_MS
    assert types(w) == ["change.proposed"]
    # the same request again is the one already waiting, not a second one
    again = w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "devs", "role": "edit"}, token=tok)
    assert again[0] == 202 and again[1]["change_id"] == o["change_id"]
    assert len(w.changes._db.changes()) == 1


def test_a_higher_role_for_a_group_that_already_has_one_is_an_increase(w):
    acc(w).grant(WS2, "devs", "read")
    propose(w, w.login(w.admin), WS2, "devs", "edit")
    assert has_grant(w, WS2, "devs", "read")


def test_member_add_to_a_group_with_a_grant_on_an_owned_workspace_is_pending(w):
    acc(w).grant(WS2, "devs", "edit")
    tok = w.login(w.admin)
    before = snapshot(w)
    s, o, _ = w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=tok)
    assert s == 202 and o["waiting_for"] == [{"workspace_id": WS2, "owner_name": "olive"}]
    assert snapshot(w) == before
    assert acc(w).members("devs") == [w.dana]
    assert row(w, o["change_id"])["kind"] == "member_add"


def test_when_no_rule_applies_the_change_is_applied_as_before(w):
    tok = w.login(w.admin)
    # no owner alias, a selective workspace (hub roles grant nothing there), a group without grants
    assert w.call("PUT", "/admin/grants", {"workspace_id": WS, "group": "devs", "role": "edit"}, token=tok)[:2] == (
        200,
        {"ok": True},
    )
    assert (
        w.call("PUT", "/admin/grants", {"workspace_id": WS_SEL, "group": "devs", "role": "edit"}, token=tok)[0] == 200
    )
    assert w.call("PUT", f"/admin/groups/readers/members/{w.newbie}", token=tok)[0] == 200
    assert has_grant(w, WS, "devs", "edit") and has_grant(w, WS_SEL, "devs", "edit")
    assert sorted(acc(w).members("readers")) == sorted([w.dora, w.newbie])
    assert w.changes._db.changes() == []


def test_an_old_error_is_still_the_same_error(w):
    tok = w.login(w.admin)
    assert w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "nope", "role": "edit"}, token=tok)[:2] == (
        404,
        {"error": "unknown_group"},
    )
    assert w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "devs", "role": "boss"}, token=tok)[0] == 400
    assert (
        w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "hub-admin", "role": "read"}, token=tok)[0] == 400
    )
    assert w.call("PUT", f"/admin/groups/nope/members/{w.newbie}", token=tok)[0] == 404
    assert w.call("PUT", "/admin/groups/devs/members/" + "9" * 32, token=tok)[0] == 404
    assert w.changes._db.changes() == []


def test_the_owner_as_actor_applies_at_once(w):
    acc(w).add_member("hub-admin", w.olive)  # an owner who is also a hub admin proposes through the same route
    tok = w.login(w.olive)
    s, o, _ = w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "devs", "role": "edit"}, token=tok)
    assert (s, o) == (200, {"ok": True})
    assert has_grant(w, WS2, "devs", "edit")
    assert w.changes._db.changes() == []
    # a membership change applies at once when the actor owns every affected workspace
    assert w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=tok)[0] == 200
    assert sorted(acc(w).members("devs")) == sorted([w.dana, w.newbie])


def test_a_lower_or_equal_role_never_waits(w):
    acc(w).grant(WS2, "devs", "edit")
    tok = w.login(w.admin)
    assert w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "devs", "role": "edit"}, token=tok)[0] == 200
    assert w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "devs", "role": "read"}, token=tok)[0] == 200
    assert has_grant(w, WS2, "devs", "read")
    assert w.changes._db.changes() == []


def test_reductions_apply_at_once(w):
    a = acc(w)
    a.grant(WS2, "devs", "edit")
    a.grant(WS2, "readers", "read")
    a.add_member("devs", w.newbie)
    tok = w.login(w.admin)
    assert w.call("POST", "/admin/grants/revoke", {"workspace_id": WS2, "group": "readers"}, token=tok)[0] == 200
    assert w.call("DELETE", f"/admin/groups/devs/members/{w.newbie}", token=tok)[0] == 200
    assert w.call("DELETE", "/admin/groups/devs", token=tok)[0] == 200
    assert w.call("DELETE", f"/admin/users/{w.newbie}", token=tok)[0] == 200
    assert [g["group"] for g in a.grants(WS2)] == ["owners"]
    assert w.changes._db.changes() == []


def test_without_the_changes_service_nothing_is_applied(w):
    w.api.d.changes = None  # fail closed: no rule behind the routes, no access change
    tok = w.login(w.admin)
    before = snapshot(w)
    for call in (
        w.call("PUT", "/admin/grants", {"workspace_id": WS, "group": "devs", "role": "edit"}, token=tok),
        w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=tok),
        w.call("POST", "/admin/grants/revoke", {"workspace_id": WS2, "group": "owners"}, token=tok),
        w.call("DELETE", f"/admin/groups/devs/members/{w.newbie}", token=tok),
        w.call("GET", "/me/changes", token=tok),
    ):
        assert call[:2] == (503, {"error": "unavailable"})
    assert snapshot(w) == before


# --- the owner protection: nothing removes or demotes the owner's own admin access ----------------------------------------


@pytest.mark.parametrize("confirm", [False, True])
def test_owner_protected_on_every_reduction_even_with_confirm(w, confirm):
    tok = w.login(w.admin)
    extra = {"confirm": True} if confirm else {}
    before = snapshot(w)
    refused = [
        w.call("POST", "/admin/grants/revoke", {"workspace_id": WS2, "group": "owners", **extra}, token=tok),
        w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "owners", "role": "edit", **extra}, token=tok),
        w.call("DELETE", f"/admin/groups/owners/members/{w.olive}", extra or None, token=tok),
        w.call("DELETE", "/admin/groups/owners", extra or None, token=tok),
        w.call("DELETE", f"/admin/users/{w.olive}", extra or None, token=tok),
    ]
    assert [(s, o) for s, o, _ in refused] == [(409, {"error": "owner_protected"})] * 5
    assert snapshot(w) == before
    # the owner's exit is to switch the workspace to selective: hub roles then grant nothing and nothing is protected
    w.reg.add(WS2, "selective", owner=w.olive)
    assert w.call("POST", "/admin/grants/revoke", {"workspace_id": WS2, "group": "owners"}, token=tok)[0] == 200


def test_owner_protected_when_the_owner_is_admin_only_through_the_hub_admin_group(w):
    acc(w).add_member("hub-admin", w.olive)
    acc(w).remove_member("owners", w.olive)  # olive administers WS2 only because she is a hub admin
    tok = w.login(w.admin)
    s, o, _ = w.call("DELETE", f"/admin/groups/hub-admin/members/{w.olive}", token=tok)
    assert (s, o) == (409, {"error": "owner_protected"})
    assert w.olive in acc(w).members("hub-admin")


def test_a_lower_role_than_admin_is_not_protected(w):
    # the protection is for the owner's ADMIN-level access: an owner who holds less is not "demoted from admin"
    acc(w).remove_member("owners", w.olive)
    acc(w).add_group("makers")
    acc(w).add_member("makers", w.olive)
    acc(w).grant(WS2, "makers", "edit")
    tok = w.login(w.admin)
    assert w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "makers", "role": "read"}, token=tok)[0] == 200


# --- the last-admin guard -----------------------------------------------------------------------------------------------


def test_last_admin_when_the_last_hub_admin_is_removed_unless_confirmed(w):
    tok = w.login(w.admin)
    # WS has no owner and no admin-level grant: its only admin is the hub admin. WS2 and WS3 keep the owners group.
    before = snapshot(w)
    s, o, _ = w.call("DELETE", f"/admin/groups/hub-admin/members/{w.admin}", token=tok)
    assert (s, o) == (409, {"error": "last_admin"})
    s, o, _ = w.call("DELETE", f"/admin/users/{w.admin}", token=tok)
    assert (s, o) == (409, {"error": "last_admin"})
    assert snapshot(w) == before
    assert w.call("DELETE", f"/admin/groups/hub-admin/members/{w.admin}", {"confirm": True}, token=tok)[0] == 200
    assert acc(w).members("hub-admin") == []


def test_a_second_hub_admin_means_no_last_admin(w):
    other = w.env.user("other-admin")
    acc(w).add_member("hub-admin", other)  # hub-admin members count as admins everywhere
    tok = w.login(w.admin)
    assert w.call("DELETE", f"/admin/groups/hub-admin/members/{w.admin}", token=tok)[0] == 200


def test_last_admin_for_revoke_group_user_and_demotion_with_confirm(w):
    a, ch = acc(w), w.changes
    a.add_group("ops")
    a.add_member("ops", w.newbie)
    a.grant(WS, "ops", "admin")
    a.remove_member("hub-admin", w.admin)  # the local operator emptied the hub-admin group: ops is WS's only admin
    for op, kw in (
        ("revoke", {"workspace_id": WS, "group": "ops"}),
        ("member_remove", {"group": "ops", "user_id": w.newbie}),
        ("group_remove", {"group": "ops"}),
        ("user_remove", {"user_id": w.newbie}),
    ):
        with pytest.raises(ChangeError) as e, ch.reducing(op, **kw):
            pytest.fail("the guard must refuse before the write")
        assert e.value.code == "last_admin", op
        with ch.reducing(op, confirm=True, **kw):
            pass  # confirmed: the caller may go on
    with pytest.raises(ChangeError) as e:
        ch.grant(w.admin, WS, "ops", "edit")  # a lower role is a reduction too
    assert e.value.code == "last_admin"
    assert ch.grant(w.admin, WS, "ops", "edit", confirm=True) is None and has_grant(w, WS, "ops", "edit")


def test_no_last_admin_when_the_workspace_had_no_admin_to_lose(w):
    a, ch = acc(w), w.changes
    a.remove_member("hub-admin", w.admin)  # WS has no admin-level holder at all now
    a.add_member("readers", w.newbie)
    a.grant(WS, "readers", "read")
    with ch.reducing("revoke", workspace_id=WS, group="readers"):
        pass


# --- condition 7: the owner's approval ------------------------------------------------------------------------------------


def test_approval_with_the_right_channel_key_applies(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, "devs", "edit")
    otok, signer, ch = owner_session(w, w.olive)
    head = w.env.audit.store.head()["seq"]
    s, o, _ = approve(w, otok, cid, signer, ch)
    assert (s, o) == (200, {"ok": True, "state": "applied", "remaining": 0})
    assert has_grant(w, WS2, "devs", "edit")
    assert row(w, cid)["state"] == "applied"
    assert w.env.audit.store.head()["seq"] == head + 1  # the grant is in the audit chain, by the proposer
    assert types(w) == ["change.approved", "change.proposed"]
    ap = w.changes._db.approvals(cid)[0]
    assert (ap["state"], ap["approver_user"]) == ("approved", w.olive)
    assert ap["proof_digest"] and len(ap["proof_digest"]) == 64
    # nothing is left waiting
    assert w.call("GET", "/me/changes", token=otok)[1] == {"pending": [], "notices": []}


def test_a_member_add_is_applied_by_the_approval(w):
    acc(w).grant(WS2, "devs", "edit")
    cid = w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=w.login(w.admin))[1]["change_id"]
    otok, signer, ch = owner_session(w, w.olive)
    assert approve(w, otok, cid, signer, ch)[:2] == (200, {"ok": True, "state": "applied", "remaining": 0})
    assert sorted(acc(w).members("devs")) == sorted([w.dana, w.newbie])


def test_the_digest_binds_change_workspace_body_version_and_expiry(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    otok, _signer, _ch = owner_session(w, w.olive)
    s, prep, _ = prepare(w, otok, cid)
    assert s == 200
    c = row(w, cid)
    version = w.env.snaps.snapshot(WS2, "full")["o"]["version"]
    ctx = {
        "contract": 1,
        "kind": ACCESS_CHANGE_KIND,
        "change_id": cid,
        "workspace_id": WS2,
        "body": {"group": "devs", "role": "edit", "workspace_id": WS2},
        "base_snapshot_version": version,
        "expires_ms": c["expires_ms"],
        "delta": [{"role": "edit", "user_id": w.dana}],
    }
    assert ACCESS_CHANGE_KIND == "orch.access_change.v1"
    assert prep["ctx"] == ctx and prep["workspace_id"] == WS2
    assert prep["digest"] == hashlib.sha256(ACCESS_CHANGE_LABEL.encode() + cj(ctx)).hexdigest()
    # every bound field changes the digest
    for key, other in (
        ("change_id", "0" * 26),
        ("workspace_id", WS3),
        ("body", {"group": "devs", "role": "admin", "workspace_id": WS2}),
        ("base_snapshot_version", version + 1),
        ("expires_ms", c["expires_ms"] + 1),
    ):
        moved = hashlib.sha256(ACCESS_CHANGE_LABEL.encode() + cj({**ctx, key: other})).hexdigest()
        assert moved != prep["digest"], key
    assert prep["change"]["id"] == cid and prep["change"]["approvals"][0]["workspace_name"] == "Orchard"


def test_a_wrong_key_is_refused_and_nothing_changes(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    otok, signer, ch = owner_session(w, w.olive)
    s, _prep, _ = prepare(w, otok, cid)
    before = snapshot(w)
    stranger = new_channel()  # a key that is not registered to olive
    s, o, _ = approve(w, otok, cid, stranger, ch)
    assert (s, o) == (403, {"error": "bad_channel_proof"})
    # a proof over some other digest is as wrong
    s, o, _ = approve(w, otok, cid, signer, ch, proof=w.proof(signer, "00" * 32))
    assert (s, o) == (403, {"error": "bad_channel_proof"})
    s, o, _ = approve(w, otok, cid, signer, ch, proof="not a proof")
    assert (s, o) == (403, {"error": "bad_channel_proof"})
    assert snapshot(w) == before and row(w, cid)["state"] == "pending"
    assert w.changes._db.approvals(cid)[0]["state"] == "pending"
    assert approve(w, otok, cid, signer, ch)[0] == 200  # the right key still works: nothing was burnt


def test_a_proof_for_one_change_is_no_proof_for_another(w):
    tok = w.login(w.admin)
    a = propose(w, tok, WS2, "devs", "edit")
    b = propose(w, tok, WS2, "readers", "read")
    otok, signer, ch = owner_session(w, w.olive)
    proof_a = w.proof(signer, prepare(w, otok, a)[1]["digest"])
    s, o, _ = approve(w, otok, b, signer, ch, proof=proof_a)
    assert (s, o) == (403, {"error": "bad_channel_proof"})
    assert row(w, b)["state"] == "pending" and not has_grant(w, WS2, "readers", "read")


def test_a_proof_is_used_once(w):
    tok = w.login(w.admin)
    a = propose(w, tok, WS2, "devs", "edit")
    b = propose(w, tok, WS2, "readers", "read")
    otok, signer, ch = owner_session(w, w.olive)
    proof_a = w.proof(signer, prepare(w, otok, a)[1]["digest"])
    assert approve(w, otok, a, signer, ch, proof=proof_a)[0] == 200
    again = w.call("POST", f"/me/changes/{a}/approve", {"channel_id": ch, "proof": proof_a}, token=otok)
    assert again[:2] == (409, {"error": "not_pending"})
    # the store itself refuses the same proof twice, whatever it is presented for
    used = w.changes._db.approvals(a)[0]["proof_digest"]
    with pytest.raises(sqlite3.IntegrityError):
        w.changes._db._db.execute("UPDATE approval SET proof_digest=? WHERE change_id=?", (used, b))
    assert row(w, b)["state"] == "pending"


def test_a_digest_replayed_at_the_store_level_is_a_replay(w):
    tok = w.login(w.admin)
    a = propose(w, tok, WS2, "devs", "edit")
    b = propose(w, tok, WS2, "readers", "read")
    otok, signer, ch = owner_session(w, w.olive)
    proof = w.proof(signer, prepare(w, otok, b)[1]["digest"])
    # a proof whose digest the store already holds (here: planted on a's row) is a replay, whichever change it is for
    planted = hashlib.sha256(unb64u(proof, 64)).hexdigest()
    w.changes._db._db.execute("UPDATE approval SET proof_digest=? WHERE change_id=?", (planted, a))
    s, o, _ = w.call("POST", f"/me/changes/{b}/approve", {"channel_id": ch, "proof": proof}, token=otok)
    assert (s, o) == (409, {"error": "replay"})
    assert row(w, b)["state"] == "pending" and not has_grant(w, WS2, "readers", "read")


def test_an_expired_change_is_refused_even_with_a_proof_made_before(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    otok, signer, ch = owner_session(w, w.olive)
    proof = w.proof(signer, prepare(w, otok, cid)[1]["digest"])
    before = snapshot(w)
    w.clock.advance(TTL_MS + 1)
    otok = w.login(w.olive)  # sessions last minutes, the change 14 days
    s, o, _ = w.call("POST", f"/me/changes/{cid}/approve", {"channel_id": ch, "proof": proof}, token=otok)
    assert (s, o) == (410, {"error": "expired"})
    assert snapshot(w) == before and row(w, cid)["state"] == "expired"
    assert len(activity(w, "change.expired")) == 1
    assert prepare(w, otok, cid)[:2] == (410, {"error": "expired"})


def test_the_last_moment_before_expiry_still_works(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    otok, signer, ch = owner_session(w, w.olive)
    w.clock.advance(TTL_MS - 5 * 60_000)  # the owner's sign-in must be fresh: it is, just
    otok = w.login(w.olive)
    assert approve(w, otok, cid, signer, ch)[0] == 200


def test_a_stale_snapshot_is_refused_and_the_proposer_proposes_again(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, "devs", "edit")
    otok, signer, ch = owner_session(w, w.olive)
    prep = prepare(w, otok, cid)[1]
    proof = w.proof(signer, prep["digest"])
    acc(w).add_member("readers", w.newbie)
    acc(w).grant(WS2, "readers", "read")  # a role change for WS2 since the proposal
    shown = w.call("GET", "/me/changes", token=otok)[1]["pending"][0]
    assert shown["approvals"][0]["stale"] is True  # the page can say that access changed
    before = snapshot(w)
    s, o, _ = w.call("POST", f"/me/changes/{cid}/approve", {"channel_id": ch, "proof": proof}, token=otok)
    assert (s, o) == (409, {"error": "stale_change"})
    assert snapshot(w) == before and not has_grant(w, WS2, "devs", "edit")
    assert row(w, cid)["state"] == "rejected" and row(w, cid)["reason"] == "stale_change"
    assert w.changes._db.approvals(cid)[0]["state"] == "stale"
    assert w.call("GET", "/me/changes", token=otok)[1]["pending"] == []
    assert approve_prepare_status(w, otok, cid) == 409  # nothing is left to approve
    again = propose(w, tok, WS2, "devs", "edit")  # a new proposal on the new state
    assert again != cid and approve(w, otok, again, signer, ch)[0] == 200
    assert has_grant(w, WS2, "devs", "edit")


def approve_prepare_status(w, tok, cid):
    return prepare(w, tok, cid)[0]


def test_stale_is_checked_when_preparing_too(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    otok, _signer, _ch = owner_session(w, w.olive)
    acc(w).add_member("readers", w.newbie)
    acc(w).grant(WS2, "readers", "read")
    assert prepare(w, otok, cid)[:2] == (409, {"error": "stale_change"})
    assert row(w, cid)["state"] == "rejected"


def test_the_approver_must_be_the_owner(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    omar_tok, omar_signer, omar_ch = owner_session(w, w.omar)
    before = snapshot(w)
    # omar owns another workspace, not this one; the hub admin who proposed it is not the owner either; nor is ann
    for tok in (omar_tok, w.login(w.admin), w.login(w.ann)):
        for path, body in (
            (f"/me/changes/{cid}/approve/prepare", {}),
            (f"/me/changes/{cid}/approve", {"channel_id": omar_ch, "proof": w.proof(omar_signer, "11" * 32)}),
            (f"/me/changes/{cid}/reject", {}),
        ):
            assert w.call("POST", path, body, token=tok)[:2] == (404, {"error": "unknown_change"}), path
    named = {"workspace_id": WS2, "channel_id": omar_ch, "proof": w.proof(omar_signer, "11" * 32)}
    assert w.call("POST", f"/me/changes/{cid}/approve", named, token=omar_tok)[:2] == (404, {"error": "unknown_change"})
    assert snapshot(w) == before and row(w, cid)["state"] == "pending"


def test_the_owner_is_read_at_approval_time(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    old_tok, old_signer, old_ch = owner_session(w, w.olive)
    new_tok, new_signer, new_ch = owner_session(w, w.omar)
    w.reg.add(WS2, "full", owner=w.omar, name="Orchard")  # the owner alias moved (a new join) since the proposal
    assert prepare(w, old_tok, cid)[:2] == (404, {"error": "unknown_change"})  # olive is no longer the owner
    body = {"channel_id": old_ch, "proof": w.proof(old_signer, "11" * 32)}
    assert w.call("POST", f"/me/changes/{cid}/approve", body, token=old_tok)[:2] == (404, {"error": "unknown_change"})
    assert row(w, cid)["state"] == "pending" and not has_grant(w, WS2, "devs", "edit")
    assert approve(w, new_tok, cid, new_signer, new_ch)[0] == 200
    assert has_grant(w, WS2, "devs", "edit")


def test_the_channel_must_be_the_owners_own_and_not_revoked(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    otok, signer, ch = owner_session(w, w.olive)
    _atok, asigner, ach = owner_session(w, w.ann)
    s, o, _ = approve(w, otok, cid, asigner, ach)  # ann's channel, ann's key, olive's session
    assert (s, o) == (403, {"error": "channel_revoked"})
    assert row(w, cid)["state"] == "pending"
    prep = prepare(w, otok, cid)[1]
    proof = w.proof(signer, prep["digest"])  # a good proof, made while the channel was alive
    assert w.call("POST", f"/channels/{ch}/revoke", {}, token=otok)[0] == 200
    # a revoked channel moves the hub-wide revoked list, so the snapshot version the owner saw is gone: the whole change
    # is stale and nothing is applied, whatever the proof says
    s, o, _ = w.call("POST", f"/me/changes/{cid}/approve", {"channel_id": ch, "proof": proof}, token=otok)
    assert (s, o) == (409, {"error": "stale_change"})
    assert row(w, cid)["state"] == "rejected" and not has_grant(w, WS2, "devs", "edit")
    # and the proof check itself refuses a revoked channel
    with pytest.raises(Refused) as e:
        check_channel_proof(
            keys=w.env.keys,
            channels=w.env.svc,
            user_id=w.olive,
            channel_id=ch,
            digest=bytes.fromhex(prep["digest"]),
            proof=proof,
            now_ms=w.clock(),
        )
    assert e.value.code == "channel_revoked"


def test_the_factor_rule_is_the_custodial_one(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    otok, signer, ch = owner_session(w, w.olive)
    digest = bytes.fromhex(prepare(w, otok, cid)[1]["digest"])
    proof = w.proof(signer, digest.hex())
    kw = {"keys": w.env.keys, "channels": w.env.svc, "user_id": w.olive, "channel_id": ch, "digest": digest}
    # a browser key is password+totp: enough for the minimum, not for a stronger factor
    assert len(check_channel_proof(**kw, proof=proof, now_ms=w.clock())) == 64
    with pytest.raises(Refused) as e:
        check_channel_proof(**kw, proof=proof, now_ms=w.clock(), min_factor="passkey")
    assert e.value.code == "factor_too_weak"
    with pytest.raises(Refused) as e:  # the proof is over THIS digest only
        check_channel_proof(**{**kw, "digest": b"\x00" * 32}, proof=proof, now_ms=w.clock())
    assert e.value.code == "bad_channel_proof"


def test_the_owner_can_say_no(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    otok, signer, ch = owner_session(w, w.olive)
    before = snapshot(w)
    assert w.call("POST", f"/me/changes/{cid}/reject", {}, token=otok)[:2] == (200, {"ok": True})
    assert snapshot(w) == before
    assert row(w, cid)["state"] == "rejected" and w.changes._db.approvals(cid)[0]["state"] == "rejected"
    assert len(activity(w, "change.rejected")) == 1
    assert approve_status(w, otok, cid, signer, ch) == 409  # a rejected change is over


def approve_status(w, tok, cid, signer, ch):
    return w.call(
        "POST", f"/me/changes/{cid}/approve", {"channel_id": ch, "proof": w.proof(signer, "22" * 32)}, token=tok
    )[0]


def test_rejecting_needs_a_registered_channel(w):
    nochan = w.env.user("nochan")
    w.reg.add(WS4, "full", owner=nochan, name="Barn")
    cid = propose(w, w.login(w.admin), WS4, "devs", "edit")
    tok = w.login(nochan)
    assert w.call("POST", f"/me/changes/{cid}/reject", {}, token=tok)[:2] == (403, {"error": "no_channel"})
    assert row(w, cid)["state"] == "pending"
    w.register_channel(tok, nochan)
    assert w.call("POST", f"/me/changes/{cid}/reject", {}, token=tok)[0] == 200


def test_the_proposer_must_still_be_a_hub_admin_when_it_is_applied(w, monkeypatch):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    otok, signer, ch = owner_session(w, w.olive)
    monkeypatch.setattr(w.env.acc, "is_hub_admin", lambda u: u != w.admin)
    s, o, _ = approve(w, otok, cid, signer, ch)
    assert (s, o) == (409, {"error": "proposer_gone"})
    assert not has_grant(w, WS2, "devs", "edit") and row(w, cid)["state"] == "rejected"


def test_the_approve_body_must_be_well_formed(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    otok, _signer, ch = owner_session(w, w.olive)
    for body in (
        {},
        {"channel_id": ch},
        {"proof": "x"},
        {"channel_id": 5, "proof": "x"},
        {"channel_id": ch, "proof": 5},
    ):
        assert w.call("POST", f"/me/changes/{cid}/approve", body, token=otok)[:2] == (400, {"error": "malformed"}), body
    assert w.call("POST", f"/me/changes/{'Z' * 26}/approve/prepare", {}, token=otok)[:2] == (
        404,
        {"error": "unknown_change"},
    )
    assert w.call("POST", f"/me/changes/{'a' * 32}/approve/prepare", {}, token=otok)[:2] == (
        404,
        {"error": "unknown_change"},
    )


# --- the owner's page is scoped by the session person ------------------------------------------------------------------------


def test_the_owner_view_is_scoped_by_the_session_alone(w):
    tok = w.login(w.admin)
    c2 = propose(w, tok, WS2, "devs", "edit")
    c3 = propose(w, tok, WS3, "devs", "edit")
    olive, omar = w.login(w.olive), w.login(w.omar)
    mine = w.call("GET", "/me/changes", token=olive)[1]
    assert [c["id"] for c in mine["pending"]] == [c2] and mine["notices"] == []
    assert [c["id"] for c in w.call("GET", "/me/changes", token=omar)[1]["pending"]] == [c3]
    # there is no user parameter: asking for someone else's list gives the session person's own
    other = w.call("GET", f"/me/changes?user_id={w.omar}", token=olive)[1]
    assert [c["id"] for c in other["pending"]] == [c2]
    assert w.call("GET", "/me/changes", token=w.login(w.ann))[1] == {"pending": [], "notices": []}
    view = mine["pending"][0]
    assert (
        view["proposer"] == {"user_id": w.admin, "name": "admin"} and view["group"] == "devs" and view["role"] == "edit"
    )
    assert view["approvals"] == [
        {
            "delta": [{"user_id": w.dana, "role": "edit"}],
            "people": [{"user_id": w.dana, "role": "edit", "name": "dana"}],
            "workspace_id": WS2,
            "workspace_name": "Orchard",
            "owner_user_id": w.olive,
            "owner_name": "olive",
            "state": "pending",
            "stale": False,
        }
    ]
    # nor can one owner touch the other's change by its id
    for path in (f"/me/changes/{c3}/approve/prepare", f"/me/changes/{c3}/reject"):
        assert w.call("POST", path, {}, token=olive)[:2] == (404, {"error": "unknown_change"})
    assert row(w, c3)["state"] == "pending"


def test_a_multi_workspace_view_shows_the_owner_only_their_own_workspace(w):
    acc(w).grant(WS2, "devs", "edit")
    acc(w).grant(WS3, "devs", "read")
    cid = w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=w.login(w.admin))[1]["change_id"]
    mine = w.call("GET", "/me/changes", token=w.login(w.olive))[1]["pending"]
    assert [c["id"] for c in mine] == [cid]
    assert [a["workspace_id"] for a in mine[0]["approvals"]] == [WS2]
    assert mine[0]["person_name"] == "newbie"
    assert "Meadow" not in json.dumps(mine) and "omar" not in json.dumps(mine)


# --- several workspaces: every owner must approve ----------------------------------------------------------------------------


def test_membership_in_several_workspaces_needs_every_owner(w):
    acc(w).grant(WS2, "devs", "edit")
    acc(w).grant(WS3, "devs", "read")
    s, o, _ = w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=w.login(w.admin))
    assert s == 202
    assert o["waiting_for"] == [
        {"workspace_id": WS2, "owner_name": "olive"},
        {"workspace_id": WS3, "owner_name": "omar"},
    ]
    cid = o["change_id"]
    otok, osig, och = owner_session(w, w.olive)
    mtok, msig, mch = owner_session(w, w.omar)
    s, got, _ = approve(w, otok, cid, osig, och)
    assert (s, got) == (200, {"ok": True, "state": "pending", "remaining": 1})
    assert acc(w).members("devs") == [w.dana]  # one of two is not enough
    assert row(w, cid)["state"] == "pending"
    s, got, _ = approve(w, mtok, cid, msig, mch)
    assert (s, got) == (200, {"ok": True, "state": "applied", "remaining": 0})
    assert sorted(acc(w).members("devs")) == sorted([w.dana, w.newbie]) and row(w, cid)["state"] == "applied"
    assert len(activity(w, "change.approved")) == 2 and len(activity(w, "change.proposed")) == 2  # one per workspace


def test_one_rejection_rejects_a_membership_change(w):
    acc(w).grant(WS2, "devs", "edit")
    acc(w).grant(WS3, "devs", "read")
    cid = w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=w.login(w.admin))[1]["change_id"]
    otok, osig, och = owner_session(w, w.olive)
    mtok, _msig, _mch = owner_session(w, w.omar)
    assert approve(w, otok, cid, osig, och)[1]["remaining"] == 1
    assert w.call("POST", f"/me/changes/{cid}/reject", {}, token=mtok)[0] == 200
    assert row(w, cid)["state"] == "rejected" and acc(w).members("devs") == [w.dana]
    assert w.call("POST", f"/me/changes/{cid}/approve/prepare", {}, token=otok)[:2] == (409, {"error": "not_pending"})


def test_an_owner_of_several_affected_workspaces_signs_each_one(w):
    w.reg.add(WS3, "full", owner=w.olive, name="Meadow")  # olive owns both now
    acc(w).grant(WS2, "devs", "edit")
    acc(w).grant(WS3, "devs", "read")
    cid = w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=w.login(w.admin))[1]["change_id"]
    otok, osig, och = owner_session(w, w.olive)
    assert prepare(w, otok, cid)[:2] == (400, {"error": "malformed"})  # which workspace?
    first = approve(w, otok, cid, osig, och, WS2)
    assert first[:2] == (200, {"ok": True, "state": "pending", "remaining": 1})
    d2, d3 = (prepare(w, otok, cid, ws)[1]["digest"] for ws in (WS3, WS3))
    assert d2 == d3
    # the proof for one workspace is no proof for the other
    s, o, _ = approve(w, otok, cid, osig, och, WS3, proof=w.proof(osig, "33" * 32))
    assert (s, o) == (403, {"error": "bad_channel_proof"})
    assert approve(w, otok, cid, osig, och, WS3)[1]["state"] == "applied"
    assert sorted(acc(w).members("devs")) == sorted([w.dana, w.newbie])


def test_a_second_owner_whose_workspace_moved_stales_the_whole_change(w):
    acc(w).grant(WS2, "devs", "edit")
    acc(w).grant(WS3, "devs", "read")
    cid = w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=w.login(w.admin))[1]["change_id"]
    otok, osig, och = owner_session(w, w.olive)
    mtok, msig, mch = owner_session(w, w.omar)
    assert approve(w, otok, cid, osig, och)[1]["remaining"] == 1
    proof = w.proof(msig, prepare(w, mtok, cid)[1]["digest"])  # omar looked at it and signed
    acc(w).add_member("readers", w.newbie)
    acc(w).grant(WS3, "readers", "read")  # WS3's roles moved before omar's answer arrived
    body = {"channel_id": mch, "proof": proof}
    assert w.call("POST", f"/me/changes/{cid}/approve", body, token=mtok)[:2] == (409, {"error": "stale_change"})
    assert acc(w).members("devs") == [w.dana] and row(w, cid)["state"] == "rejected"


# --- condition 9: override ------------------------------------------------------------------------------------------------------


def test_override_applies_audits_with_the_reason_and_tells_the_owner(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, "devs", "edit")
    reason = "Release tonight: Olive is on leave"
    head = w.env.audit.store.head()["seq"]
    s, o, _ = w.call("POST", f"/admin/changes/{cid}/override", {"reason": reason}, token=tok)
    assert (s, o) == (200, {"ok": True})
    assert has_grant(w, WS2, "devs", "edit")
    c = row(w, cid)
    assert (c["state"], c["decided_by"], c["reason"]) == ("overridden", w.admin, reason)
    assert w.env.audit.store.head()["seq"] == head + 1
    (rec,) = activity(w, "change.overridden")
    assert rec["actor_id"] == w.admin and rec["workspace_id"] == WS2
    assert rec["detail"] == {"change_id": cid, "reason": reason}
    assert w.activity.verify()
    # the owner sees that it was applied without their approval; nobody else does
    notices = w.call("GET", "/me/changes", token=w.login(w.olive))[1]["notices"]
    assert [n["id"] for n in notices] == [cid]
    assert (notices[0]["reason"], notices[0]["decided_by"], notices[0]["state"]) == (reason, "admin", "overridden")
    assert w.call("GET", "/me/changes", token=w.login(w.omar))[1]["notices"] == []
    assert w.call("GET", "/me/changes", token=w.login(w.ann))[1]["notices"] == []
    # a second override of the same change is refused
    assert w.call("POST", f"/admin/changes/{cid}/override", {"reason": reason}, token=tok)[:2] == (
        409,
        {"error": "not_pending"},
    )


def test_override_needs_a_reason_of_three_to_two_hundred_characters(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, "devs", "edit")
    before = snapshot(w)
    for bad in (None, "", "ab", "  a ", "x" * 201, "line\nbreak", "tab\there", 7, ["long enough"]):
        body = {} if bad is None else {"reason": bad}
        assert w.call("POST", f"/admin/changes/{cid}/override", body, token=tok)[:2] == (400, {"error": "malformed"}), (
            bad
        )
    assert w.call("POST", f"/admin/changes/{cid}/override", None, token=tok)[0] == 400
    assert snapshot(w) == before and row(w, cid)["state"] == "pending" and activity(w, "change.overridden") == []
    assert w.call("POST", f"/admin/changes/{cid}/override", {"reason": "abc"}, token=tok)[0] == 200  # 3 is enough
    other = propose(w, tok, WS2, "readers", "read")
    assert w.call("POST", f"/admin/changes/{other}/override", {"reason": "é" * 200}, token=tok)[0] == 200  # 200 is too
    assert activity(w, "change.overridden")[0]["detail"]["reason"] == "é" * 200


def test_only_a_hub_admin_overrides_and_only_a_known_change(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    for tok in (w.login(w.olive), w.login(w.ann)):
        assert w.call("POST", f"/admin/changes/{cid}/override", {"reason": "because"}, token=tok)[:2] == (
            403,
            {"error": "forbidden_role"},
        )
    admin = w.login(w.admin)
    assert w.call("POST", f"/admin/changes/{'0' * 26}/override", {"reason": "because"}, token=admin)[0] == 404
    assert row(w, cid)["state"] == "pending"


def test_override_never_removes_the_owners_admin_access(w):
    a = acc(w)
    a.remove_member("owners", w.olive)  # olive: only read, through devs
    a.add_member("devs", w.olive)
    a.grant(WS2, "devs", "read")
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, "devs", "edit")  # waiting for olive
    a.grant(WS2, "devs", "admin")  # by now devs is the owner's admin access (e.g. set by the local operator)
    before = snapshot(w)
    s, o, _ = w.call("POST", f"/admin/changes/{cid}/override", {"reason": "force it through"}, token=tok)
    assert (s, o) == (409, {"error": "owner_protected"})  # applying 'edit' would demote her from admin
    assert snapshot(w) == before and row(w, cid)["state"] == "pending"
    assert activity(w, "change.overridden") == []


# --- condition 10: expiry --------------------------------------------------------------------------------------------------------


def test_expiry_is_swept_once_and_recorded(w):
    tok = w.login(w.admin)
    c2, c3 = propose(w, tok, WS2, "devs", "edit"), propose(w, tok, WS3, "devs", "edit")
    w.clock.advance(TTL_MS - 1)
    assert w.changes.sweep() == 0
    w.clock.advance(1)
    before = snapshot(w)
    assert w.changes.sweep() == 2
    assert w.changes.sweep() == 0  # nothing is recorded twice
    assert (row(w, c2)["state"], row(w, c3)["state"]) == ("expired", "expired")
    assert snapshot(w) == before
    assert sorted(r["workspace_id"] for r in activity(w, "change.expired")) == [WS2, WS3]
    assert all(r["actor_kind"] == "hub" for r in activity(w, "change.expired"))


def test_expiry_is_swept_lazily_when_the_owner_looks(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    w.clock.advance(TTL_MS)
    otok = w.login(w.olive)
    assert w.call("GET", "/me/changes", token=otok)[1] == {"pending": [], "notices": []}
    assert row(w, cid)["state"] == "expired" and len(activity(w, "change.expired")) == 1
    admin_list = w.call("GET", "/admin/changes", token=w.login(w.admin))[1]["changes"]
    assert [(c["id"], c["state"]) for c in admin_list] == [(cid, "expired")]


def test_an_expired_change_is_never_applied(w):
    cid = propose(w, w.login(w.admin), WS2, "devs", "edit")
    c = row(w, cid)
    w.clock.advance(TTL_MS + 1)
    # not by override, not by cancel ...
    atok = w.login(w.admin)
    assert w.call("POST", f"/admin/changes/{cid}/override", {"reason": "too late"}, token=atok)[:2] == (
        410,
        {"error": "expired"},
    )
    assert w.call("POST", f"/admin/changes/{cid}/cancel", None, token=atok)[:2] == (410, {"error": "expired"})
    assert not has_grant(w, WS2, "devs", "edit") and row(w, cid)["state"] == "expired"
    # ... and not by the apply step itself, whatever state the row was in
    with pytest.raises(ChangeError) as e:
        w.changes._apply(c, json.loads(c["body"]), actor=w.admin, approved=None)
    assert e.value.code == "expired" and not has_grant(w, WS2, "devs", "edit")


# --- cancel --------------------------------------------------------------------------------------------------------------------------


def test_the_proposer_or_a_hub_admin_cancels(w):
    other = w.env.user("admin2")
    acc(w).add_member("hub-admin", other)
    t1, t2 = w.login(w.admin), w.login(other)
    a, b = propose(w, t1, WS2, "devs", "edit"), propose(w, t1, WS3, "devs", "edit")
    assert w.call("POST", f"/admin/changes/{a}/cancel", None, token=t1)[:2] == (200, {"ok": True})  # the proposer
    assert w.call("POST", f"/admin/changes/{b}/cancel", None, token=t2)[:2] == (200, {"ok": True})  # another admin
    assert (row(w, a)["state"], row(w, b)["state"]) == ("cancelled", "cancelled")
    assert w.call("POST", f"/admin/changes/{a}/cancel", None, token=t1)[:2] == (409, {"error": "not_pending"})
    assert w.call("POST", f"/admin/changes/{a}/cancel", None, token=w.login(w.olive))[:2] == (
        403,
        {"error": "forbidden_role"},
    )
    assert len(activity(w, "change.cancelled")) == 2
    # a cancelled change cannot be approved
    otok, _osig, _och = owner_session(w, w.olive)
    assert w.call("POST", f"/me/changes/{a}/approve/prepare", {}, token=otok)[:2] == (409, {"error": "not_pending"})
    assert not has_grant(w, WS2, "devs", "edit")


def test_the_admin_list_shows_what_waits_and_for_whom(w):
    acc(w).grant(WS2, "devs", "edit")
    tok = w.login(w.admin)
    cid = propose(w, tok, WS3, "readers", "read")
    m = w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=tok)[1]["change_id"]
    rows = w.call("GET", "/admin/changes", token=tok)[1]["changes"]
    assert {r["id"] for r in rows} == {cid, m} and all(r["state"] == "pending" for r in rows)
    grant = next(r for r in rows if r["id"] == cid)
    assert grant["approvals"][0]["owner_name"] == "omar" and grant["approvals"][0]["workspace_name"] == "Meadow"
    assert grant["proposer"]["name"] == "admin" and grant["expires_ms"] - grant["created_ms"] == TTL_MS
    assert w.call("GET", "/admin/changes", token=w.login(w.olive))[:2] == (403, {"error": "forbidden_role"})


# --- void_for_user (the user-disable slice calls this) ----------------------------------------------------------------------------------


def test_void_for_user_cancels_proposals_and_rejects_approvals_waiting_on_them(w):
    other = w.env.user("admin2")
    acc(w).add_member("hub-admin", other)
    by_admin = propose(w, w.login(w.admin), WS2, "devs", "edit")  # admin proposed, olive is asked
    by_other = propose(w, w.login(other), WS3, "devs", "edit")  # admin2 proposed, omar is asked
    third = propose(w, w.login(other), WS2, "readers", "read")  # admin2 proposed, olive is asked
    assert w.changes.void_for_user(w.admin) == {"cancelled": 1, "rejected": 0}
    assert row(w, by_admin)["state"] == "cancelled"
    assert (row(w, by_other)["state"], row(w, third)["state"]) == ("pending", "pending")
    assert w.changes.void_for_user(w.omar) == {"cancelled": 0, "rejected": 1}
    assert row(w, by_other)["state"] == "rejected" and w.changes._db.approvals(by_other)[0]["state"] == "rejected"
    assert row(w, third)["state"] == "pending"  # olive is still there
    assert w.changes.void_for_user(w.olive) == {"cancelled": 0, "rejected": 1}
    assert row(w, third)["state"] == "rejected"
    assert w.changes.void_for_user(w.olive) == {"cancelled": 0, "rejected": 0}
    assert not any(has_grant(w, ws, g, r) for ws, g, r in ((WS2, "devs", "edit"), (WS3, "devs", "edit")))


# --- condition 8: the owner alias is never editable by an admin -------------------------------------------------------------------------


PKG = Path(orch_hub.__file__).parent
WRITES_ALIAS = re.compile(r"(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+owner_alias", re.IGNORECASE)


def test_the_owner_alias_is_written_at_join_only():
    files = {p.relative_to(PKG).as_posix(): p.read_text() for p in PKG.rglob("*.py")}
    # 1. the only file that writes the table is the registry; inside it only the join and the workspace's own removal
    assert {f for f, src in files.items() if WRITES_ALIAS.search(src)} == {"link/registry.py"}
    reg = files["link/registry.py"]
    funcs = {
        n.name
        for n in ast.walk(ast.parse(reg))
        if isinstance(n, ast.FunctionDef) and WRITES_ALIAS.search(ast.get_source_segment(reg, n) or "")
    }
    assert funcs == {"commit_join", "forget"}
    # 2. commit_join (which sets it) is called from the join op and nowhere else
    callers = {}
    for f, src in files.items():
        for fn in ast.walk(ast.parse(src)):
            if isinstance(fn, ast.FunctionDef):
                for call in ast.walk(fn):
                    if isinstance(call, ast.Attribute) and call.attr == "commit_join":
                        callers.setdefault(f, set()).add(fn.name)
    assert callers == {"link/ops.py": {"join"}}
    # 3. no route can write it: no route has an owner in its path, none reaches a writer, the one that reaches `forget`
    #    removes a workspace that has left (the alias goes with it, it is not edited)
    reaching = {}
    for r in ROUTES:
        assert "owner" not in r.path, r.path
        src = inspect.getsource(getattr(WebApi, "_h_" + r.handler))
        assert "commit_join" not in src and not WRITES_ALIAS.search(src) and "owner_alias=" not in src, r.handler
        if re.search(r"\.forget\(", src):
            reaching[r.handler] = r.path
    assert reaching == {"a_workspace_remove": "/api/v1/admin/workspaces/{workspace_id}"}
    # 4. the changes module, the admin writers and the new handlers never name the registry's writers either
    for f, src in files.items():
        if f.startswith(("changes/", "webapi/changes_api")):
            assert "commit_join" not in src and "forget(" not in src and not WRITES_ALIAS.search(src), f


def test_no_admin_route_takes_an_owner_in_its_body(w):
    tok = w.login(w.admin)
    before = w.reg.owner_alias(WS2)
    for path, body in (
        ("/admin/grants", {"workspace_id": WS2, "group": "devs", "role": "read", "owner_alias": w.omar}),
        ("/admin/connect", {"owner_alias": w.omar}),
    ):
        w.call("PUT" if path == "/admin/grants" else "POST", path, body, token=tok)
    assert w.reg.owner_alias(WS2) == before


# --- labels ---------------------------------------------------------------------------------------------------------------------------------


def test_the_access_change_label_is_prefix_free_with_every_label():
    assert ACCESS_CHANGE_LABEL.endswith("|") and ACCESS_CHANGE_LABEL.startswith("orch/v2/")
    others = {*V2, *HUB_LABELS.values(), *V2_REUSED.values()}
    assert ACCESS_CHANGE_LABEL not in others
    for other in others:
        assert not other.startswith(ACCESS_CHANGE_LABEL) and not ACCESS_CHANGE_LABEL.startswith(other), other


# --- the structural invariant: an owner approval is for the change in EFFECTIVE reach (reach_delta) ----------------------


def reach(w, ws, user):
    return acc(w).roles(ws).get(user)


def test_adding_a_person_to_the_hub_admin_group_needs_every_owners_approval(w):
    from orch_hub.access.store import ADMIN_GROUP

    tok = w.login(w.admin)
    before = snapshot(w)
    s, o, _ = w.call("PUT", f"/admin/groups/{ADMIN_GROUP}/members/{w.newbie}", token=tok)
    assert s == 202, o
    waiting = {x["workspace_id"] for x in o["waiting_for"]}
    assert {WS2, WS3} <= waiting  # every full workspace with an owner other than the actor
    assert snapshot(w) == before and reach(w, WS2, w.newbie) is None
    # the owners see the PERSON who would gain access, not just a group name
    mine = w.call("GET", "/me/changes", token=w.login(w.olive))[1]["pending"]
    assert [p["user_id"] for p in mine[0]["approvals"][0]["delta"]] == [w.newbie]
    assert mine[0]["approvals"][0]["delta"][0]["role"] == "admin"
    # one owner is not enough
    otok, osig, och = owner_session(w, w.olive)
    assert approve(w, otok, o["change_id"], osig, och)[1]["state"] == "pending"
    assert reach(w, WS3, w.newbie) is None
    mtok, msig, mch = owner_session(w, w.omar)
    others = [x for x in waiting if x not in (WS2, WS3)]
    assert approve(w, mtok, o["change_id"], msig, mch)[1]["state"] in ("pending", "applied")
    if not others:
        assert reach(w, WS2, w.newbie) == "admin" and reach(w, WS3, w.newbie) == "admin"


def test_a_member_added_while_a_grant_is_pending_makes_the_approval_stale(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, "devs", "edit")  # the owner is shown: dana
    # newbie joins devs now: devs holds no grant yet, so nobody gains reach and it applies at once ...
    assert w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=tok)[0] == 200
    assert reach(w, WS2, w.newbie) is None
    # ... but approving the grant would now also give newbie edit, which the owner never saw: refused, re-review
    otok, osig, och = owner_session(w, w.olive)
    s, o, _ = w.call("POST", f"/me/changes/{cid}/approve/prepare", {}, token=otok)
    assert (s, o) == (409, {"error": "stale_change"})
    assert reach(w, WS2, w.newbie) is None and not has_grant(w, WS2, "devs", "edit")
    # asking again shows both people
    cid2 = propose(w, tok, WS2, "devs", "edit")
    ap = w.call("GET", "/me/changes", token=otok)[1]["pending"][0]["approvals"][0]
    assert {d["user_id"] for d in ap["delta"]} == {w.dana, w.newbie}
    assert approve(w, otok, cid2, osig, och)[1]["state"] == "applied"
    assert reach(w, WS2, w.newbie) == "edit"


@pytest.mark.parametrize("grant_first", [True, False])
def test_grant_and_member_add_in_either_order_never_give_unapproved_reach(w, grant_first):
    tok = w.login(w.admin)
    otok, osig, och = owner_session(w, w.olive)
    pending = []
    steps = [
        lambda: w.call("PUT", "/admin/grants", {"workspace_id": WS2, "group": "devs", "role": "edit"}, token=tok),
        lambda: w.call("PUT", f"/admin/groups/devs/members/{w.newbie}", token=tok),
    ]
    for step in steps if grant_first else steps[::-1]:
        s, o, _ = step()
        if s == 202:
            pending.append(o["change_id"])
    # whatever happened, newbie has no role in the owner's workspace until the owner approved what includes them
    assert reach(w, WS2, w.newbie) is None
    for cid in pending:
        s, o, _ = w.call("POST", f"/me/changes/{cid}/approve/prepare", {}, token=otok)
        if s == 200:
            assert approve(w, otok, cid, osig, och)[0] == 200
        else:
            assert (s, o) == (409, {"error": "stale_change"})
        # at no point did an unapproved person gain reach
    for cid in [c["id"] for c in w.call("GET", "/me/changes", token=otok)[1]["pending"]]:
        assert w.call("POST", f"/me/changes/{cid}/reject", {}, token=otok)[0] == 200
    assert reach(w, WS2, w.newbie) in (None, "edit")
    if reach(w, WS2, w.newbie) == "edit":
        # it only happened through a change whose delta named newbie and that the owner approved
        done = [c for c in w.changes._db.changes() if c["state"] == "applied"]
        assert done and any(
            w.newbie in {d["user_id"] for d in json.loads(a["delta"])}
            for c in done
            for a in w.changes._db.approvals(c["id"])
        )


def test_roles_overlay_is_exactly_roles_after_the_edit_is_applied():
    """Property: for random edits, the simulated roles equal roles() once the edit is really applied (one computation)."""
    import random

    from orch_hub.access import Access
    from orch_hub.access.store import ADMIN_GROUP, AccessError

    rng = random.Random(1028)
    wss = [f"{n:02x}" * 16 for n in (0xA1, 0xB2, 0xC3)]
    for _ in range(60):
        a = Access(":memory:")
        users = [f"{n:02x}" * 16 for n in range(1, 6)]
        groups = ["g1", "g2", "g3"]
        for u in users:
            a.add_user(u, "u" + u[:2])
        for g in groups:
            a.add_group(g)
        for u in users:
            for g in rng.sample([*groups, ADMIN_GROUP], rng.randint(0, 2)):
                a.add_member(g, u)
        for ws in wss:
            for g in rng.sample(groups, rng.randint(0, 3)):
                a.grant(ws, g, rng.choice(["read", "edit", "admin"]))
        for _step in range(6):
            kind = rng.choice(["set_grant", "drop_grant", "drop_member", "add_member", "drop_group", "drop_user"])
            ws0, g, u = rng.choice(wss), rng.choice([*groups, ADMIN_GROUP]), rng.choice(users)
            role = rng.choice(["read", "edit", "admin"])
            ggrant = rng.choice(groups)  # a grant is never made to the implicit admin group
            args = {
                "set_grant": {"set_grant": (ggrant, role)},
                "drop_grant": {"drop_grant": ggrant},
                "drop_member": {"drop_member": (g, u)},
                "add_member": {"add_member": (g, u)},
                "drop_group": {"drop_group": ggrant},
                "drop_user": {"drop_user": u},
            }[kind]
            predicted = {ws: a.roles_overlay(ws, **args) for ws in wss}
            try:
                if kind in ("set_grant", "drop_grant"):  # a grant is per workspace: only ws0 changes
                    predicted = {ws: a.roles_overlay(ws, **(args if ws == ws0 else {})) for ws in wss}
                    if kind == "set_grant":
                        a.grant(ws0, ggrant, role)
                    else:
                        a.revoke(ws0, ggrant)
                elif kind == "drop_member":
                    a.remove_member(g, u)
                elif kind == "add_member":
                    a.add_member(g, u)
                elif kind == "drop_group":
                    a.remove_group(ggrant)
                else:
                    a.remove_user(u)
            except AccessError:
                continue  # a refused edit changes nothing (the overlay is only asked about what would apply)
            assert {ws: a.roles(ws) for ws in wss} == predicted, (kind, args)
        a.close()


def test_override_reaches_a_workspace_bound_after_the_proposal(w):
    """Opus blocker: an override is judged by what the change does NOW; every owner it reaches gets the record and notice."""
    from orch_hub.access.store import ADMIN_GROUP

    tok = w.login(w.admin)
    s, o, _ = w.call("PUT", f"/admin/groups/{ADMIN_GROUP}/members/{w.newbie}", token=tok)
    assert s == 202
    cid = o["change_id"]
    nora, ws5 = w.env.user("nora"), "d4" * 16
    w.reg.add(ws5, "full", owner=nora, name="Late")  # bound AFTER the proposal: no approval row names it
    assert ws5 not in {a["workspace_id"] for a in w.changes._db.approvals(cid)}
    assert w.call("POST", f"/admin/changes/{cid}/override", {"reason": "release tonight"}, token=tok)[0] == 200
    assert reach(w, ws5, w.newbie) == "admin"
    # the activity record exists for the late workspace ...
    rows = [e for e in w.activity.list(limit=200) if e["type"] == "change.overridden"]
    assert ws5 in {e["workspace_id"] for e in rows} and {WS2, WS3} <= {e["workspace_id"] for e in rows}
    # ... and its owner gets the notice WITH the people who really gained access
    notices = w.call("GET", "/me/changes", token=w.login(nora))[1]["notices"]
    assert [n["id"] for n in notices] == [cid]
    ap = notices[0]["approvals"][0]
    assert ap["workspace_id"] == ws5 and [d["user_id"] for d in ap["delta"]] == [w.newbie]
    assert ap["people"][0]["name"] == "newbie"


def test_only_changes_writes_membership_and_grants():
    """AST guard (like the owner_alias one): outside orch_hub/changes, ANY attribute call named ``add_member`` / ``grant`` /
    ``set_role`` is a write to the access model, whatever the receiver is called. The only ones allowed are the operator CLI
    at the hub terminal (state-dir owner), the one-time first-admin setup, and the store itself; and those sites must really
    be there (so the guard cannot go blind)."""
    root = Path(orch_hub.__file__).parent
    watched = {"add_member", "grant", "set_role"}
    allowed = {("webapi/api.py", "_h_setup", "self.d.access"), ("webapi/api.py", "_h_a_grant", "self._chg()")}
    found = set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("changes/") or rel == "access/store.py":
            continue
        tree = ast.parse(path.read_text())
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in watched:
                fn = node
                while fn in parents and not isinstance(fn, ast.FunctionDef):
                    fn = parents[fn]
                found.add((rel, fn.name if isinstance(fn, ast.FunctionDef) else None, ast.unparse(node.func.value)))
    cli_sites = {f for f in found if f[0] == "access/cli.py"}
    unexpected = {f for f in found if f not in allowed and f not in cli_sites}
    assert not unexpected, f"access is written outside Changes: {sorted(unexpected)}"
    assert allowed <= found and cli_sites, f"the guard no longer sees its allowlisted sites: {found}"


def test_setup_refuses_once_an_admin_exists(w, tmp_path):
    token_file = tmp_path / "setup-token"
    token_file.write_text("tok-123\n")
    w.api.d.setup_token_file = token_file  # a leftover token file with an admin already present
    s, o, _ = w.call("POST", "/setup", {"token": "tok-123", "name": "mallory", "password": "x" * 14})
    assert (s, o) == (403, {"error": "not_allowed"})
    assert not any(u["name"] == "mallory" for u in acc(w).users())


def test_a_full_workspace_without_an_owner_alias_fails_closed(w):
    """No alias row: nobody can approve for the workspace, so only a hub admin changes who reaches it."""
    ws6 = "e6" * 16
    w.reg.add(ws6, "full", owner=None, name="Orphan")
    with pytest.raises(ChangeError) as e:
        w.changes.grant(w.newbie, ws6, "devs", "edit")  # a non-admin actor: refused, never "applies at once"
    assert e.value.code == "owner_unknown"
    assert not has_grant(w, ws6, "devs", "edit")
    # a hub admin may still manage it (hub admins manage every workspace)
    assert w.changes.grant(w.admin, ws6, "devs", "edit") is None and has_grant(w, ws6, "devs", "edit")


def test_approval_routes_have_the_same_freshness_gate_as_signing():
    """A session a workspace vouched for (switch ticket, no password sign-in) is read-only: it reaches neither /sign nor
    the owner's approve/prepare/reject, because none of them is in SWITCH_HANDLERS (routes default closed)."""
    from orch_hub.webapi.api import SWITCH_HANDLERS

    gated = {r.handler for r in ROUTES if r.path.startswith("/api/v1/me/changes") or r.path.startswith("/api/v1/sign")}
    assert {"change_prepare", "change_approve", "change_reject", "prepare", "sign"} <= gated
    assert not gated & SWITCH_HANDLERS


def test_a_real_registry_without_an_alias_says_none_not_an_owner():
    from orch_hub.changes import rules as R
    from orch_hub.link import Registry

    reg = Registry(":memory:")
    assert reg.owner_alias("e5" * 16) is None and R.owner_of(reg, "e5" * 16) is None
    assert R.is_full(reg, "e5" * 16) is False  # an unknown workspace never needs, and never skips, an approval


def test_a_switch_ticket_session_is_refused_on_approve_and_prepare(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, "devs", "edit")
    ticket_session = w.env.svc.open_session(w.olive)["token"]  # a workspace vouched for it: no password sign-in
    w.browser_tokens.add(ticket_session)  # the console carries it as its cookie, like a real switch redeem
    assert w.call("GET", "/session", token=ticket_session)[1]["needs_sign_in"] is True
    for path in (f"/me/changes/{cid}/approve/prepare", f"/me/changes/{cid}/approve", f"/me/changes/{cid}/reject"):
        s, o, _ = w.call("POST", path, {}, token=ticket_session)
        assert (s, o["error"]) == (403, "reauth_required"), path
    assert row(w, cid)["state"] == "pending" and not has_grant(w, WS2, "devs", "edit")


def test_an_orphan_full_workspace_bound_after_the_proposal_makes_the_approval_stale(w):
    from orch_hub.access.store import ADMIN_GROUP

    tok = w.login(w.admin)
    s, o, _ = w.call("PUT", f"/admin/groups/{ADMIN_GROUP}/members/{w.newbie}", token=tok)
    assert s == 202
    cid = o["change_id"]
    w.reg.add("f7" * 16, "full", owner=None, name="Orphan")  # bound later, no owner alias: nobody can approve for it
    otok, osig, och = owner_session(w, w.olive)
    mtok, msig, mch = owner_session(w, w.omar)
    assert approve(w, otok, cid, osig, och)[1]["state"] == "pending"
    s, o, _ = w.call(
        "POST",
        f"/me/changes/{cid}/approve",
        {"channel_id": mch, "proof": w.proof(msig, prepare(w, mtok, cid)[1]["digest"])},
        token=mtok,
    )
    assert (s, o) == (409, {"error": "stale_change"})
    assert (
        row(w, cid)["state"] == "rejected" and reach(w, WS2, w.newbie) is None and reach(w, "f7" * 16, w.newbie) is None
    )


def test_a_flag_disabled_member_is_named_in_the_reach_and_re_enable_changes_no_roles(w):
    """A disabled member of the group would regain access if re-enabled: the owner is shown them; and the role map is the
    same before and after an enabling (enable strips groups; it never restores reach)."""
    acc(w).add_member("devs", w.newbie)
    acc(w).set_disabled(w.newbie, True)  # flag only: the stale membership stays
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, "devs", "edit")
    people = w.call("GET", "/me/changes", token=w.login(w.olive))[1]["pending"][0]["approvals"][0]["people"]
    assert {p["user_id"] for p in people} == {w.dana, w.newbie}
    assert reach(w, WS2, w.newbie) is None  # disabled: no role from the hub
    w.call("POST", f"/admin/changes/{cid}/cancel", {}, token=tok)
    from orch_hub.invites import Invites

    w.api.d.user_invites = Invites(":memory:", now_ms=w.clock)  # the enable route issues a recovery link
    before = acc(w).roles(WS2)
    s, _o, _ = w.call("POST", f"/admin/users/{w.newbie}/enable", token=tok)
    assert s == 200
    assert acc(w).roles(WS2) == before and w.newbie not in acc(w).roles(WS2)
    assert acc(w).groups_of(w.newbie) == []


# --- the proposer's own workspaces: exempt only while they STILL own them; the proposer must still stand -----------------


def _admin_group_change(w, own=True):
    """admin owns WSA (so it needs no approval from anyone); olive and omar own WS2/WS3 and must approve."""
    from orch_hub.access.store import ADMIN_GROUP

    wsa = "a9" * 16
    if own:
        w.reg.add(wsa, "full", owner=w.admin, name="Mine")
    tok = w.login(w.admin)
    s, o, _ = w.call("PUT", f"/admin/groups/{ADMIN_GROUP}/members/{w.newbie}", token=tok)
    assert s == 202, o
    return wsa, o["change_id"], tok


def _approve_all(w, cid):
    last = None
    for owner in (w.olive, w.omar):
        otok, osig, och = owner_session(w, owner)
        last = approve(w, otok, cid, osig, och)
    return last


def test_proposer_owned_workspace_is_exempt_while_the_proposer_still_owns_it(w):
    wsa, cid, _tok = _admin_group_change(w)
    assert wsa not in {a["workspace_id"] for a in w.changes._db.approvals(cid)}
    s, o, _ = _approve_all(w, cid)
    assert s == 200 and o["state"] == "applied" and reach(w, wsa, w.newbie) == "admin"


def test_ownership_of_a_proposer_owned_workspace_moving_makes_the_change_stale(w):
    wsa, cid, _tok = _admin_group_change(w)
    w.reg.ws[wsa]["owner"] = w.dana  # someone else owns it now: they never approved, the exemption is gone
    s, o, _ = _approve_all(w, cid)
    assert (s, o) == (409, {"error": "stale_change"})
    assert row(w, cid)["state"] == "rejected" and reach(w, wsa, w.newbie) is None and reach(w, WS2, w.newbie) is None


def test_an_owner_less_workspace_never_matches_the_exemption(w):
    wsa, cid, _tok = _admin_group_change(w)
    w.reg.ws[wsa]["owner"] = None  # no alias now: not "proposer-owned", it is an orphan (hub admin only)
    ch = w.changes._db.change(cid)
    body = json.loads(ch["body"])
    with pytest.raises(ChangeError) as e:
        w.changes._check_reach(
            ch, body, w.newbie, {WS2: w.olive, WS3: w.omar}
        )  # a non-admin applier is refused, never exempt
    assert e.value.code == "owner_unknown"
    w.changes._check_reach(
        ch, body, w.admin, {WS2: w.olive, WS3: w.omar}
    )  # a hub admin may (it was in the proposal's reach)


@pytest.mark.parametrize("how", ["disabled", "demoted"])
def test_the_proposer_must_still_stand_at_finish(w, how, monkeypatch):
    from orch_hub.access.store import ADMIN_GROUP

    # snapshot versions held still, so the finish step itself (not the staleness check before it) is what is under test
    monkeypatch.setattr(w.changes, "_version", lambda ws: 1)
    _wsa, cid, _tok = _admin_group_change(w)
    admin2 = w.env.user("admin2")
    acc(w).add_member(ADMIN_GROUP, admin2)  # so the last-admin guard allows removing the proposer's standing
    if how == "disabled":
        acc(w).set_disabled(w.admin, True)
    else:
        acc(w).remove_member(ADMIN_GROUP, w.admin)
    s, o, _ = _approve_all(w, cid)
    assert (s, o) == (409, {"error": "proposer_gone"})
    assert row(w, cid)["state"] == "rejected" and reach(w, WS2, w.newbie) is None


def test_override_refuses_an_orphan_workspace_bound_after_the_proposal(w):
    _wsa, cid, tok = _admin_group_change(w, own=False)
    w.reg.add("b8" * 16, "full", owner=None, name="Orphan")  # nobody can be told about it
    s, o, _ = w.call("POST", f"/admin/changes/{cid}/override", {"reason": "release tonight"}, token=tok)
    assert (s, o) == (409, {"error": "stale_change"})
    assert row(w, cid)["state"] == "pending" and reach(w, WS2, w.newbie) is None


def test_the_overlay_leaves_a_disabled_member_out_unless_asked(w):
    acc(w).set_disabled(w.newbie, True)
    assert w.newbie not in acc(w).roles_overlay(WS2, add_member=("owners", w.newbie))
    assert acc(w).roles_overlay(WS2, add_member=("owners", w.newbie), include_disabled=True)[w.newbie] == "admin"


def test_an_approval_follows_the_approver_not_the_workspace(w):
    """olive approves WS2, then WS2 moves to dana: olive's yes does not stand for dana."""
    _wsa, cid, _tok = _admin_group_change(w, own=False)
    otok, osig, och = owner_session(w, w.olive)
    assert approve(w, otok, cid, osig, och)[1]["state"] == "pending"
    w.reg.ws[WS2]["owner"] = w.dana  # rebound / re-aliased after olive consented
    mtok, msig, mch = owner_session(w, w.omar)
    s, o, _ = w.call(
        "POST",
        f"/me/changes/{cid}/approve",
        {"channel_id": mch, "proof": w.proof(msig, prepare(w, mtok, cid)[1]["digest"])},
        token=mtok,
    )
    assert (s, o) == (409, {"error": "stale_change"})
    assert row(w, cid)["state"] == "rejected" and reach(w, WS2, w.newbie) is None and reach(w, WS3, w.newbie) is None


def test_an_owner_less_workspace_proposed_as_such_finishes_end_to_end(w):
    orphan = "b7" * 16
    w.reg.add(orphan, "full", owner=None, name="Orphan")  # already owner-less when the hub admin proposes
    _wsa, cid, _tok = _admin_group_change(w, own=False)
    assert orphan not in {a["workspace_id"] for a in w.changes._db.approvals(cid)}  # nobody to ask for it
    s, o, _ = _approve_all(w, cid)
    assert s == 200 and o["state"] == "applied"  # a hub admin proposed it: it was in the reach as proposed
    assert reach(w, orphan, w.newbie) == "admin" and reach(w, WS2, w.newbie) == "admin"


def test_an_orphans_people_must_be_exactly_as_proposed_on_every_path(w):
    orphan = "b6" * 16
    w.reg.add(orphan, "full", owner=None, name="Orphan")
    _wsa, cid, _tok = _admin_group_change(w, own=False)
    ch = w.changes._db.change(cid)
    stored = json.loads(ch["reach"])
    stored[orphan]["delta"] = [{"user_id": w.dana, "role": "read"}]  # what was shown differs from what would happen now
    ch = {**ch, "reach": json.dumps(stored)}
    body = json.loads(ch["body"])
    for approved in (None, {WS2: w.olive, WS3: w.omar}):  # override and approval both refuse
        with pytest.raises(ChangeError) as e:
            w.changes._check_reach(ch, body, w.admin, approved)
        assert e.value.code == "stale_change"


def test_deltas_compare_equal_whatever_their_order():
    from orch_hub.changes.service import _canon

    a = [{"user_id": "b", "role": "edit"}, {"user_id": "a", "role": "read"}]
    assert cj(_canon(a)) == cj(_canon(list(reversed(a))))


# --- GA-07: end dates through the same approval path (R3, R4) ----------------------------------------------------------

DAY = 86_400_000


def test_ga07_a_later_end_or_none_waits_for_the_owner_even_when_no_role_changes_today(w):
    acc(w).grant(WS2, "devs", "edit")  # dana is already in devs: she has edit in WS2 (owner olive)
    soon = w.clock() + 5 * DAY
    acc(w).add_member("devs", w.dana, ends_ms=soon)
    r = w.changes.member_add(w.admin, "devs", w.dana, ends_ms=soon + 10 * DAY)
    assert r is not None and [x["workspace_id"] for x in r["waiting_for"]] == [WS2]
    assert acc(w).member_end("devs", w.dana) == soon  # nothing applied yet
    r2 = w.changes.member_add(w.admin, "devs", w.dana, ends_ms=None)  # permanent is also an increase
    assert r2 is not None and acc(w).member_end("devs", w.dana) == soon
    body = json.loads(row(w, r["change_id"])["body"])
    assert body == {"group": "devs", "user_id": w.dana, "ends_ms": soon + 10 * DAY}  # the proof covers the end date
    otok, osig, och = owner_session(w, w.olive)
    s, got, _ = approve(w, otok, r["change_id"], osig, och)
    assert s == 200 and got["state"] == "applied"
    assert acc(w).member_end("devs", w.dana) == soon + 10 * DAY


def test_ga07_a_group_that_reaches_no_workspace_extends_at_once(w):
    acc(w).add_group("loose")
    soon = w.clock() + 5 * DAY
    acc(w).add_member("loose", w.dana, ends_ms=soon)
    assert w.changes.member_add(w.admin, "loose", w.dana, ends_ms=soon + DAY) is None
    assert acc(w).member_end("loose", w.dana) == soon + DAY


def test_ga07_a_shorter_end_applies_at_once_and_is_audited(w):
    acc(w).grant(WS2, "devs", "edit")
    far = w.clock() + 20 * DAY
    acc(w).add_member("devs", w.dana, ends_ms=far)
    head = w.env.audit.store.head()
    assert w.changes.member_add(w.admin, "devs", w.dana, ends_ms=far - 10 * DAY) is None
    assert acc(w).member_end("devs", w.dana) == far - 10 * DAY
    assert w.env.audit.store.head() != head


def test_ga07_shortening_runs_the_owner_guard_as_if_the_member_were_dropped_now(w):
    # olive holds admin on WS2 only through "owners": a (shorter) end for her would drop it
    with pytest.raises(ChangeError) as e:
        w.changes.member_add(w.admin, "owners", w.olive, ends_ms=w.clock() + 3 * DAY)
    assert e.value.code == "owner_protected"
    assert acc(w).member_end("owners", w.olive) is None


def test_ga07_a_pending_change_whose_end_has_passed_is_refused(w):
    acc(w).grant(WS2, "devs", "edit")
    r = w.changes.member_add(w.admin, "devs", w.newbie, ends_ms=w.clock() + 1000)
    assert r is not None
    otok, osig, och = owner_session(w, w.olive)
    w.clock.advance(5000)
    s, got, _ = approve(w, otok, r["change_id"], osig, och)
    assert (s, got) == (409, {"error": "expired"}) or got.get("error") in ("expired", "stale_change"), got
    assert w.newbie not in acc(w).members("devs")


def test_ga07_a_past_end_date_is_refused_up_front(w):
    with pytest.raises(AccessError) as e:
        w.changes.member_add(w.admin, "devs", w.dana, ends_ms=w.clock() - 1)
    assert e.value.code == "end_in_past"

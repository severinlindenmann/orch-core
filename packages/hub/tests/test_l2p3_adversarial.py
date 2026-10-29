"""L2 (phase-3 contract extension) adversarial tests: orch-hub PR #13, shared spaces §10.4, audit §10.5, admin read
§10.6. Every test here FAILS at 5f87949 by design; each names its finding (state/l2-review-p3.md, L2P-n). A fix may
change a call signature; keep the attack and the expected refusal."""

from __future__ import annotations

import json

import pytest

from orch_hub.contract import audit as AU
from orch_hub.contract import hub as H
from orch_hub.contract import spaces as SP
from orch_hub.contract import vectorgen as G

DEV = {"dev_mode": True}
WS_C = G.WS_C


def _held(card=None):
    return H.verify_card(card or G.card(), now_ms=G.NOW, dev_mode=True)


def _rotated_card():
    return G.card(op=G.op_cert(cert_id=G.OP_CERT_NEXT, key="hk_op_next"), op_key="hk_op_next", card_seq=2)


def _held_rotated():
    old = _held()
    return H.verify_card(_rotated_card(), now_ms=G.NOW, dev_mode=True, held=old)


# --- L2P-1: the author's signature does not commit to the epoch (or hub) it consented to share with -----------------


def test_l2p1_space_item_signature_commits_to_epoch_and_hub():
    """The hub holds every SK_e (option B). It opens A's epoch-1 item and re-seals the same WSK-signed object under
    epoch 2 (members A, C: C joined later, A never put anything for C). The AAD binds the epoch, but the hub writes the
    AAD, so only the author's signature can carry A's consent to an epoch's member set. Today it verifies."""
    signed = G.space_item_obj(G.WS_A)
    iid = signed["o"]["item_id"]
    resealed = SP.seal_item(G.SK[2], G.SPACE, 2, iid, signed, salt=bytes(16))
    with pytest.raises(H.Refused):
        SP.verify_item(
            resealed,
            sk=G.SK[2],
            space_id=G.SPACE,
            epoch=2,
            item_id=iid,
            members=[G.WS_A, WS_C],
            wsk_pubs={G.WS_A: G.K.pub["wsk_a"]},
            hub_id=G.HUB_ID,  # L2P-1 fix: verify_item now takes the reader's hub (call-signature change, attack kept)
            **DEV,
        )


def test_l2p1_space_item_names_hub_and_epoch():
    assert {"hub_id", "epoch"} <= set(H.FIELDS["space.item"]), "space.item must sign hub_id and epoch"


# --- L2P-2: a workspace can be made a member without its owner's consent and then leaks through later puts ----------


def test_l2p2_host_put_guard_refuses_when_member_set_grew():
    """The owner's share rule allowed space S when its members were {A, B}. A hub-admin (or a compromised hub) adds C.
    The host must stop putting into S until the owner re-approves the new member set. The contract has no host-side
    check for this at all (share rules name only the space_id)."""
    guard = getattr(SP, "check_host_put", None)
    if guard is None:
        pytest.fail("no host-side put guard: share rules pin only space_id, so an added member reads later puts")
    rule = {"space_id": G.SPACE, "members": sorted([G.WS_A, G.WS_B])}
    grown = G.space_rec(2, members=[G.WS_A, G.WS_B, WS_C], sk=G.SK[2])["o"]
    with pytest.raises(H.Refused):
        guard(rule, grown)


# --- L2P-3: HK_op rotation breaks the audit chain and every held space record ----------------------------------------


def test_l2p3_audit_record_before_op_rotation_still_verifies():
    """Record 1 was signed under op cert 1. After a routine HK_op rotation the host holds the new card and must still
    verify the chain from genesis (a new workspace) or past its head. Today verify_record only knows the newest op key."""
    rec1 = G.audit_chain(1)[0]
    held = _held_rotated()
    try:
        o = AU.verify_record(rec1, held, op_certs=[G.op_cert()], now_ms=G.NOW, **DEV)
    except TypeError:
        pytest.fail("verify_record cannot verify a record signed under an earlier (root-certified) HK_op")
    assert o["seq"] == 1


def test_l2p3_space_record_resigned_after_rotation_is_not_a_conflict():
    """After rotation the hub must re-sign the current hub.space under the new op cert (one op cert at a time, §5).
    Same space, epoch, members and sk_commit: the host must accept it. Today: space_conflict."""
    prior = SP.verify_space(G.space_rec(1), _held(), **DEV)
    resigned = G.resign("hk_op_next", "sig_hub_space", G.space_rec(1), op_cert_id=G.OP_CERT_NEXT)
    o = SP.verify_space(resigned, _held_rotated(), prior=prior, **DEV)
    assert o["epoch"] == 1 and o["sk_commit"] == prior["sk_commit"]


# --- L2P-4: audit_get shows every workspace the ids and activity of every other workspace ---------------------------


def test_l2p4_audit_record_does_not_name_other_workspaces():
    """Every workspace verifies every record. A record about workspace B must not carry B's id in clear."""
    ev = {"space_id": G.SPACE, "epoch": 2, "change": "remove", "workspace_id": G.WS_B, "actor_user_id": G.U_ALICE}
    rec = G.audit_rec(1, AU.GENESIS_PREV, "space", ev)
    assert G.WS_B not in json.dumps(rec["o"]), "hub.audit.scope reveals another workspace's id to every workspace"


# --- L2P-5: admin read scope bypass by target spelling ---------------------------------------------------------------


def _admin_state(mode="full"):
    acards = {
        G.WS_A: {"wsk_pub": G.K.b("wsk_a"), "owner_person_id": G.P_ALICE},
        WS_C: {"wsk_pub": G.K.b("wsk_b"), "owner_person_id": G.P_ALICE},
    }
    b = H.verify_binding(
        G.binding(mode=mode), workspace_id=G.WS_A, wsk_pub=G.K.pub["wsk_a"], now_ms=G.NOW, dev_mode=True
    )
    return {
        "cards": acards,
        "bindings": {G.WS_A: H.apply_binding(None, b)},
        "hub_cards": {G.HUB_ID: G.card()},
        "revoked_op_certs": [],
    }


@pytest.mark.parametrize(
    "target",
    [
        f"/v1/ws/{WS_C.upper()}/releases",
        f"//v1/ws/{WS_C}/releases",
        f"/v1/./ws/{WS_C}/releases",
        f"/v1/ws/%{ord(WS_C[0]):02x}{WS_C[1:]}/releases",
        f"/v1/admin/releases?workspace={WS_C}",
    ],
)
def test_l2p5_admin_read_scope_not_bypassed_by_target_spelling(target):
    """C is not bound to this hub. Any spelling of a target that a router may resolve to C (case, empty or dot
    segments, percent-encoding, a query parameter) must be refused, not passed to the data layer."""
    with pytest.raises(H.Refused) as e:
        H.check_admin_read(G.admin_req(target=target), _admin_state(), now_ms=G.NOW, **DEV)
    assert e.value.code in ("not_bound", "malformed")


# --- L2P-6: hub-admin reads metadata of selective workspaces (D71 / D80) --------------------------------------------


def test_l2p6_admin_read_scope_excludes_selective_workspaces():
    r = H.check_admin_read(G.admin_req(), _admin_state(mode="selective"), now_ms=G.NOW, **DEV)
    assert G.WS_A not in r["workspaces"], "D71: hub-admin has no role in a selective workspace"


# --- L2P-10: a re-delivered old record is reported as a fork -----------------------------------------------------------


def test_l2p10_redelivered_old_record_is_not_a_fork():
    """A host that keeps only its head cannot compare an older record; re-delivery (overlapping audit_get pages) is
    not evidence of a fork. A fork needs two HK_op-signed records with the same seq and different bytes."""
    ch = G.audit_chain(3)
    head = None
    for r in ch:
        head = AU.apply_record(head, r, AU.verify_record(r, _held(), now_ms=G.NOW, **DEV))
    assert AU.apply_record(head, ch[1], ch[1]["o"]) == head

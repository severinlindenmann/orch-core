"""C3, phase 4: the host's verification of custodial (``auth: "hub"``) events, hub contract section 8.

The 28 hub vectors (``custodial_*``) are replayed in ``test_vectors_hub``. Here are the negatives the vectors do not
carry, built by RE-MINTING events from the vector keys (all FAKE): a wrong chain, a revoked op cert, a stale snapshot,
the registration label used as a proof, a wrong role, a smuggled field, a missing or changed decision hash. And the
production path (:func:`verify_hub_event`, :func:`accept_event_deliver`) over a real :class:`HubState`."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from orch.canon.jcs import dumps as cj
from orch.crypto import suite as S
from orch.crypto.encoding import b64u
from orch.hubclient import HubRefused, HubState, wire
from orch.hubclient import custodial as C

from .vec import V, private_key, pub

CASE = next(c for c in V["cases"] if c["name"] == "custodial_valid")["args"]
WS, LOG, NOW = CASE["workspace_id"], CASE["log"], CASE["now_ms"]
HUB, OP_CERT = V["ids"]["hub"], V["ids"]["op_cert"]
ALICE, BOB = V["ids"]["user_alice"], V["ids"]["user_bob"]
PHONE = V["ids"]["channel_alice_phone"]
SNAP = CASE["snapshot"]
#: D66 floor that every factor meets, i.e. "no requirement": the argument is required, so a test says it out loud
ANY_FACTOR = "password+totp"
HELD = wire.verify_card(CASE["card"], now_ms=NOW, dev_mode=True)


def mint(
    changes: dict[str, Any] | None = None,
    *,
    drop: tuple[str, ...] = (),
    uk: str = "uk_alice",
    channel: str = "channel_alice_phone",
    proof_label: bytes = C.SIG_CHANNEL_PROOF,
    ws: str = WS,
    log: str = LOG,
    resign: bool = True,
) -> dict[str, Any]:
    """The vector's valid event with ``changes`` applied and BOTH signatures redone (so only the change is wrong)."""
    e = copy.deepcopy(CASE["event"])
    e.update(changes or {})
    for k in drop:
        e.pop(k, None)
    if resign:
        e.pop("sig", None)
        e.pop("channel_proof", None)
        digest = C.event_digest(ws, log, e)
        e["channel_proof"] = b64u(S.sign(private_key(channel), proof_label + digest))
        e["sig"] = b64u(S.sign(private_key(uk), C.custodial_label(e["type"]) + C._context(ws, log, e)))  # noqa: SLF001
    return e


def verify(event: Any, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "workspace_id": WS, "log": LOG, "held": HELD, "snapshot": SNAP, "now_ms": NOW, "dev_mode": True,
        "min_factor": ANY_FACTOR,
    }  # fmt: skip
    args.update(kw)
    return C.verify_custodial(event, **args)


def code(event: Any, **kw: Any) -> str:
    with pytest.raises(HubRefused) as e:
        verify(event, **kw)
    return e.value.code


def test_the_remint_helper_reproduces_the_vector_exactly():
    """If this holds, every negative below differs from the valid event only by the change it names."""
    strip = lambda e: {k: v for k, v in e.items() if k not in ("sig", "channel_proof")}  # noqa: E731 (the vectors' sigs are RFC 6979)
    assert strip(mint()) == strip(CASE["event"])
    assert verify(mint())["user_id"] == ALICE


# --- the chain UK -> HK_op -> HK_root --------------------------------------------------------------------------------


def test_uk_cert_signed_by_a_rogue_op_key_is_refused():
    cert = copy.deepcopy(CASE["event"]["uk_cert"])
    cert["sig"] = b64u(S.sign(private_key("hk_op_rogue"), wire.SIG_UK_CERT + cj(cert["o"])))
    assert code(mint({"uk_cert": cert})) == "bad_signature"


def test_channel_cert_signed_by_a_rogue_op_key_is_refused():
    cert = copy.deepcopy(CASE["event"]["channel_cert"])
    cert["sig"] = b64u(S.sign(private_key("hk_op_rogue"), wire.SIG_CHANNEL_CERT + cj(cert["o"])))
    assert code(mint({"channel_cert": cert})) == "bad_signature"


def test_a_uk_cert_under_the_uk_cert_label_of_the_channel_cert_is_refused():
    cert = copy.deepcopy(CASE["event"]["uk_cert"])
    cert["sig"] = b64u(S.sign(private_key("hk_op"), wire.SIG_CHANNEL_CERT + cj(cert["o"])))  # wrong domain
    assert code(mint({"uk_cert": cert})) == "bad_signature"


def test_a_card_under_another_root_is_not_a_chain_to_the_pinned_root():
    other = V["objects"]["card"]
    pinned = pub("hk_root_rogue")
    with pytest.raises(HubRefused) as e:
        wire.verify_card(other, now_ms=NOW, dev_mode=True, pinned_root=pinned)
    assert e.value.code == "other_hub"


def test_a_revoked_op_cert_refuses_the_event_even_if_the_card_was_admitted_before():
    assert code(mint(), revoked_op_certs=frozenset({OP_CERT})) == "revoked"


def test_an_event_naming_another_hub_is_refused_before_any_cert():
    assert code(mint({"hub_id": V["ids"]["hub_rogue"]})) == "other_hub"


# --- the channel proof ---------------------------------------------------------------------------------------------


def test_the_registration_label_is_never_a_custodial_proof():
    reg = V["labels"]["sig_hub_channel_register"].encode()
    assert reg != C.SIG_CHANNEL_PROOF and not reg.startswith(C.SIG_CHANNEL_PROOF)
    assert code(mint(proof_label=reg)) == "bad_channel_proof"


def test_a_proof_by_another_users_channel_key_is_refused():
    assert code(mint(channel="channel_bob_browser")) == "bad_channel_proof"


def test_a_proof_over_a_digest_of_another_log_is_refused():
    e = mint()
    other = C.event_digest(WS, "workspace", {k: v for k, v in e.items() if k not in ("sig", "channel_proof")})
    e["channel_proof"] = b64u(S.sign(private_key("channel_alice_phone"), C.SIG_CHANNEL_PROOF + other))
    e["sig"] = b64u(S.sign(private_key("uk_alice"), C.custodial_label(e["type"]) + C._context(WS, LOG, {  # noqa: SLF001
        k: v for k, v in e.items() if k != "sig"}))
    )  # fmt: skip
    assert code(e) == "bad_channel_proof"


def test_a_garbage_proof_is_malformed_not_a_crash():
    assert code(mint(resign=False) | {"channel_proof": "AAAA"}) == "malformed"


# --- the snapshot ---------------------------------------------------------------------------------------------------


def test_an_event_built_on_a_newer_snapshot_than_the_host_holds_asks_for_a_sync():
    assert code(mint({"snapshot_version": SNAP["version"] + 1})) == "unknown_snapshot"


def test_an_event_on_an_older_snapshot_still_obeys_the_newest_revocations():
    """Contract 8.3 step 7 names 'that snapshot'; the host holds only the newest pin, which only ever adds revoked
    channels and removes users. The newest applies: an older version cannot bring a revoked channel back."""
    newer = {**SNAP, "version": SNAP["version"] + 1, "revoked_channels": [PHONE]}
    assert code(mint({"snapshot_version": 1}), snapshot=newer) == "channel_revoked"
    gone = {**SNAP, "version": SNAP["version"] + 1, "roles": {BOB: "read"}}
    assert code(mint({"snapshot_version": 1}), snapshot=gone) == "forbidden_role"


def test_a_snapshot_of_another_workspace_is_not_used():
    assert code(mint(), snapshot={**SNAP, "workspace_id": V["ids"]["workspace_b"]}) == "other_workspace"


# --- roles and the closed table ----------------------------------------------------------------------------------


def _role(r: str) -> dict[str, Any]:
    return {**SNAP, "roles": {ALICE: r, BOB: "read"}}


@pytest.mark.parametrize("typ", sorted({t for (t, k) in C.CUSTODIAL_TYPES if k == "ticket"}))
def test_every_ticket_type_floor_is_enforced(typ):
    need = C.CUSTODIAL_TYPES[(typ, "ticket")]
    assert C.check_custodial_role(typ, LOG, need) is None
    below = {"edit": "read", "admin": "edit"}[need]
    with pytest.raises(HubRefused) as e:
        C.check_custodial_role(typ, LOG, below)
    assert e.value.code == "forbidden_role"


def test_the_workspace_log_types_are_admin_only_and_a_ticket_type_is_not_custodial_there():
    for typ in ("addon.disabled", "addon.purged"):
        assert C.check_custodial_role(typ, "workspace", "admin") is None
        with pytest.raises(HubRefused) as e:
            C.check_custodial_role(typ, "workspace", "edit")
        assert e.value.code == "forbidden_role"
        with pytest.raises(HubRefused) as e:  # an admin type on a ticket log: not in the table for that log
            C.check_custodial_role(typ, LOG, "admin")
        assert e.value.code == "not_custodial"
    with pytest.raises(HubRefused) as e:
        C.check_custodial_role("gate.approved", "workspace", "admin")
    assert e.value.code == "not_custodial"


@pytest.mark.parametrize(
    "typ",
    ["policy.changed", "settings.changed", "member.added", "role.changed", "device.added", "hub.joined", "hub.left",
     "grant.issued", "addon.granted", "restore", "invalid.acknowledged", "artifact.added", "claim.taken",
     "task.done", "workspace.created", "gate.invalidated", "branch.pushed"],
)  # fmt: skip
def test_the_types_the_contract_excludes_are_not_custodial_whatever_the_role(typ):
    for log in (LOG, "workspace"):
        with pytest.raises(HubRefused) as e:
            C.check_custodial_role(typ, log, "admin")
        assert e.value.code == "not_custodial"


#: hub protocol 8.8 (owner V3-D29): the phase-5 rows core C10 (#878) adds; until then the host refuses them
PHASE5_PENDING = {
    ("grant.requested", "workspace"),
    ("grant.revoked", "workspace"),
    ("device.excluded", "workspace"),
    ("policy.changed", "workspace"),
    ("policy.changed", "ticket"),
    ("settings.changed", "workspace"),
}


def test_the_table_is_exactly_the_contract_table():
    rows = {(r["type"], r["log"]): r["min_role"] for r in V["phase4"]["custodial_types"]}
    assert PHASE5_PENDING <= set(rows)
    want = {k: v for k, v in rows.items() if k not in PHASE5_PENDING or k in C.CUSTODIAL_TYPES}
    assert C.CUSTODIAL_TYPES == want


def test_a_read_role_signs_nothing_and_an_edit_role_cannot_start_an_admin_type():
    assert code(mint(), snapshot=_role("read")) == "forbidden_role"
    assert verify(mint(), snapshot=_role("edit"))["floor"] == "edit"


SEL = next(c for c in V["cases"] if c["name"] == "custodial_selective_granted_member_decides")["args"]


def sel(**over: Any) -> dict[str, Any]:
    """The vector's selective decision (a live item that shows exactly its values) with arguments replaced."""
    args = {
        "workspace_id": SEL["workspace_id"], "log": SEL["log"],
        "held": wire.verify_card(SEL["card"], now_ms=NOW, dev_mode=True),
        "snapshot": SEL["snapshot"], "now_ms": NOW, "dev_mode": True, "min_factor": ANY_FACTOR, "mode": "selective",
        "granted_users": frozenset(SEL["granted_users"]), "member_roles": SEL["member_roles"],
        "live_items": SEL["live_items"], "log_heads": SEL["log_heads"],
    }  # fmt: skip
    event = over.pop("event", SEL["event"])
    return C.verify_custodial(event, **{**args, **over})


def test_the_selective_floor_comes_from_the_member_role_not_the_hub_role():
    def refused(**over: Any) -> str:
        with pytest.raises(HubRefused) as e:
            sel(**over)
        return e.value.code

    # the snapshot says admin, the F1 role says viewer: nothing is signed (hub roles grant nothing, D80)
    assert refused(member_roles={ALICE: "viewer"}) == "forbidden_role"
    assert refused(member_roles={}) == "forbidden_role"  # granted but unaliased: a viewer
    assert refused(member_roles={ALICE: "root"}) == "forbidden_role"  # unknown role fails closed
    assert sel()["role"] == "admin" and sel(member_roles={ALICE: "owner"})["user_id"] == ALICE
    assert refused(granted_users=frozenset()) == "not_granted"


def test_selective_needs_an_item_hash_and_full_mode_refuses_one():
    e = dict(SEL["event"])
    del e["item_hash"]
    with pytest.raises(HubRefused) as x:
        sel(event=e)
    assert x.value.code == "malformed"
    with pytest.raises(HubRefused) as x:
        verify(mint({"item_hash": SEL["event"]["item_hash"]}))  # full mode: refused (15.7)
    assert x.value.code == "malformed"


# --- shape: smuggling, host fields, the reviewed hash ---------------------------------------------------------------


@pytest.mark.parametrize("field", ["seq", "at", "prev", "ws_seq", "host_sig"])
def test_host_fields_in_a_delivered_event_are_malformed(field):
    assert code(mint({field: 1})) == "malformed"


def test_a_smuggled_payload_field_is_refused_even_when_the_hub_signed_it():
    # gate.approved's schema is closed: any field it does not list is smuggled, signed by the hub or not
    for extra in ({"artifact": "x"}, {"verify": {"cmd": "rm -rf /"}}, {"text": "ok"}, {"owner": "p_" + "0" * 32}):
        assert code(mint(extra)) == "malformed"


def test_a_missing_payload_field_is_malformed():
    assert code(mint(drop=("policy_hash",))) == "malformed"
    assert code(mint(drop=("gate_gen",))) == "malformed"


def test_gate_approved_without_a_hash_is_refused_and_a_changed_hash_is_a_different_signature():
    assert code(mint(drop=("hash",))) == "malformed"
    assert code(mint({"hash": 5})) == "malformed"
    # a changed hash with the old signatures: the UK signature covers the hash
    changed = mint()
    changed["hash"] = "sha256:" + "0" * 64
    assert code(changed) == "bad_signature"


def test_check_built_hash_rule_is_checked_on_all_four_decision_types():
    """The payload shape runs after the signature steps, so it is exercised directly for the types whose events the
    vectors do not carry: every decision type needs `hash` present as a string."""
    from .test_custodial_shape import VALID, event

    for typ in C.DECISION_TYPES:
        C._payload_shape(typ, event(typ))  # noqa: SLF001
        for bad in ({"hash": ...}, {"hash": None}, {"hash": 1}):
            assert VALID[typ]["hash"]
            with pytest.raises(HubRefused) as e:
                C._payload_shape(typ, event(typ, **bad))  # noqa: SLF001
            assert e.value.code == "malformed"


def test_actor_shape_is_exact():
    good = CASE["event"]["actor"]
    for bad in (
        {**good, "device": "d_" + "0" * 32},
        {"kind": "person", "id": good["id"]},
        {**good, "via": "00" * 16},
        {**good, "kind": "agent"},
        {**good, "id": "p_" + ALICE},
        {**good, "id": good["id"].upper()},
    ):
        assert code(mint({"actor": bad})) == "malformed"


def test_event_type_grammar_keeps_the_bar_out_of_the_signing_domain():
    for typ in ("gate.approved|x", "Gate.approved", "gate", "a.b.c.d.e", "gate.approved\n", 5):
        assert code(mint({"type": typ}, resign=False)) == "malformed"


def test_not_a_dict_and_bad_logs_are_malformed():
    for ev in (None, [], "x", 1):
        assert code(ev) == "malformed"
    for log in ("", "tkt", "01j9zp0000000000000000tkt1", "9" * 26):
        assert code(mint(), log=log) == "malformed"


# --- the production path over a HubState -----------------------------------------------------------------------------


def _state(*, mode: str = "full", snapshot: Any = "vec", revoked: tuple[str, ...] = ()) -> HubState:
    return HubState(
        hub_id=HUB,
        hk_root_pub=V["keys"]["hk_root"]["pub"],
        hk_op_pub=b64u(HELD["hk_op_pub"]),
        mode=mode,
        binding_seq=1,
        status="bound",
        binding={"o": {"workspace_id": WS}},
        card=CASE["card"],
        revoked_op_certs=list(revoked),
        snapshot=V["objects"]["snapshot_a_v3"] if snapshot == "vec" else snapshot,
    )


def test_verify_hub_event_over_a_state_accepts_the_vector_event():
    r = C.verify_hub_event(_state(), CASE["event"], log=LOG, now_ms=NOW, dev_mode=True, min_factor=ANY_FACTOR)
    assert r["user_id"] == ALICE and r["channel"] == PHONE


def test_verify_hub_event_fails_closed_without_a_snapshot_a_binding_or_with_a_revoked_op():
    for st, expect in ((_state(snapshot=None), "unknown_snapshot"), (_state(revoked=(OP_CERT,)), "revoked")):
        with pytest.raises(HubRefused) as e:
            C.verify_hub_event(st, CASE["event"], log=LOG, now_ms=NOW, dev_mode=True, min_factor=ANY_FACTOR)
        assert e.value.code == expect
    unbound = _state()
    unbound.status = "unbound"
    with pytest.raises(HubRefused) as e:
        C.verify_hub_event(unbound, CASE["event"], log=LOG, now_ms=NOW, dev_mode=True, min_factor=ANY_FACTOR)
    assert e.value.code == "not_bound"


def test_a_real_workspace_refuses_a_dev_event_chain():
    with pytest.raises(HubRefused) as e:
        C.verify_hub_event(_state(), CASE["event"], log=LOG, now_ms=NOW, dev_mode=False, min_factor=ANY_FACTOR)
    assert e.value.code == "dev_refused"


def test_min_factor_is_required_everywhere_so_step_8_cannot_be_skipped_by_forgetting_it():
    """L2-C3-3: no default and no None. Omitting it is a TypeError; None and an unknown policy value refuse."""
    kw = {"workspace_id": WS, "log": LOG, "held": HELD, "snapshot": SNAP, "now_ms": NOW, "dev_mode": True}
    with pytest.raises(TypeError):
        C.verify_custodial(CASE["event"], **kw)
    with pytest.raises(TypeError):
        C.verify_hub_event(_state(), CASE["event"], log=LOG, now_ms=NOW, dev_mode=True)
    for bad in (None, "", "magic"):
        assert code(CASE["event"], min_factor=bad) == "factor_too_weak"
    assert code(CASE["event"], min_factor="passkey") == "factor_too_weak"  # the vector event is se-unlocked
    assert verify(CASE["event"], min_factor="se-unlocked")["factor"] == "se-unlocked"

"""V3 C8 (design-c8 M1, M2.2, M3): the break-glass table, the model's full-mode rule, what a leave voids and ends."""

from __future__ import annotations

import pytest

from orch.model import Code, admit, breakglass, replay, workspace
from orch.model.types import Grant, Member, WsCore
from tests.model.test_hub_consumers import (
    CHAN,
    CHAN2,
    HUB,
    U1,
    U2,
    alias,
    decide,
    decisions,
    hub_joined,
    pin,
    snapx,
)
from tests.model.test_hubledger import card
from tests.model.world import World, pol
from tests.schema.examples import cert, hex32, revocation


@pytest.fixture
def w() -> World:
    w = World(validate=False).bootstrap({"mara": "maintainer", "tom": "member"})
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer", "member"], 3)})
    hub_joined(w, [alias(w, "mara", U2)])
    pin(w, card())
    pin(w, snapx(1))
    return w


def strict(w: World):
    return replay(w.ws, w.tl, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id, hub_only=True)


# --- the table -----------------------------------------------------------------------------------------------------


def test_the_set_is_exactly_leave_recover_repin_rotate_and_the_v3_d29_reaffirmation():
    assert set(breakglass.BREAK_GLASS) == {"hub.leave", "device.recover", "hub.repin", "keys.rotate", "hub.mode"}
    assert breakglass.event_types() == {"hub.left", "device.added", "hub.repinned", "hub.joined"}
    assert breakglass.BREAK_GLASS["hub.mode"].shape is breakglass.reaffirms_full  # not any hub.joined


def test_only_the_exact_reaffirmation_shape_is_the_hub_joined_a_local_key_may_sign():
    ws = WsCore(created=True)
    ws.hub = {"status": "joined", "mode": "full", "hub_id": "h", "hk_root_pub": "r", "binding_seq": 3}
    ok = {"type": "hub.joined", "mode": "full", "hub_id": "h", "hk_root_pub": "r", "binding_seq": 3, "aliases": []}
    assert breakglass.reaffirms_full(ws, ok)
    for over in ({"binding_seq": 4}, {"hub_id": "x"}, {"hk_root_pub": "q"}, {"mode": "selective"},
                 {"aliases": [{"person_id": "a", "user_id": "b"}]}):  # fmt: skip
        assert not breakglass.reaffirms_full(ws, {**ok, **over}), over
    ws.hub["mode"] = "selective"
    assert not breakglass.reaffirms_full(ws, ok)


@pytest.mark.parametrize("signer", breakglass.SIGNER_KINDS)
def test_matrix_verb_by_signer(signer):
    dev = signer == "owner_device_key"
    assert breakglass.allowed("hub.leave", signer) is dev
    assert breakglass.allowed("keys.rotate", signer) is dev
    assert breakglass.allowed("device.recover", signer) is (signer == "pk")  # L2-C8i-1: the recovery key recovers
    assert breakglass.allowed("hub.repin", signer) is (signer == "pk")  # recovery key only: a device key is refused
    assert not breakglass.allowed("hub.join", signer) and not breakglass.allowed("hub.leave", "nobody")


# --- the model rule ------------------------------------------------------------------------------------------------


def test_a_local_person_event_in_full_mode_is_refused_and_leave_is_not(w):
    st = strict(w)
    e = w.build_unappended("workspace", "settings.changed", "sev", roster_v=w.roster_v, set={"claim_ttl_min": 30})
    assert admit(st, e, log="workspace").code == Code.SIGN_HUB_ONLY
    left = w.build_unappended("workspace", "hub.left", "sev", roster_v=w.roster_v, hub_id=HUB, binding_seq=1)
    assert not hasattr(admit(st, left, log="workspace"), "code")


def test_leave_by_the_click_signer_or_a_non_owner_is_refused(w):
    st = strict(w)
    for who, extra in (("sev", {"auth": "convenience"}), ("mara", {})):
        e = w.build_unappended("workspace", "hub.left", who, roster_v=w.roster_v, hub_id=HUB, binding_seq=1, **extra)
        assert admit(st, e, log="workspace").code == Code.SIGN_HUB_ONLY


def test_a_selective_or_left_workspace_signs_locally():
    w = World(validate=False).bootstrap({})
    hub_joined(w, [], mode="selective")
    e = w.build_unappended("workspace", "settings.changed", "sev", roster_v=w.roster_v, set={"claim_ttl_min": 30})
    assert not hasattr(admit(strict(w), e, log="workspace"), "code")


def test_an_excluded_owner_device_still_signs_the_leave(w):
    """Exclusion is a hub-custodial line; the model never feeds it into the break-glass check (C10 also refuses it)."""
    st = strict(w)
    e = w.build_unappended("workspace", "hub.left", "sev", roster_v=w.roster_v, hub_id=HUB, binding_seq=1)
    assert not hasattr(admit(st, e, log="workspace"), "code")


# --- a leave voids what the hub decided and ends what it granted ---------------------------------------------------


@pytest.fixture
def uid(w):
    u = w.ticket()
    w.fill(u)
    return u


def test_leave_voids_open_hub_decisions_only(w, uid):
    assert decide(w, uid, chan=CHAN) is None
    assert decide(w, uid, user=U1, chan=CHAN2) is None
    w.decide(uid, "tom", "requirements")
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    by = {d.signer: d for d in decisions(w, uid)}
    assert by[f"hub:{HUB}/{CHAN}"].voided and by[f"hub:{HUB}/{CHAN2}"].voided
    assert not by[w.dev["tom"]].voided


def test_leave_keeps_decisions_of_a_consumed_generation(w, uid):
    assert decide(w, uid) is None
    w.edit(uid, "sev", sections={"context": "changed"})  # the decision is no longer in the open generation
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    (d,) = decisions(w, uid)
    assert not d.voided


def test_hub_lines_are_refused_after_the_leave(w, uid):
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    r = decide(w, uid)
    assert r.code == Code.HUB_INVALID
    _, r = w.try_("workspace", "hub.pinned", {"kind": "host"}, hub_id=HUB, object=snapx(2))
    assert r.code == Code.HUB_INVALID


def _ws_with_grants() -> WsCore:
    ws = WsCore(created=True)
    ws.members = {"p_a": Member("p_a", "a", "owner", "k")}
    ws.hub = {
        "hub_id": HUB,
        "status": "joined",
        "binding_seq": 1,
        "mode": "full",
        "member": {"kind": "hub", "hub_id": HUB},
    }
    ws.grants = {
        "gr_hub": Grant("gr_hub", "p_a", "all", "agent", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", hub_id=HUB),
        "gr_own": Grant("gr_own", "p_a", "all", "agent", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
        "gr_other": Grant("gr_other", "p_a", "all", "agent", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z",
                          hub_id="f" * 32),
    }  # fmt: skip
    return ws


def test_leave_ends_grants_the_hub_requested_and_no_others():
    ws = _ws_with_grants()
    e = {"type": "hub.left", "actor": {"kind": "person", "id": "p_a"}, "hub_id": HUB, "binding_seq": 1}
    assert workspace.hub_left(ws, e) is None
    assert ws.grants["gr_hub"].revoked
    assert not ws.grants["gr_own"].revoked and not ws.grants["gr_other"].revoked


# --- the signer is classified fail closed (security review of the first push) ----------------------------------------


def _leave(w, who="sev", **extra):
    return w.build_unappended("workspace", "hub.left", who, roster_v=w.roster_v, hub_id=HUB, binding_seq=1, **extra)


@pytest.mark.parametrize("auth", ["secure-enclave", "secure-enclave-unlocked", "webauthn", "tpm", "windows-hello"])
def test_only_the_passphrase_label_is_the_owners_terminal(w, auth):
    r = admit(strict(w), _leave(w, auth=auth), log="workspace")
    assert r.code == Code.SIGN_HUB_ONLY


def test_a_passphrase_leave_by_the_owners_own_device_is_allowed(w):
    assert not hasattr(admit(strict(w), _leave(w, auth="passphrase"), log="workspace"), "code")


def test_a_relay_signed_leave_is_refused(w):
    r = admit(strict(w), _leave(w, signed_by=["p_x", "d_x"]), log="workspace")
    assert r.code == Code.SIGN_HUB_ONLY


def test_a_device_that_is_not_the_actors_is_not_the_owners_key(w):
    e = _leave(w)
    e["actor"] = {**e["actor"], "device": w.dev["tom"]}
    assert breakglass.signer_kind(strict(w)._core.ws, e) == "other"
    assert not breakglass.allowed("hub.leave", "other")


def _lose_device(w):
    w.wev(
        "device.revoked", w.HOST, device=w.dev["sev"], reason="lost",
        revocation=revocation(w.people["sev"][2:], w.dev["sev"][2:], "lost"),
    )  # fmt: skip
    newdev = hex32("recovered")
    actor = {"kind": "person", "id": w.people["sev"], "device": "d_" + newdev}
    return newdev, actor


def _recovery(w, newdev, actor, **extra):
    return w.build_unappended(
        "workspace", "device.added", actor, roster_v=w.roster_v, device="d_" + newdev,
        cert=cert(w.people["sev"][2:], newdev), **extra,
    )  # fmt: skip


def _admit_append(w, e):
    r = admit(strict(w), e, log="workspace")
    assert not hasattr(r, "code"), r
    e["host_sig"] = "h" * 8
    w._append("workspace", e)


def test_the_recovery_shape_is_its_own_kind(w):
    newdev, actor = _lose_device(w)
    ws = strict(w)._core.ws
    e = _recovery(w, newdev, actor)
    assert breakglass.is_recovery(ws, e) and breakglass.signer_kind(ws, e) == "pk"
    assert breakglass.allowed("device.recover", "pk") and not breakglass.allowed("hub.repin", "owner_device_key")


def test_lost_device_pk_recovery_is_admitted_then_the_new_device_leaves(w):
    """L2-C8i-1, end to end: the owner's only device is lost, a hub-joined full-mode workspace still lets the owner
    recover with the recovery key (append and replay) and leave with the recovered device."""
    newdev, actor = _lose_device(w)
    assert admit(strict(w), _leave(w), log="workspace").code == Code.DEVICE_INVALID  # the lost device itself: no
    _admit_append(w, _recovery(w, newdev, actor))
    left = w.build_unappended("workspace", "hub.left", actor, roster_v=w.roster_v, hub_id=HUB, binding_seq=1)
    assert breakglass.signer_kind(strict(w)._core.ws, left) == "owner_device_key"
    _admit_append(w, left)
    assert strict(w)._core.ws.hub.get("status") != "joined"


def test_recovery_by_a_non_owner_or_with_a_valid_device_or_another_signer_is_refused(w):
    newdev, actor = _lose_device(w)
    # a valid device exists (tom's own): tom's "recovery" is not the recovery shape, the device is unknown
    t_actor = {"kind": "person", "id": w.people["tom"], "device": "d_" + newdev}
    e = w.build_unappended(
        "workspace", "device.added", t_actor, roster_v=w.roster_v, device="d_" + newdev,
        cert=cert(w.people["tom"][2:], newdev),
    )  # fmt: skip
    assert admit(strict(w), e, log="workspace").code == Code.DEVICE_UNKNOWN
    # the owner has no valid device, but a non-owner member with none either: refused as break-glass is owner only
    w.wev(
        "device.revoked", w.HOST, device=w.dev["tom"], reason="lost",
        revocation=revocation(w.people["tom"][2:], w.dev["tom"][2:], "lost"),
    )  # fmt: skip
    e = w.build_unappended(
        "workspace", "device.added", t_actor, roster_v=w.roster_v, device="d_" + newdev,
        cert=cert(w.people["tom"][2:], newdev),
    )  # fmt: skip
    assert admit(strict(w), e, log="workspace").code == Code.SIGN_HUB_ONLY
    # a labelled (not terminal) recovery, a relay-signed one, and a convenience-labelled one
    for extra in ({"auth": "convenience"}, {"auth": "secure-enclave-unlocked"}, {"signed_by": ["p_x", "d_x"]}):
        r = admit(strict(w), _recovery(w, newdev, actor, **extra), log="workspace")
        assert r.code == Code.SIGN_HUB_ONLY, extra


def test_an_ordinary_device_added_in_full_mode_is_still_refused(w):
    """A plain ``device.added`` by the owner's valid device is not the recovery shape: it needs the hub."""
    newdev = hex32("plain")
    e = w.build_unappended(
        "workspace", "device.added", "sev", roster_v=w.roster_v, device="d_" + newdev,
        cert=cert(w.people["sev"][2:], newdev),
    )  # fmt: skip
    assert admit(strict(w), e, log="workspace").code == Code.SIGN_HUB_ONLY


def test_agent_and_hub_cannot_recover_a_device(w):
    newdev, actor = _lose_device(w)
    e = _recovery(w, newdev, actor)
    assert breakglass.signer_kind(strict(w)._core.ws, {**e, "actor": {**actor, "via": "ch"}}) == "hub"
    assert breakglass.signer_kind(strict(w)._core.ws, {**e, "actor": {**actor, "kind": "agent"}}) == "agent"


def test_an_agent_cannot_write_a_human_only_event_in_full_mode():
    w = World(validate=False).bootstrap({"mara": "maintainer"})
    gid = w.grant("sev")
    u = w.ticket()  # made before the join: a full-mode workspace takes no local ticket
    hub_joined(w, [])
    assert {"gate.approved", "ticket.closed", "grant.issued", "verdict.given"} <= breakglass.human_only_types()
    assert "task.done" not in breakglass.human_only_types() and "log.added" not in breakglass.human_only_types()
    ag = w.agent("sev", gid)
    e = w.build_unappended(u, "ticket.closed", ag)
    # the actor-kind rule already refuses it; the full-mode check is the second line of defence at the same choke point
    assert admit(strict(w), e, log=u).code in (Code.EVENT_BAD_ACTOR, Code.SIGN_HUB_ONLY)
    assert breakglass.agent_refusal(strict(w)._core.ws, e).code == Code.SIGN_HUB_ONLY
    e = w.build_unappended(u, "log.added", ag, text="hello")
    assert getattr(admit(strict(w), e, log=u), "code", None) != Code.SIGN_HUB_ONLY  # agents keep their other writes

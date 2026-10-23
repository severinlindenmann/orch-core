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


def test_the_set_is_exactly_leave_repin_rotate():
    assert set(breakglass.BREAK_GLASS) == {"hub.leave", "hub.repin", "keys.rotate"}
    assert breakglass.event_types() == {"hub.left", "hub.repinned"}


@pytest.mark.parametrize("signer", breakglass.SIGNER_KINDS)
def test_matrix_verb_by_signer(signer):
    both = signer in ("owner_device_key", "pk")
    assert breakglass.allowed("hub.leave", signer) is both
    assert breakglass.allowed("keys.rotate", signer) is both
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


def test_the_recovery_shape_is_its_own_kind(w):
    ws = strict(w)._core.ws
    e = _leave(w)
    e["actor"] = {**e["actor"], "device": "d_" + "9" * 32}
    e["cert"] = {"o": {}, "sig": "x"}
    assert breakglass.signer_kind(ws, e) == "pk"
    assert breakglass.allowed("hub.leave", "pk") and not breakglass.allowed("hub.repin", "owner_device_key")


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

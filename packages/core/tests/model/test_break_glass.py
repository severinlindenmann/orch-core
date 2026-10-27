"""A1 (D83): a local person event is valid in every hub mode; what a leave voids and ends; the recovery shapes; the
agent gate."""

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
    return replay(w.ws, w.tl, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id)


# --- the model: local signing is valid in a full-mode position ----------------------------------------------------


@pytest.mark.parametrize("auth", ["passphrase", "convenience", "secure-enclave-unlocked", "webauthn"])
def test_a_local_person_event_in_full_mode_is_admitted_whatever_the_way_of_signing(w, auth):
    st = strict(w)
    e = w.build_unappended(
        "workspace", "settings.changed", "sev", roster_v=w.roster_v, set={"claim_ttl_min": 30}, auth=auth
    )
    assert not hasattr(admit(st, e, log="workspace"), "code")


def test_a_non_owner_member_follows_the_roster_not_the_hub(w):
    """Local authority is the roster: the maintainer cannot change settings locally, in any hub mode."""
    e = w.build_unappended("workspace", "settings.changed", "mara", roster_v=w.roster_v, set={"claim_ttl_min": 30})
    assert hasattr(admit(strict(w), e, log="workspace"), "code")


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


# --- the recovery shapes and the agent gate -----------------------------------------------------------------------


def _leave(w, who="sev", **extra):
    return w.build_unappended("workspace", "hub.left", who, roster_v=w.roster_v, hub_id=HUB, binding_seq=1, **extra)


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


def test_the_recovery_shape_is_recognised(w):
    newdev, actor = _lose_device(w)
    ws = strict(w)._core.ws
    assert breakglass.is_recovery(ws, _recovery(w, newdev, actor))


def test_lost_device_recovery_is_admitted_then_the_new_device_leaves(w):
    newdev, actor = _lose_device(w)
    assert admit(strict(w), _leave(w), log="workspace").code == Code.DEVICE_INVALID  # the lost device itself: no
    _admit_append(w, _recovery(w, newdev, actor))
    left = w.build_unappended("workspace", "hub.left", actor, roster_v=w.roster_v, hub_id=HUB, binding_seq=1)
    _admit_append(w, left)
    assert strict(w)._core.ws.hub.get("status") != "joined"


def test_an_ordinary_device_added_in_full_mode_is_admitted_for_the_owner(w):
    newdev = hex32("plain")
    e = w.build_unappended(
        "workspace", "device.added", "sev", roster_v=w.roster_v, device="d_" + newdev,
        cert=cert(w.people["sev"][2:], newdev),
    )  # fmt: skip
    assert not hasattr(admit(strict(w), e, log="workspace"), "code")


def test_an_agent_cannot_write_a_human_only_event_in_any_mode():
    w = World(validate=False).bootstrap({"mara": "maintainer"})
    gid = w.grant("sev")
    u = w.ticket()
    hub_joined(w, [])
    ag = w.agent("sev", gid)
    e = w.build_unappended(u, "ticket.closed", ag)
    assert admit(strict(w), e, log=u).code == Code.EVENT_BAD_ACTOR
    e = w.build_unappended(u, "log.added", ag, text="hello")
    assert not hasattr(admit(strict(w), e, log=u), "code")

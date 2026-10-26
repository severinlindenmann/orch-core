"""Opus review of C3b PR B (core#1038, l2-c3bB): adversarial tests for the findings. Each one asserts the behaviour
the spec requires; the ones the PR does not meet yet are strict xfails naming the finding."""

from __future__ import annotations

from orch.model import Code
from tests.model.test_hub_consumers import (  # noqa: F401 - fixtures
    U3,
    alias,
    hub,
    hub_joined,
    pin,
    snapx,
    w,  # noqa: F401
)
from tests.model.test_hub_replay import hub_actor, hub_fields
from tests.model.test_hubledger import HUB, HUB_B, ROOT_B, U1, card


def test_a_u_ref_a_member_signs_on_a_device_belongs_to_the_hub_joined_then(w):  # noqa: F811
    """15.6 (2): a u_ reference in a person event's payload is recorded under the hub joined at that position. The
    owner (device) makes hub A's unaliased user U1 the ticket's reviewer. After a leave and a join of hub B, B presents
    U1 as its own user: it must be foreign_user, else B's user inherits the A-era reviewer slot (L2P4-C1b)."""
    u = w.ticket()
    w.tev(u, "people.changed", "sev", role="reviewers", add=["u_" + U1], remove=[])
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    hub_joined(w, [], hub=HUB_B, root=ROOT_B, seq=2)
    pin(w, card(1, HUB_B), HUB_B)
    pin(w, snapx(1, {U1: "edit", U3: "admin"}, hub=HUB_B), HUB_B)
    _, r = w.try_(u, "log.added", hub_actor(U1), roster_v=w.roster_v, text="x",
                  **hub_fields(hub_id=HUB_B, snapshot_version=1))  # fmt: skip
    assert getattr(r, "code", None) == Code.HUB_FOREIGN_USER


def test_hub_b_may_not_alias_an_id_only_a_device_event_referenced_under_hub_a(w):  # noqa: F811
    u = w.ticket()
    w.tev(u, "people.changed", "sev", role="reviewers", add=["u_" + U1], remove=[])
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    _, r = w.try_("workspace", "hub.joined", "sev", hub_id=HUB_B, hk_root_pub=ROOT_B, mode="full", binding_seq=2,
                  aliases=[alias(w, "tom", U1)])  # fmt: skip
    assert getattr(r, "code", None) == Code.HUB_INVALID and r.detail == "foreign_user"


def test_a_device_events_u_ref_is_recorded_under_the_joined_hub(w):  # noqa: F811
    u = w.ticket()
    w.tev(u, "people.changed", "sev", role="reviewers", add=["u_" + U1], remove=[])
    assert w.state()._core.ws.hub.get("user_hub", {}).get(U1) == HUB

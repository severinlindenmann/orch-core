"""V3 C8: a rejoin of the same hub, the grants a leave ends (and what the runner then holds), and the owner's review."""

from __future__ import annotations

from orch.hubclient import leavereview
from orch.model import authz
from orch.model.codes import Code
from orch.model.types import Core, Grant, Member, WsCore
from tests.model.test_hub_replay import hub_actor, hub_fields
from tests.model.test_hubledger import HUB, U2, card, join, pin, snap
from tests.model.world import World


def _world() -> World:
    w = World(validate=False).bootstrap({"mara": "maintainer"})
    join(w, aliases=[{"person_id": w.people["mara"][2:], "user_id": U2}])
    pin(w, card())
    pin(w, snap(1))
    return w


def _line(w: World, at: str):
    return w.try_(
        "workspace", "addon.disabled", hub_actor(), roster_v=w.roster_v, name="estimate", at=at, **hub_fields()
    )


def test_a_same_hub_rejoin_refuses_a_custodial_line_made_before_the_leave():
    w = _world()
    w.tick()
    held = w.at()  # a line the hub signed now and keeps
    assert _line(w, held)[1].code == Code.ADDON_UNKNOWN  # valid in this binding (it passes authorization)
    w.tick()
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    w.tick()
    join(w, seq=2, aliases=[{"person_id": w.people["mara"][2:], "user_id": U2}])
    r = _line(w, held)[1]  # the hub delivers the held line after the rejoin
    assert (r.code, r.detail) == (Code.HUB_INVALID, "before_binding")
    w.tick()
    pin(w, snap(2))  # the new membership gets its own snapshot (none is inherited from the left one)
    assert _line(w, w.at())[1].code == Code.ADDON_UNKNOWN  # a line of the new binding passes


def test_a_leave_ends_the_grant_the_hub_asked_for_and_an_agent_write_with_it_is_refused():
    ws = WsCore(created=True)
    ws.members = {"p_a": Member("p_a", "a", "owner", "k")}
    ws.grants = {
        "gr_h": Grant("gr_h", "p_a", "all", "agent", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", hub_id=HUB)
    }
    ws.hub = {"hub_id": HUB, "status": "joined", "binding_seq": 1, "mode": "full"}
    ev = {
        "type": "task.done", "at": "2026-01-01T01:00:00Z",
        "actor": {"kind": "agent", "id": "x", "session": "s", "for": "p_a", "grant": "gr_h"},
    }  # fmt: skip
    assert authz.authorize_agent(Core(ws=ws), None, ev) is None  # before the leave the grant works
    from orch.model import workspace

    left = {"type": "hub.left", "actor": {"kind": "person", "id": "p_a"}, "hub_id": HUB, "binding_seq": 1}
    assert workspace.hub_left(ws, left) is None
    r = authz.authorize_agent(Core(ws=ws), None, ev)
    assert r is not None and r.code == Code.GRANT_INVALID


# --- the review ----------------------------------------------------------------------------------------------------

CH = {"auth": "hub", "hub_id": HUB, "channel": "c" * 32, "channel_factor": "se-unlocked"}


def _e(i, typ, at, **kw):
    return {
        "id": f"e{i}",
        "type": typ,
        "at": at,
        "actor": {"kind": "person", "id": "u_" + "1" * 32, "via": "c" * 32},
        **CH,
        **kw,
    }


def test_the_review_lists_hub_made_policy_settings_devices_and_grants_first_with_their_undo():
    pol = {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False}
    events = [
        ("workspace", _e(1, "log.added", "2026-01-01T00:00:00Z")),  # old, outside the 7 days: not listed in "other"
        ("workspace", _e(2, "policy.changed", "2026-01-02T00:00:00Z", gates={"code": {**pol, "applies": "all"}})),
        ("workspace", _e(3, "settings.changed", "2026-01-02T00:00:01Z", set={"grant_hours": 100})),
        ("workspace", _e(4, "device.excluded", "2026-01-02T00:00:02Z", device="d_" + "2" * 32, reason="lost")),
        ("workspace", _e(5, "grant.requested", "2026-01-02T00:00:03Z", scope="all", hours=168)),
        ("T-1", _e(6, "question.answered", "2026-01-20T00:00:00Z")),
        ("workspace", {"id": "e7", "type": "policy.changed", "at": "2026-01-02T00:00:00Z", "actor": {}}),
        ("workspace", _e(8, "settings.changed", "2026-01-03T00:00:00Z", set={"claim_ttl_min": 1})),
    ]  # fmt: skip
    r = leavereview.build(events, hub_id=HUB, invalid=frozenset({"e8"}), stuck=[{"ticket": "T-2", "gate": "code"}],
                          now="2026-01-21T00:00:00Z")  # fmt: skip
    kinds = [x["type"] for x in r["first"]]
    assert kinds == ["policy.changed", "settings.changed", "device.excluded", "grant.requested"]  # any age, in order
    assert r["first"][0]["revert"]["gates"]["code"]["applies"] == "off"  # the previous effective value (the default)
    assert r["first"][1]["revert"] == {"op": "settings.set", "set": {"grant_hours": 8}}
    assert r["first"][2]["revert"] is None and "no inverse" in r["first"][2]["note"]
    assert r["stuck"] == [{"ticket": "T-2", "gate": "code"}]
    assert [x["type"] for x in r["other"]] == ["question.answered"]  # then the rest, since the last 7 days
    assert all(x["id"] != "e7" and x["id"] != "e8" for x in r["first"] + r["other"])  # not the hub's / not admitted


def test_the_snapshot_floor_survives_leave_and_rejoin():
    """An old snapshot is never pinned again after a rejoin; the active projection starts empty."""
    w = _world()
    pin(w, snap(5))
    w.tick()
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    w.tick()
    join(w, seq=2, aliases=[{"person_id": w.people["mara"][2:], "user_id": U2}])
    assert w.state()._core.ws.hub["ledger"][HUB]["snapshot"] is None
    r = pin(w, snap(3))[1]
    assert (r.code, r.detail) == (Code.HUB_INVALID, "stale_snapshot")
    assert not hasattr(pin(w, snap(6))[1], "code")

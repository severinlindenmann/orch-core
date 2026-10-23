"""V3 C10 (design-c10 M6, hub protocol 8.8): the model side of the five hub-signed custodial types.

``grant.requested`` + the host-actor ``grant.issued`` that answers it (needs a request, used once, same terms, 300 s,
``issued_at = min(request.at, now)``, ``Grant.hub_id``), ``grant.revoked`` by the canonical person (also via an alias),
``device.excluded`` (never an owner's device), ``policy.changed`` keeping the stored factor floor, ``settings.changed``.
Real admitted hub lines (``World.try_``) with the fake verifier for the crypto."""

from __future__ import annotations

import pytest

from orch.model import Code, replay
from orch.model.codes import Refusal
from tests.model.test_hub_consumers import CHAN, HUB, U1, U2, alias, hub_joined, pin, snapx
from tests.model.test_hub_replay import hub_actor, hub_fields
from tests.model.test_hubledger import card
from tests.model.world import World, pol, sig

U3 = "6" * 32
HOST = {"kind": "host"}


@pytest.fixture
def w() -> World:
    """sev owner, mara maintainer (hub user U2, role edit), tom member (U3); U1 an unaliased hub admin."""
    w = World(validate=False).bootstrap({"mara": "maintainer", "tom": "member"})
    hub_joined(w, [alias(w, "mara", U2), alias(w, "tom", U3)])
    pin(w, card())
    pin(w, snapx(1, {U1: "admin", U2: "edit", U3: "edit"}))
    return w


def hub(w: World, typ: str, user: str = U2, log: str = "workspace", **payload):
    _, r = w.try_(log, typ, hub_actor(user, CHAN), roster_v=w.roster_v, **hub_fields(), **payload)
    return r if isinstance(r, Refusal) else None


def request(w: World, user: str = U2, **over):
    terms = {"scope": "workable", "verbs": "agent", "hours": 8, **over}
    r = hub(w, "grant.requested", user, **terms)
    assert r is None, r
    ws = w.state()._core.ws
    return next(i for i, q in ws.requests.items() if not q["used"] and q["at"] == w.ws[-1]["at"])


def host_issue(w: World, req: str, **over):
    r = w.ws[[e["id"] for e in w.ws].index(req)]
    terms = {"scope": "workable", "verbs": "agent", "hours": 8, "request": req, "label": r.get("label")}
    terms.update(over)
    terms = {k: v for k, v in terms.items() if v is not None}
    issued = terms.pop("issued_at", None)
    e = w.build_unappended("workspace", "grant.issued", HOST, grant="gr_" + "01J9ZP0000000000000000GRT1"[:26],
                           issued_at="x", expires_at="x", secret_hash="sha256:" + "d" * 64, **terms)  # fmt: skip
    at = e["at"]
    from datetime import UTC, datetime

    from orch.model.types import ts

    def iso(s):
        return datetime.fromtimestamp(s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    iss = issued or iso(min(ts(r["at"]), ts(at)))
    e["issued_at"], e["expires_at"] = iss, iso(ts(iss) + 3600 * terms["hours"])
    return e


def admit_host(w: World, e: dict):
    from orch.model import admit

    res = admit(w.state(), e, log="workspace")
    if not isinstance(res, Refusal):
        e["host_sig"] = sig("host" + str(w.n))
        w._append("workspace", e)
    return res


# --- grant.requested and the host-actor grant.issued ---------------------------------------------------------------


def test_a_request_then_a_host_grant_makes_a_grant_of_the_canonical_person_that_belongs_to_the_hub(w):
    req = request(w)
    assert not isinstance(admit_host(w, host_issue(w, req)), Refusal)
    ws = w.state()._core.ws
    g = ws.grants["gr_01J9ZP0000000000000000GRT1"]
    assert (g.person, g.hub_id, g.device) == (w.people["mara"], HUB, "")  # alias resolved; no device
    assert ws.requests[req]["used"] is True


def test_a_request_from_a_member_with_no_alias_is_refused(w):
    assert hub(w, "grant.requested", U1, scope="workable", verbs="agent", hours=8).code == Code.MEMBER_UNKNOWN


def test_a_member_request_is_workable_and_within_grant_hours(w):
    r = hub(w, "grant.requested", U3, scope="all", verbs="agent", hours=8)  # tom is a member
    assert r.code == Code.GRANT_TERMS
    cap = w.state().workspace.settings["grant_hours"]
    r = hub(w, "grant.requested", U3, scope="workable", verbs="agent", hours=cap + 1)
    assert r.code == Code.GRANT_TERMS


def test_a_viewer_through_the_hub_holds_no_grant(w):
    pin(w, snapx(2, {U1: "admin", U2: "read", U3: "edit"}))
    assert hub(w, "grant.requested", U2, scope="workable", verbs="agent", hours=1).code == Code.ROLE_DENIED


def test_host_grant_needs_a_request(w):
    e = host_issue(w, request(w), request="01J9ZP00000000000000000NOP")
    assert admit_host(w, e).code == Code.GRANT_TERMS


def test_host_grant_without_the_request_field_is_refused_by_the_schema():
    from orch.schema import SchemaError, validate

    w = World().bootstrap({"mara": "maintainer"})
    e = w.build_unappended("workspace", "grant.issued", HOST, grant="gr_01J9ZP0000000000000000GRT1", scope="workable",
                           verbs="agent", issued_at="2026-10-09T09:10:11Z", hours=8, expires_at="2026-10-09T17:10:11Z",
                           secret_hash="sha256:" + "d" * 64)  # fmt: skip
    e["host_sig"] = sig("h")
    with pytest.raises(SchemaError):
        validate("event", e, log="workspace")


def test_a_request_is_answered_once(w):
    req = request(w)
    assert not isinstance(admit_host(w, host_issue(w, req)), Refusal)
    again = host_issue(w, req)
    again["grant"] = "gr_01J9ZP0000000000000000GRT2"
    assert admit_host(w, again).code == Code.GRANT_TERMS


@pytest.mark.parametrize("over", [{"hours": 7}, {"scope": "all"}, {"verbs": ["log"]}, {"label": "other"}])
def test_host_grant_must_be_what_was_requested(w, over):
    req = request(w, hours=8)
    assert admit_host(w, host_issue(w, req, **over)).code == Code.GRANT_TERMS


def test_host_grant_for_a_late_request_is_refused(w):
    req = request(w)
    w.tick(301)
    assert admit_host(w, host_issue(w, req)).code == Code.GRANT_TERMS


def test_host_grant_issued_at_is_min_of_request_and_now(w):
    req = request(w)
    w.tick(100)
    e = host_issue(w, req)
    assert e["issued_at"] != e["at"]  # the request's time, not now
    e["issued_at"] = e["at"]
    e["expires_at"] = host_issue(w, req, issued_at=e["at"])["expires_at"]
    assert admit_host(w, e).code == Code.GRANT_TERMS
    assert not isinstance(admit_host(w, host_issue(w, req)), Refusal)


def test_a_person_may_not_name_a_request_and_a_hub_may_not_sign_grant_issued(w):
    req = request(w)
    gid = "gr_01J9ZP0000000000000000GRT1"
    e = w.build_unappended("workspace", "grant.issued", "mara", roster_v=w.roster_v, grant=gid,
                           scope="workable", verbs="agent", issued_at=w.at(), hours=8, expires_at=w.at(),
                           secret_hash="sha256:" + "d" * 64, request=req)  # fmt: skip
    from orch.model import admit

    assert admit(w.state(), e, log="workspace").code in (Code.GRANT_TERMS, Code.SIG_INVALID, Code.EVENT_BAD_ACTOR)
    h = w.build_unappended("workspace", "grant.issued", hub_actor(U2, CHAN), roster_v=w.roster_v, **hub_fields(),
                           grant="gr_01J9ZP0000000000000000GRT1", scope="workable", verbs="agent", issued_at=w.at(),
                           hours=8, expires_at=w.at(), secret_hash="sha256:" + "d" * 64)  # fmt: skip
    assert admit(w.state(), h, log="workspace").code == Code.HUB_DENIED


def test_a_device_cannot_sign_a_request(w):
    e = w.build_unappended("workspace", "grant.requested", "sev", roster_v=w.roster_v, scope="workable", verbs="agent",
                           hours=8)  # fmt: skip
    from orch.model import admit

    assert admit(w.state(), e, log="workspace").code == Code.EVENT_BAD_ACTOR


def test_the_leave_ends_a_hub_requested_grant_and_not_a_local_one(w):
    req = request(w)
    admit_host(w, host_issue(w, req))
    w.wev("grant.issued", "sev", grant="gr_01J9ZP0000000000000000GRT9", scope="workable", verbs="agent",
          issued_at=w.at(), hours=8, expires_at=host_issue(w, req, issued_at=w.at())["expires_at"],
          secret_hash="sha256:" + "e" * 64)  # fmt: skip
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    gs = w.state()._core.ws.grants
    assert gs["gr_01J9ZP0000000000000000GRT1"].revoked and not gs["gr_01J9ZP0000000000000000GRT9"].revoked


# --- grant.revoked --------------------------------------------------------------------------------------------------


def test_an_edit_user_revokes_own_grant_also_via_the_alias_and_not_another_persons(w):
    req = request(w, U2)
    admit_host(w, host_issue(w, req))
    other = w.wev("grant.issued", "sev", grant="gr_01J9ZP0000000000000000GRT9", scope="workable", verbs="agent",
                  issued_at=w.at(), hours=8, expires_at=host_issue(w, req, issued_at=w.at())["expires_at"],
                  secret_hash="sha256:" + "e" * 64)  # fmt: skip
    assert other["actor"]["id"] == w.people["sev"]
    assert hub(w, "grant.revoked", U2, grant="gr_01J9ZP0000000000000000GRT9").code == Code.ROLE_DENIED  # sev's
    assert hub(w, "grant.revoked", U2, grant="gr_01J9ZP0000000000000000GRT1") is None  # mara's own, via U2
    assert w.state()._core.ws.grants["gr_01J9ZP0000000000000000GRT1"].revoked


def test_an_admin_revokes_any_grant(w):
    req = request(w, U2)
    admit_host(w, host_issue(w, req))
    assert hub(w, "grant.revoked", U1, grant="gr_01J9ZP0000000000000000GRT1") is None  # U1 is an admin (owner)


# --- device.excluded -------------------------------------------------------------------------------------------------


def _plus(stamp: str, hours: int) -> str:
    from datetime import UTC, datetime, timedelta

    t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (t + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _device_of(w: World, name: str) -> str:
    return w.dev[name]


def test_device_excluded_ends_a_members_device_and_never_an_owners(w):
    tom = _device_of(w, "tom")
    assert hub(w, "device.excluded", U1, device=tom, reason="lost") is None
    assert w.state()._core.ws.devices[tom].removed
    r = hub(w, "device.excluded", U1, device=_device_of(w, "sev"), reason="compromised")
    assert (r.code, "owner" in r.detail) == (Code.ROLE_DENIED, True)  # L2-P5-2: break-glass cannot be blocked
    assert not w.state()._core.ws.devices[_device_of(w, "sev")].removed


def test_device_excluded_needs_an_admin_and_a_known_device(w):
    assert hub(w, "device.excluded", U2, device=_device_of(w, "tom"), reason="lost").code == Code.ROLE_DENIED
    assert hub(w, "device.excluded", U1, device="d_" + "9" * 32, reason="lost").code == Code.DEVICE_UNKNOWN


def test_a_compromised_exclusion_ends_the_devices_grants(w):
    now = w.at()
    w.wev("grant.issued", "tom", grant="gr_01J9ZP0000000000000000GRT7", scope="workable", verbs="agent",
          issued_at=now, hours=8, expires_at=_plus(now, 8), secret_hash="sha256:" + "e" * 64)  # fmt: skip
    assert hub(w, "device.excluded", U1, device=_device_of(w, "tom"), reason="compromised") is None
    assert w.state()._core.ws.grants["gr_01J9ZP0000000000000000GRT7"].revoked


# --- policy.changed and settings.changed -------------------------------------------------------------------------


def five(approvers=("owner",), count=1, independent=False):
    return {"approvers": list(approvers), "count": count, "not": [], "applies": "all", "independent": independent}


def test_policy_changed_by_a_hub_admin_keeps_the_stored_factor_floor(w):
    w.wev("policy.changed", "sev", gates={"requirements": {**pol(["owner"], 1), "factor": "passkey"}})
    assert hub(w, "policy.changed", U1, gates={"requirements": five(["maintainer", "owner"], 2)}) is None
    p = w.state()._core.ws.policies["requirements"]
    assert p["factor"] == "passkey" and p["count"] == 2 and p["approvers"] == ["maintainer", "owner"]


def test_policy_changed_by_a_hub_admin_never_sets_a_factor_floor(w):
    assert hub(w, "policy.changed", U1, gates={"plan": five()}) is None
    assert "factor" not in w.state()._core.ws.policies["plan"]


def test_policy_changed_on_the_workspace_is_admin_only(w):
    assert hub(w, "policy.changed", U2, gates={"plan": five()}).code == Code.ROLE_DENIED


def test_settings_changed_is_admin_only_and_sets_the_three_keys(w):
    assert hub(w, "settings.changed", U2, set={"grant_hours": 72}).code == Code.ROLE_DENIED
    assert hub(w, "settings.changed", U1, set={"grant_hours": 72, "claim_ttl_min": 120}) is None
    s = w.state()._core.ws.settings
    assert (s["grant_hours"], s["claim_ttl_min"]) == (72, 120)


def test_replay_is_the_same_as_the_admission(w):
    req = request(w)
    admit_host(w, host_issue(w, req))
    hub(w, "settings.changed", U1, set={"lease_ttl_min": 60})
    st = replay(w.ws, w.tl, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id)
    assert not st.chain_errors and "gr_01J9ZP0000000000000000GRT1" in st._core.ws.grants


@pytest.mark.parametrize(
    "key,value",
    [
        ("sign_methods", ["terminal", "hub"]),
        ("repos", {"x": {"path": "../x"}}),
        ("models", None),
        ("publish", None),
        ("icon", None),
    ],
)
def test_a_hub_settings_changed_refuses_every_off_channel_key_at_append_and_on_replay(w, key, value):
    r = hub(w, "settings.changed", U1, set={key: value})
    assert r.code == Code.SETTINGS_INVALID
    # replay: the same line, forced into the log, is invalid there too
    e = w.build_unappended("workspace", "settings.changed", hub_actor(U1, CHAN), roster_v=w.roster_v,
                           set={key: value}, **hub_fields())  # fmt: skip
    e["host_sig"] = sig("hostx")
    w._append("workspace", e)
    st = replay(w.ws, w.tl, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id)
    assert any(i.type == "settings.changed" for i in st._core.logs["workspace"].invalid)
    assert key not in st._core.ws.settings


def test_device_excluded_lost_can_be_upgraded_to_compromised_like_device_revoked(w):
    tom = _device_of(w, "tom")
    assert hub(w, "device.excluded", U1, device=tom, reason="lost") is None
    assert hub(w, "device.excluded", U1, device=tom, reason="lost").code == Code.DEVICE_EXISTS  # same reason again
    now = w.at()
    w.wev("grant.issued", "mara", grant="gr_01J9ZP0000000000000000GRT8", scope="workable", verbs="agent",
          issued_at=now, hours=8, expires_at=_plus(now, 8), secret_hash="sha256:" + "e" * 64)  # fmt: skip
    assert hub(w, "device.excluded", U1, device=tom, reason="compromised") is None  # the upgrade is accepted
    assert hub(w, "device.excluded", U1, device=tom, reason="compromised").code == Code.DEVICE_EXISTS

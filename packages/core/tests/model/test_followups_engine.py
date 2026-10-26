"""Follow-up ledger transitions and derived grants, through append and replay."""

from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace

import pytest

from orch import canon, crypto
from orch.host.auth import build_auth
from orch.model import Code, advance, replay
from orch.model.types import ts
from tests.model.world import World, digest, refused, stamp, ulid


@pytest.fixture
def env():
    w = World().bootstrap({"tom": "member"})
    w.wev(
        "addon.granted",
        "sev",
        name="schedules",
        version="1.0.0",
        package_sha256=digest("schedules"),
        capabilities=[],
        fields={},
        sections=[],
        artifact_kinds=[],
    )
    uid = w.ticket(owner="tom")
    a = w.claim(uid, "tom")
    return w, uid, a


def set_followup(w, uid, a, **over):
    payload = dict(
        followup="fu_" + w.nid(), trigger="time", due=stamp(w.clock + timedelta(hours=2)), brief="Check the backfill"
    )
    payload.update(over)
    e, result = w.try_(uid, "followup.set", a, **payload)
    assert not hasattr(result, "code"), result
    return e


def fire(w, uid, e):
    w.clock += timedelta(hours=2)
    event, result = w.try_(
        uid,
        "followup.fired",
        w.HOST,
        followup=e["followup"],
        grant="gr_" + w.nid(),
        trigger="time",
        secret_hash=canon.grant_secret_hash(b"s" * 32),
        run="r_" + w.nid(),
    )
    assert not hasattr(result, "code"), result
    return event


def test_set_releases_claim_and_replaces(env):
    w, uid, a = env
    first = set_followup(w, uid, a)
    v = w.view(uid)
    assert v.followups[0]["state"] == "pending"
    assert v.claim is None and v.status == "in_progress"
    w.tev(uid, "claim.taken", a)
    set_followup(w, uid, a)
    old, new = w.view(uid).followups
    assert old["id"] == first["followup"] and old["state"] == "cancelled" and old["reason"] == "replaced"
    assert new["state"] == "pending"


@pytest.mark.parametrize("case", ["too_soon", "not_holder", "addon_off"])
def test_set_refused(env, case):
    w, uid, a = env
    due = stamp(w.clock + timedelta(minutes=30 if case == "too_soon" else 120))
    if case == "not_holder":
        w.tev(uid, "claim.released", a, session=a["session"], reason="released")
    if case == "addon_off":
        w.wev("addon.disabled", "sev", name="schedules")
    _, result = w.try_(uid, "followup.set", a, followup="fu_" + w.nid(), trigger="time", due=due, brief="Check")
    assert result.code == Code.FOLLOWUP_DENIED and case in result.detail
    assert not w.view(uid).followups


def test_fired_derives_and_outcome_consumes_grant(env, tmp_path):
    w, uid, a = env
    # A twelve-hour parent is reduced to the eight-hour run ceiling.
    parent = w.grant("sev", hours=12)
    w.tev(uid, "claim.released", a, session=a["session"], reason="released")
    a = w.claim(uid, gid=parent)
    e = set_followup(w, uid, a)
    f = fire(w, uid, e)
    st = w.state()
    g = st._core.ws.grants[f["grant"]]
    assert (g.scope, g.verbs, g.parent) == (uid, "agent", parent)
    assert ts(g.expires_at) - ts(g.issued_at) == 8 * 3600
    store = SimpleNamespace(state=st, locked=nullcontext, grant_secret_hash=lambda gid: f["secret_hash"])
    verified = build_auth(store, tmp_path).verify(g.id + "." + crypto.b64u(b"s" * 32), ts(w.at()))
    assert verified.state == "verified" and verified.grant.scope == uid
    runner = w.agent("sev", g.id)
    assert refused(w, uid, "log.added", runner, text="resumed") is None
    w.tev(uid, "log.added", runner, text="resumed")
    stranger = {**runner, "session": "s_" + ulid(1234)}
    assert refused(w, uid, "log.added", stranger, text="another run") == Code.GRANT_SCOPE
    other = w.ticket()
    assert refused(w, other, "log.added", runner, text="wrong ticket") == Code.GRANT_SCOPE
    from orch.model.authz import authorize_agent

    assert (
        authorize_agent(st._core, None, {"actor": runner, "at": w.at(), "type": "log.added"}).code == Code.GRANT_SCOPE
    )
    before = w.state()
    outcome, result = w.try_(uid, "followup.outcome", w.HOST, followup=e["followup"], outcome="ran", run=f["run"])
    assert not hasattr(result, "code"), result
    assert not before._core.ws.grants[g.id].ended  # scratch isolation
    incremental = advance(before, outcome, log=uid)
    rebuilt = w.state()
    assert incremental._core == rebuilt._core
    assert rebuilt._core.ws.grants[g.id].ended and not rebuilt._core.ws.grants[g.id].revoked
    assert refused(w, uid, "log.added", runner, text="again") == Code.GRANT_INVALID
    assert w.view(uid).followups[0]["state"] == "ran"


def test_chain_cap_and_person_cancel(env):
    w, uid, a = env
    for i in range(6):
        if i:
            w.tev(uid, "claim.taken", a)
        e = set_followup(w, uid, a)
        assert w.view(uid).followups[-1]["state"] == ("needs_person" if i == 5 else "pending")
    _, result = w.try_(uid, "followup.cancelled", "tom", followup=e["followup"], reason="person")
    assert not hasattr(result, "code"), result
    assert w.view(uid).followups[-1]["state"] == "cancelled"
    assert not w.state()._core.tickets[uid].fu_chain.get("sets")


def test_parent_revocation_is_derived_without_mutation(env):
    w, uid, a = env
    e = set_followup(w, uid, a)
    w.wev("grant.revoked", "tom", grant=a["grant"])
    state = w.state()
    assert state.tickets[uid].followups[0]["reason"] == "grant_revoked"
    assert state._core.tickets[uid].followups[e["followup"]].state == "pending"
    assert (
        replay(w.ws, w.tl, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id, hub_only=False)._core
        == state._core
    )


def test_transition_refusals_leave_grants_and_followups_unchanged(env):
    w, uid, a = env
    e = set_followup(w, uid, a)
    fid = e["followup"]
    assert refused(w, uid, "followup.outcome", w.HOST, followup=fid, outcome="ran") == Code.FOLLOWUP_DENIED
    payload = dict(
        followup=fid,
        grant="gr_" + w.nid(),
        trigger="time",
        secret_hash=canon.grant_secret_hash(b"s" * 32),
        run="r_" + w.nid(),
    )
    assert refused(w, uid, "followup.fired", w.HOST, **payload) == Code.FOLLOWUP_DENIED
    assert payload["grant"] not in w.state()._core.ws.grants
    f = fire(w, uid, e)
    assert refused(w, uid, "followup.fired", w.HOST, **payload) == Code.FOLLOWUP_DENIED
    assert refused(w, uid, "followup.cancelled", a, followup=fid, reason="agent") == Code.FOLLOWUP_DENIED
    assert refused(w, uid, "followup.outcome", w.HOST, followup=fid, outcome="failed") == Code.FOLLOWUP_DENIED
    assert not w.state()._core.ws.grants[f["grant"]].revoked
    _, result = w.try_(uid, "followup.outcome", w.HOST, followup=fid, outcome="failed", reason="no_writes")
    assert not hasattr(result, "code"), result
    assert w.view(uid).followups[0]["reason"] == "no_writes"


def test_agent_cancel_and_expired_parent_can_fire(env):
    w, uid, a = env
    e = set_followup(w, uid, a)
    _, result = w.try_(uid, "followup.cancelled", a, followup=e["followup"], reason="agent")
    assert not hasattr(result, "code"), result
    assert w.view(uid).followups[0]["reason"] == "agent"
    short = w.grant("tom", "workable", hours=1)
    a = w.claim(uid, "tom", gid=short)
    e = set_followup(w, uid, a)
    f = fire(w, uid, e)
    g = w.state()._core.ws.grants[f["grant"]]
    assert ts(g.expires_at) - ts(g.issued_at) == 3600


def test_a_follow_up_set_during_a_run_fires_after_the_first_outcome(env):
    w, uid, a = env
    e1 = set_followup(w, uid, a)
    f1 = fire(w, uid, e1)
    runner = w.agent("tom", f1["grant"])
    w.tev(uid, "claim.taken", runner)  # the fresh session picks the unclaimed ticket up
    e2 = set_followup(w, uid, runner)
    w.tev(uid, "followup.outcome", w.HOST, followup=e1["followup"], outcome="ran", run=f1["run"])
    assert [f["state"] for f in w.view(uid).followups] == ["ran", "pending"]
    f2 = fire(w, uid, e2)
    assert w.view(uid).followups[1]["state"] == "fired"
    assert w.state()._core.ws.grants[f2["grant"]].parent == f1["grant"]


def test_claim_verdicts_of_a_ticket_without_follow_ups_are_unchanged(env):
    w, uid, a = env
    other = w.agent("tom", w.grant("tom", "workable"))
    assert refused(w, uid, "claim.taken", other) == Code.CLAIM_EXISTS  # still claimed by a
    set_followup(w, uid, a)
    assert refused(w, uid, "claim.taken", other) is None  # released by the set: a waiting ticket may be picked up


def test_non_time_triggers_are_refused_until_wired(env):
    w, uid, a = env
    _, result = w.try_(
        uid, "followup.set", a, followup="fu_" + w.nid(), trigger="orch", deadline=stamp(w.clock + timedelta(hours=3)),
        brief="x",
    )  # fmt: skip
    assert result.code == Code.FOLLOWUP_DENIED

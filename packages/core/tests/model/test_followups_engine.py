"""Follow-up ledger transitions and derived grants, through append and replay."""

from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace

import pytest

from orch import canon, crypto
from orch.host.auth import build_auth
from orch.model import Code, advance, replay
from orch.model import followups as fu
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
        replay(w.ws, w.tl, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id)._core == state._core
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
    assert refused(w, uid, "claim.taken", w.agent("tom", short)) == Code.CLAIM_EXISTS  # cancelled: nothing waits now
    uid = w.ticket(owner="tom")
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


def _event_set(w, uid, a, on, trigger, **over):
    deadline = stamp(w.clock + timedelta(hours=3))
    return w.try_(
        uid, "followup.set", a, followup="fu_" + w.nid(), trigger=trigger, deadline=deadline, brief="x", on=on, **over
    )


def _fire_with(w, uid, e, trigger, **extra):
    return w.try_(
        uid, "followup.fired", w.HOST, followup=e["followup"], grant="gr_" + w.nid(), trigger=trigger,
        secret_hash=canon.grant_secret_hash(b"s" * 32), run="r_" + w.nid(), **extra,
    )  # fmt: skip


def test_addon_and_unmatched_triggers_are_refused(env):
    w, uid, a = env
    on = {"source": "orch", "kind": "question", "status": "answered"}
    _, result = _event_set(w, uid, a, on, "addon")
    assert result.code == Code.FOLLOWUP_DENIED and "does not fit" in result.detail
    for bad in (None, {"source": "webhook", "kind": "hook", "status": "rang", "hook": "0" * 64}):
        _, result = _event_set(w, uid, a, bad, "orch")
        assert result.code == Code.FOLLOWUP_DENIED
    _, result = _event_set(w, uid, a, on, "webhook")
    assert result.code == Code.FOLLOWUP_DENIED


def test_an_orch_follow_up_fires_on_its_event_only(env):
    w, uid, a = env
    on = {"source": "orch", "kind": "question", "status": "answered", "ref": "q1"}
    e, result = _event_set(w, uid, a, on, "orch")
    assert not hasattr(result, "code"), result
    assert w.view(uid).followups[0]["on"]["ref"] == "q1"
    w.clock += timedelta(minutes=5)
    other = {"source": "orch", "kind": "question", "ref": "q2", "status": "answered", "at": stamp(w.clock)}
    _, result = _fire_with(w, uid, e, "orch", event=other)
    assert result.code == Code.FOLLOWUP_DENIED
    wake = {**other, "ref": "q1"}
    _, result = _fire_with(w, uid, e, "orch", event=wake)
    assert not hasattr(result, "code"), result
    v = w.view(uid).followups[0]
    assert v["state"] == "fired" and v["event"]["ref"] == "q1"


def test_a_webhook_follow_up_fires_on_a_ring_and_falls_back_at_the_deadline(env):
    from orch.model import followup_events as fe

    w, uid, a = env
    hid = fe.new_hook_id()
    on = fe.parse_on("webhook:" + hid, "w1")
    e, result = _event_set(w, uid, a, on, "webhook", hook=on["hook"])
    assert not hasattr(result, "code"), result
    w.clock += timedelta(minutes=5)
    ring = fe.webhook_event(on["hook"], stamp(w.clock))
    _, result = _fire_with(w, uid, e, "webhook", event=ring)
    assert not hasattr(result, "code"), result
    assert w.view(uid).followups[0]["state"] == "fired"
    # a second follow-up: nothing rings, the deadline fires it with a fallback reason
    uid, a2 = w.ticket(owner="tom"), None
    a2 = w.claim(uid, "tom")
    on2 = fe.parse_on("webhook:" + fe.new_hook_id(), "w1")
    e2, result = _event_set(w, uid, a2, on2, "webhook", hook=on2["hook"])
    assert not hasattr(result, "code"), result
    w.clock += timedelta(hours=3)
    _, result = _fire_with(w, uid, e2, "deadline", fallback="no_event")
    assert result.code == Code.FOLLOWUP_DENIED  # the reason must be the webhook's own
    _, result = _fire_with(w, uid, e2, "deadline", fallback="no_ring")
    assert not hasattr(result, "code"), result
    assert w.view(uid).followups[-1]["fallback"] == "no_ring"


def test_the_claim_of_a_follow_up_session_is_not_live_after_its_outcome(env):
    from orch.model.claims import lapse_reason

    w, uid, a = env
    e1 = set_followup(w, uid, a)
    f1 = fire(w, uid, e1)
    runner = w.agent("tom", f1["grant"])
    w.tev(uid, "claim.taken", runner)
    assert lapse_reason(w.state()._core.ws, w.state()._core.tickets[uid], ts(w.at())) is None
    w.tev(uid, "followup.outcome", w.HOST, followup=e1["followup"], outcome="ran", run=f1["run"])
    assert lapse_reason(w.state()._core.ws, w.state()._core.tickets[uid], ts(w.at())) == "grant_ended"


def test_a_person_cancel_needs_the_followups_person_or_a_manager():
    w = World().bootstrap({"tom": "member", "ida": "member", "mara": "maintainer"})
    w.wev(
        "addon.granted", "sev", name="schedules", version="1.0.0", package_sha256=digest("schedules"),
        capabilities=[], fields={}, sections=[], artifact_kinds=[],
    )  # fmt: skip
    uid = w.ticket(owner="tom")
    a = w.claim(uid, "tom")
    for i in range(6):  # the sixth is needs_person: it can be cancelled too
        if i:
            w.tev(uid, "claim.taken", a)
        e = set_followup(w, uid, a)
    assert w.view(uid).followups[-1]["state"] == "needs_person"
    assert refused(w, uid, "followup.cancelled", "ida", followup=e["followup"], reason="person") == Code.FOLLOWUP_DENIED
    assert w.view(uid).followups[-1]["state"] == "needs_person"
    _, result = w.try_(uid, "followup.cancelled", "mara", followup=e["followup"], reason="person")  # a manager may
    assert not hasattr(result, "code"), result
    assert w.view(uid).followups[-1]["state"] == "cancelled"


def test_reschedule_changes_fire_window(env):
    w, uid, a = env
    e = set_followup(w, uid, a)
    due = stamp(w.clock + timedelta(hours=4))
    w.tev(uid, "followup.rescheduled", "tom", followup=e["followup"], due=due)
    assert w.view(uid).followups[0]["due"] == due
    w.clock += timedelta(hours=2)
    _, result = _fire_with(w, uid, e, "time")
    assert result.code == Code.FOLLOWUP_DENIED and "out_of_window" in result.detail
    fire(w, uid, e)
    assert w.view(uid).followups[0]["state"] == "fired"


def test_reschedule_needs_person_and_manager(env):
    w, uid, a = env
    for i in range(6):
        if i:
            w.tev(uid, "claim.taken", a)
        e = set_followup(w, uid, a)
    assert w.view(uid).followups[-1]["state"] == "needs_person"
    payload = dict(followup=e["followup"], due=stamp(w.clock + timedelta(hours=3)))
    assert refused(w, uid, "followup.rescheduled", "sev", **payload) == Code.FOLLOWUP_DENIED
    w.tev(uid, "followup.rescheduled", "tom", **payload)
    assert w.view(uid).followups[-1]["state"] == "pending"
    assert not w.state()._core.tickets[uid].fu_chain.get("sets")


@pytest.mark.parametrize("hours,code", [(0.5, "too_soon"), (15 * 24, "too_far")])
def test_reschedule_caps(env, hours, code):
    w, uid, a = env
    e = set_followup(w, uid, a)
    _, result = w.try_(
        uid, "followup.rescheduled", "tom", followup=e["followup"], due=stamp(w.clock + timedelta(hours=hours))
    )
    assert result.code == Code.FOLLOWUP_DENIED and code in result.detail


def test_event_reschedule_and_derived_deadline_cap(env):
    from orch.model import followups as fu

    w, uid, a = env
    on = {"source": "orch", "kind": "question", "status": "answered"}
    e, _ = _event_set(w, uid, a, on, "orch")
    deadline = stamp(w.clock + timedelta(hours=4))
    assert refused(w, uid, "followup.rescheduled", "tom", followup=e["followup"], due=deadline) == Code.FOLLOWUP_DENIED
    w.tev(uid, "followup.rescheduled", "tom", followup=e["followup"], deadline=deadline)
    rec = w.state()._core.tickets[uid].followups[e["followup"]]
    assert rec.deadline == deadline and rec.due is None and rec.on == on
    caps = fu.effective_caps({"max_days": 1})
    req = fu.SetRequest(0, "time", 7200, None, "x")
    assert fu.check_reschedule("pending", req, caps, old_deadline=2 * 86400).code == "too_far"


@pytest.mark.parametrize("seconds,person,ok", [(120, "tom", True), (121, "tom", False), (1, "sev", False)])
def test_restore_window_and_actor(env, seconds, person, ok):
    w, uid, a = env
    e = set_followup(w, uid, a)
    w.tev(uid, "followup.cancelled", "tom", followup=e["followup"], reason="person")
    w.clock += timedelta(seconds=seconds)
    _, result = w.try_(uid, "followup.restored", person, followup=e["followup"], at=w.at())
    assert (not hasattr(result, "code")) == ok
    if ok:
        assert w.view(uid).followups[0]["state"] == "pending"
        fire(w, uid, e)
        assert w.view(uid).followups[0]["state"] == "fired"
    else:
        assert result.code == Code.FOLLOWUP_DENIED


@pytest.mark.parametrize("reason", ["dismissed", "run_now", "agent", "derived", "newer", "deadline"])
def test_restore_refusals(env, reason):
    w, uid, a = env
    e = set_followup(w, uid, a)
    if reason == "run_now":
        for _ in range(5):
            w.tev(uid, "claim.taken", a)
            e = set_followup(w, uid, a)
    if reason == "derived":
        w.wev("grant.revoked", "tom", grant=a["grant"])
    else:
        if reason == "deadline":
            w.clock += timedelta(hours=2)
        if reason == "newer":
            w.tev(uid, "claim.taken", a)
        w.tev(
            uid,
            "followup.cancelled",
            a if reason == "agent" else "tom",
            followup=e["followup"],
            reason=reason if reason in ("dismissed", "run_now", "agent") else "person",
        )
        if reason == "newer":
            newer = set_followup(w, uid, a)
            w.tev(uid, "followup.cancelled", "tom", followup=newer["followup"], reason="person")
        if reason == "deadline":
            w.clock += timedelta(seconds=1)
    assert refused(w, uid, "followup.restored", "tom", followup=e["followup"]) == Code.FOLLOWUP_DENIED


def _one_pending_cap(monkeypatch):
    from dataclasses import replace

    from orch.model import followups_wire

    real = followups_wire.caps_of
    monkeypatch.setattr(followups_wire, "caps_of", lambda ws: replace(real(ws), pending_max=1))


def test_restore_counts_the_workspace_pending_cap(env, monkeypatch):
    w, uid, a = env
    e = set_followup(w, uid, a)
    w.tev(uid, "followup.cancelled", "tom", followup=e["followup"], reason="person")
    uid2 = w.ticket(owner="tom")
    set_followup(w, uid2, w.claim(uid2, "tom"))  # the one allowed pending follow-up of the workspace
    _one_pending_cap(monkeypatch)
    assert refused(w, uid, "followup.restored", "tom", followup=e["followup"]) == Code.FOLLOWUP_DENIED


def test_reschedule_of_needs_person_counts_the_workspace_pending_cap(env, monkeypatch):
    w, uid, a = env
    for i in range(6):
        if i:
            w.tev(uid, "claim.taken", a)
        e = set_followup(w, uid, a)
    assert w.view(uid).followups[-1]["state"] == "needs_person"
    uid2 = w.ticket(owner="tom")
    set_followup(w, uid2, w.claim(uid2, "tom"))
    _one_pending_cap(monkeypatch)
    due = stamp(w.clock + timedelta(hours=3))
    assert refused(w, uid, "followup.rescheduled", "tom", followup=e["followup"], due=due) == Code.FOLLOWUP_DENIED


def _two_people(*, needs_person=False):
    w = World().bootstrap({"tom": "member", "mara": "maintainer"})
    w.wev(
        "addon.granted", "sev", name="schedules", version="1.0.0", package_sha256=digest("schedules"),
        capabilities=[], fields={}, sections=[], artifact_kinds=[],
    )  # fmt: skip
    uid = w.ticket(owner="tom")
    a = w.claim(uid, "tom")
    for i in range(6 if needs_person else 1):
        if i:
            w.tev(uid, "claim.taken", a)
        e = set_followup(w, uid, a)
    return w, uid, a, e


def test_a_manager_cancel_cannot_be_undone(env):
    w, uid, a, e = _two_people(needs_person=True)
    assert w.view(uid).followups[-1]["state"] == "needs_person"
    w.tev(uid, "followup.cancelled", "mara", followup=e["followup"], reason="person")
    assert refused(w, uid, "followup.restored", "mara", followup=e["followup"]) == Code.FOLLOWUP_DENIED
    assert w.view(uid).followups[-1]["state"] == "cancelled"


def test_a_manager_reschedule_keeps_the_persons_chain_and_cannot_resume_needs_person():
    w, uid, a, e = _two_people(needs_person=True)
    due = stamp(w.clock + timedelta(hours=3))
    assert refused(w, uid, "followup.rescheduled", "mara", followup=e["followup"], due=due) == Code.FOLLOWUP_DENIED
    assert w.view(uid).followups[-1]["state"] == "needs_person"  # the guard ran: note_event comes after the handler
    assert w.state()._core.tickets[uid].fu_chain.get("sets")  # the refused event did not reset the chain
    w2, uid2, a2, e2 = _two_people()
    w2.tev(uid2, "followup.rescheduled", "mara", followup=e2["followup"], due=stamp(w2.clock + timedelta(hours=3)))
    assert w2.state()._core.tickets[uid2].fu_chain.get("sets")  # an allowed manager reschedule keeps it too


def test_reschedule_after_grant_revoked_is_refused(env):
    w, uid, a = env
    e = set_followup(w, uid, a)
    due = stamp(w.clock + timedelta(hours=3))
    w.wev("grant.revoked", "tom", grant=a["grant"])
    assert refused(w, uid, "followup.rescheduled", "tom", followup=e["followup"], due=due) == Code.FOLLOWUP_DENIED


def test_reschedule_after_ticket_left_in_progress_is_refused(env):
    w, uid, a = env
    e = set_followup(w, uid, a)
    w.tev(uid, "ticket.closed", "sev", resolution="other")
    due = stamp(w.clock + timedelta(hours=3))
    assert refused(w, uid, "followup.rescheduled", "tom", followup=e["followup"], due=due) == Code.FOLLOWUP_DENIED


def test_negative_undo_window_is_refused(env):
    w, uid, a = env
    e = set_followup(w, uid, a)
    w.clock += timedelta(seconds=30)
    w.tev(uid, "followup.cancelled", "tom", followup=e["followup"], reason="person")
    w.clock -= timedelta(seconds=60)  # an `at` before the cancel
    assert refused(w, uid, "followup.restored", "tom", followup=e["followup"]) is not None


def test_event_trigger_reschedule_needs_the_minimum_lead():
    caps = fu.effective_caps({})
    req = fu.SetRequest(0, "orch", None, 60, "x")
    assert fu.check_reschedule("pending", req, caps).code == "too_soon"

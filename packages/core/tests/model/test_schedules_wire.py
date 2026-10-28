"""Schedule authority is checked identically at append and replay."""

from datetime import timedelta

from orch import canon
from orch.model import Code, advance
from orch.model.types import ts
from tests.model.world import World, digest, refused, stamp


def grant_addon(w):
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


def setup(extra=None):
    w = World().bootstrap({"tom": "member", **(extra or {})})
    grant_addon(w)
    uid = w.ticket(owner="tom")
    due = w.clock.replace(second=0) + timedelta(hours=2)
    e, result = w.try_(
        uid,
        "schedule.set",
        "tom",
        schedule="sc_" + w.nid(),
        brief="Check progress",
        rule={"every": "once", "at": due.strftime("%H:%M"), "tz": "UTC", "date": due.strftime("%Y-%m-%d")},
        starts=w.at(),
        ends=stamp(due + timedelta(hours=1)),
        run_hours=2,
    )
    assert not hasattr(result, "code"), result
    return w, uid, e, due


def fire(w, uid, e, due):
    return w.try_(
        uid,
        "schedule.fired",
        w.HOST,
        schedule=e["schedule"],
        occurrence=stamp(due),
        grant="gr_" + w.nid(),
        secret_hash=canon.grant_secret_hash(b"s" * 32),
        run="r_" + w.nid(),
    )


def test_fired_bound_and_replay_and_duplicate():
    w, uid, e, due = setup()
    w.clock = due
    before = w.state()
    f, result = fire(w, uid, e, due)
    assert not hasattr(result, "code"), result
    assert advance(before, f, log=uid)._core == w.state()._core
    g = w.state()._core.ws.grants[f["grant"]]
    assert g.scope == uid and g.parent == e["id"] and g.verbs == "agent"
    assert ts(g.expires_at) <= ts(e["ends"])
    assert ts(g.expires_at) - ts(g.issued_at) <= e["run_hours"] * 3600
    assert hasattr(fire(w, uid, e, due)[1], "code")
    w.tev(uid, "schedule.cancelled", "tom", schedule=e["schedule"])
    assert w.state()._core.ws.grants[g.id].ended


def test_cancel_and_role_loss_refuse_firing():
    for cancel in (True, False):
        w, uid, e, due = setup()
        if cancel:
            w.tev(uid, "schedule.cancelled", "tom", schedule=e["schedule"])
        else:
            w.wev("role.changed", "sev", person=w.people["tom"], role="viewer")
        w.clock = due
        assert hasattr(fire(w, uid, e, due)[1], "code")


def test_agent_cannot_set():
    w, uid, e, due = setup()
    a = w.agent("tom", w.grant("tom"))
    payload = {k: e[k] for k in ("brief", "rule", "starts", "ends", "run_hours")}
    _, result = w.try_(uid, "schedule.set", a, schedule="sc_" + w.nid(), **payload)
    assert hasattr(result, "code")


def test_device_loss_and_outcome_needs_you():
    w, uid, e, due = setup()
    w.wev("device.removed", "tom", device=w.dev["tom"])
    w.clock = due
    _, result = fire(w, uid, e, due)
    assert hasattr(result, "code")
    _, result = w.try_(
        uid,
        "schedule.outcome",
        w.HOST,
        schedule=e["schedule"],
        occurrence=stamp(due),
        outcome="did_not_run",
        reason="device_lost",
    )
    assert not hasattr(result, "code"), result
    assert any(n.kind == "schedule" and n.detail == "device_lost" for n in w.view(uid).needs)
    assert hasattr(fire(w, uid, e, due)[1], "code")


def test_grant_works_only_on_ticket_and_ends_at_cancel():
    w, uid, e, due = setup()
    w.clock = due
    fired, result = fire(w, uid, e, due)
    assert not hasattr(result, "code"), result
    actor = w.agent("tom", fired["grant"])
    assert refused(w, uid, "log.added", actor, text="scheduled work") is None
    w.tev(uid, "log.added", actor, text="scheduled work")
    other = w.ticket(owner="tom")
    assert refused(w, other, "log.added", actor, text="wrong scope") is not None
    w.tev(uid, "schedule.cancelled", "tom", schedule=e["schedule"])
    assert refused(w, uid, "log.added", actor, text="after cancel") is not None


def test_addon_off_refuses_and_ends_run_grants_and_on_again_fires_fresh():
    from orch.model.followups import chain_valid
    from orch.model.followups_wire import grant_chain
    from orch.model.schedules_wire import root_valid

    w = World().bootstrap({"tom": "member"})
    grant_addon(w)
    uid = w.ticket(owner="tom")
    due = w.clock.replace(second=0) + timedelta(hours=2)
    e, result = w.try_(
        uid,
        "schedule.set",
        "tom",
        schedule="sc_" + w.nid(),
        brief="Daily check",
        rule={"every": "day", "at": due.strftime("%H:%M"), "tz": "UTC"},
        starts=w.at(),
        ends=stamp(due + timedelta(days=2, hours=1)),
        run_hours=2,
    )
    assert not hasattr(result, "code"), result
    w.clock = due
    fired, result = fire(w, uid, e, due)
    assert not hasattr(result, "code"), result
    actor = w.agent("tom", fired["grant"])
    assert refused(w, uid, "log.added", actor, text="scheduled work") is None

    w.clock = due + timedelta(minutes=10)
    w.wev("addon.disabled", "sev", name="schedules")
    core, now = w.state()._core, ts(w.at())
    chain = grant_chain(core.ws, fired["grant"])
    assert chain and not root_valid(core, chain[-1].id, now)
    assert core.ws.grants[fired["grant"]].ended
    assert refused(w, uid, "log.added", actor, text="add-on off") is not None
    payload = {k: e[k] for k in ("brief", "rule", "ends", "run_hours")}
    _, result = w.try_(uid, "schedule.set", "tom", schedule="sc_" + w.nid(), starts=w.at(), **payload)
    assert "addon_off" in result.detail

    second = due + timedelta(days=1)
    w.clock = second
    result = fire(w, uid, e, second)[1]
    assert result.code == Code.SCHEDULE_DENIED and "addon_off" in result.detail
    _, result = w.try_(
        uid,
        "schedule.outcome",
        w.HOST,
        schedule=e["schedule"],
        occurrence=stamp(second),
        outcome="did_not_run",
        reason="addon_disabled",
    )
    assert not hasattr(result, "code"), result

    w.clock = second + timedelta(hours=1)
    grant_addon(w)
    # On again: the occurrence missed while off stays did_not_run and the old run grant stays ended.
    assert hasattr(fire(w, uid, e, second)[1], "code")
    assert refused(w, uid, "log.added", actor, text="revived?") is not None
    third = due + timedelta(days=2)
    w.clock = third
    again, result = fire(w, uid, e, third)
    assert not hasattr(result, "code"), result
    assert again["grant"] != fired["grant"]
    core, now = w.state()._core, ts(w.at())
    chain = grant_chain(core.ws, again["grant"])
    assert root_valid(core, chain[-1].id, now) and chain_valid(chain, at=now, root_person_active=True)
    assert refused(w, uid, "log.added", w.agent("tom", again["grant"]), text="fresh run") is None


def test_purge_ends_schedule_root():
    from orch.model.schedules_wire import root_valid

    w, uid, e, due = setup()
    w.clock = due
    fired, result = fire(w, uid, e, due)
    assert not hasattr(result, "code"), result
    w.wev("addon.purged", "sev", name="schedules")
    assert not root_valid(w.state()._core, e["id"], ts(w.at()))
    assert refused(w, uid, "log.added", w.agent("tom", fired["grant"]), text="purged") is not None


def test_fabricated_occurrence_and_early_outcome_refused():
    w, uid, e, due = setup()
    _, result = w.try_(
        uid,
        "schedule.outcome",
        w.HOST,
        schedule=e["schedule"],
        occurrence=stamp(due),
        outcome="did_not_run",
        reason="missed",
    )
    assert hasattr(result, "code")
    w.clock = due
    assert hasattr(fire(w, uid, e, due + timedelta(minutes=1))[1], "code")


def test_workspace_concurrency_cap_and_cancel_releases_slot():
    w, uid, first, due = setup()
    events = [first]
    for _ in range(2):
        payload = {k: first[k] for k in ("brief", "rule", "starts", "ends", "run_hours")}
        event, result = w.try_(uid, "schedule.set", "tom", schedule="sc_" + w.nid(), **payload)
        assert not hasattr(result, "code"), result
        events.append(event)
    w.clock = due
    for event in events[:2]:
        assert not hasattr(fire(w, uid, event, due)[1], "code")
    assert "concurrent_cap" in fire(w, uid, events[2], due)[1].detail
    w.tev(uid, "schedule.cancelled", "tom", schedule=first["schedule"])
    assert not hasattr(fire(w, uid, events[2], due)[1], "code")


def test_only_setter_or_owner_can_cancel():
    w, uid, e, due = setup()
    # The owner may cancel another person's schedule.
    _, result = w.try_(uid, "schedule.cancelled", "sev", schedule=e["schedule"])
    assert not hasattr(result, "code"), result


def test_crafted_far_future_starts_refused():
    w = World().bootstrap({"tom": "member"})
    grant_addon(w)
    uid = w.ticket(owner="tom")
    start = w.clock.replace(second=0) + timedelta(days=365)
    due = start + timedelta(hours=2)
    _, result = w.try_(
        uid,
        "schedule.set",
        "tom",
        schedule="sc_" + w.nid(),
        brief="Later",
        rule={"every": "once", "at": due.strftime("%H:%M"), "tz": "UTC", "date": due.strftime("%Y-%m-%d")},
        starts=stamp(start),
        ends=stamp(due + timedelta(hours=1)),
        run_hours=2,
    )
    assert result.code == Code.SCHEDULE_DENIED and "starts must be the signing time" in result.detail


def test_unclean_label_refused_and_clean_label_kept():
    w, uid, e, due = setup()
    payload = {k: e[k] for k in ("brief", "rule", "ends", "run_hours")}
    for bad in ("two\nlines", "rtl\u202eoverride", "bell\x07"):
        _, result = w.try_(uid, "schedule.set", "tom", schedule="sc_" + w.nid(), starts=w.at(), label=bad, **payload)
        assert result.code == Code.SCHEDULE_DENIED and "label" in result.detail
    ok, result = w.try_(
        uid, "schedule.set", "tom", schedule="sc_" + w.nid(), starts=w.at(), label="Weekly report", **payload
    )
    assert not hasattr(result, "code"), result
    assert w.state()._core.tickets[uid].schedules[ok["schedule"]].label == "Weekly report"


def test_occurrence_must_be_canonical_for_fired_and_outcome():
    from orch.model import schedules_wire

    w, uid, e, due = setup()
    w.clock = due
    canonical = stamp(due)
    spellings = (canonical.replace("Z", "+00:00"), canonical.replace("Z", ".000Z"), canonical[:-1])
    for occurrence in spellings:
        _, result = w.try_(
            uid,
            "schedule.fired",
            w.HOST,
            schedule=e["schedule"],
            occurrence=occurrence,
            grant="gr_" + w.nid(),
            secret_hash=canon.grant_secret_hash(b"s" * 32),
            run="r_" + w.nid(),
        )
        assert result.code == Code.SCHEDULE_DENIED and "canonical" in result.detail
        _, result = w.try_(
            uid,
            "schedule.outcome",
            w.HOST,
            schedule=e["schedule"],
            occurrence=occurrence,
            outcome="did_not_run",
            reason="missed",
        )
        assert result.code == Code.SCHEDULE_DENIED and "canonical" in result.detail
        core = w.state()._core
        host = {"actor": w.HOST, "at": w.at(), "schedule": e["schedule"], "occurrence": occurrence}
        assert (
            schedules_wire.outcome_event(
                core, core.tickets[uid], {**host, "outcome": "did_not_run", "reason": "missed"}
            ).code
            == Code.SCHEDULE_DENIED
        )
    assert w.state()._core.tickets[uid].schedules[e["schedule"]].occurrences == {}
    # The canonical spelling still fires exactly once.
    assert not hasattr(fire(w, uid, e, due)[1], "code")
    assert hasattr(fire(w, uid, e, due)[1], "code")


def restrict_away_from_tom(w, uid):
    w.tev(uid, "visibility.changed", "sev", visibility={"restricted": [w.people["sev"]]})


def test_access_lost_refuses_firing_and_settles_did_not_run():
    from orch.model.schedules_wire import blocked

    w, uid, e, due = setup()
    restrict_away_from_tom(w, uid)
    w.clock = due
    result = fire(w, uid, e, due)[1]
    assert result.code == Code.SCHEDULE_DENIED and result.detail == "access_lost"
    core = w.state()._core
    t = core.tickets[uid]
    assert blocked(core, t, t.schedules[e["schedule"]], ts(w.at())) == "access_lost"
    _, result = w.try_(
        uid,
        "schedule.outcome",
        w.HOST,
        schedule=e["schedule"],
        occurrence=stamp(due),
        outcome="did_not_run",
        reason="access_lost",
    )
    assert not hasattr(result, "code"), result


def test_access_lost_ends_run_grant_and_chain():
    from orch.model.followups_wire import grant_chain
    from orch.model.schedules_wire import root_valid

    w, uid, e, due = setup()
    w.clock = due
    fired, result = fire(w, uid, e, due)
    assert not hasattr(result, "code"), result
    actor = w.agent("tom", fired["grant"])
    assert refused(w, uid, "log.added", actor, text="scheduled work") is None
    w.clock = due + timedelta(minutes=5)
    restrict_away_from_tom(w, uid)
    core, now = w.state()._core, ts(w.at())
    chain = grant_chain(core.ws, fired["grant"])
    assert chain and chain[-1].id == e["id"]
    assert not root_valid(core, e["id"], now)
    assert refused(w, uid, "log.added", actor, text="after access lost") is not None
    # A new schedule by the person who lost access is refused too.
    payload = {k: e[k] for k in ("brief", "rule", "ends", "run_hours")}
    _, result = w.try_(uid, "schedule.set", "tom", schedule="sc_" + w.nid(), starts=w.at(), **payload)
    assert hasattr(result, "code")


def test_set_refused_without_access_to_the_ticket():
    from orch.model import schedules_wire

    w, uid, e, due = setup()
    restrict_away_from_tom(w, uid)
    core = w.state()._core
    t = core.tickets[uid]
    attempt = {**e, "schedule": "sc_" + w.nid(), "at": w.at(), "starts": w.at()}
    result = schedules_wire.set_event(core, t, attempt)
    assert result.code == Code.SCHEDULE_DENIED and "access_lost" in result.detail


def test_host_early_window_is_the_model_window():
    from types import SimpleNamespace

    from orch.model import followups as fu
    from orch.model.schedules_wire import early_s

    assert early_s(SimpleNamespace(prefix="DEMO", settings={})) == fu.FU_TIME_EARLY_S == 300
    assert early_s(SimpleNamespace(prefix="SBX", settings={})) == fu.DEV_EARLY_S
    # At the very start of the window the model admits the firing (never refused for earliness).
    # (each built event ticks the clock 10 s, so the clock is set 10 s before the intended `at`)
    w, uid, e, due = setup()
    w.clock = due - timedelta(seconds=early_s(w.state()._core.ws) + 10)
    assert not hasattr(fire(w, uid, e, due)[1], "code")
    w2, uid2, e2, due2 = setup()
    w2.clock = due2 - timedelta(seconds=early_s(w2.state()._core.ws) + 11)
    assert "missed" in fire(w2, uid2, e2, due2)[1].detail


def test_fire_releases_own_claim_but_refuses_another_person():
    w, uid, e, due = setup()
    # The setter's own session claimed under a one-hour grant: by the occurrence the claim has lapsed (dead session).
    w.claim(uid, "tom", gid=w.grant("tom", "workable", hours=1))
    w.clock = due
    f, result = fire(w, uid, e, due)
    assert not hasattr(result, "code"), result
    assert w.view(uid).claim is None
    assert w.state()._core.tickets[uid].schedules[e["schedule"]].occurrences[stamp(due)]["released"] is True
    assert refused(w, uid, "claim.taken", w.agent("tom", f["grant"])) is None

    w, uid, e, due = setup()
    w.clock = due - timedelta(minutes=1)
    w.claim(uid, "sev")
    w.clock = due
    assert "claimed_by_other" in fire(w, uid, e, due)[1].detail


def second_schedule(w, uid, e):
    payload = {k: e[k] for k in ("brief", "rule", "starts", "ends", "run_hours")}
    event, result = w.try_(uid, "schedule.set", "tom", schedule="sc_" + w.nid(), **payload)
    assert not hasattr(result, "code"), result
    return event


def test_fire_refuses_busy_while_setters_run_holds_claim_then_releases_dead_run():
    from orch.model.schedules_wire import blocked

    w, uid, e, due = setup()
    other = second_schedule(w, uid, e)
    w.clock = due
    first, result = fire(w, uid, e, due)
    assert not hasattr(result, "code"), result
    run = w.claim(uid, "tom", gid=first["grant"])
    # The setter's own earlier run session holds the claim with a live run grant: busy, never released.
    result = fire(w, uid, other, due)[1]
    assert result.code == Code.SCHEDULE_DENIED and result.detail == "busy"
    core = w.state()._core
    t = core.tickets[uid]
    assert blocked(core, t, t.schedules[other["schedule"]], ts(w.at())) == "busy"
    assert w.view(uid).claim.session == run["session"]
    # Once that run settled its grant ended: the dead session's claim is released by the firing.
    w.tev(uid, "schedule.outcome", w.HOST, schedule=e["schedule"], occurrence=stamp(due), outcome="ran")
    again, result = fire(w, uid, other, due)
    assert not hasattr(result, "code"), result
    assert w.view(uid).claim is None
    assert refused(w, uid, "claim.taken", w.agent("tom", again["grant"])) is None


def test_busy_is_a_schedule_outcome_reason():
    from orch.model import schedules

    assert "busy" in schedules.REASONS
    w, uid, e, due = setup()
    w.clock = due
    _, result = w.try_(
        uid,
        "schedule.outcome",
        w.HOST,
        schedule=e["schedule"],
        occurrence=stamp(due),
        outcome="did_not_run",
        reason="busy",
    )
    assert not hasattr(result, "code"), result


def backdated_daily(w, uid):
    """A daily 09:00 rule signed at 09:30 whose `starts` is backdated to 08:40 (inside SET_PAST_S)."""
    nine = (w.clock + timedelta(days=1)).replace(hour=9, minute=0, second=0)
    w.clock = nine + timedelta(minutes=30) - timedelta(seconds=10)
    e, result = w.try_(
        uid,
        "schedule.set",
        "tom",
        schedule="sc_" + w.nid(),
        brief="Daily check",
        rule={"every": "day", "at": "09:00", "tz": "UTC"},
        starts=stamp(nine - timedelta(minutes=20)),
        ends=stamp(nine + timedelta(days=2)),
        run_hours=2,
    )
    assert not hasattr(result, "code"), result
    assert e["at"] == stamp(nine + timedelta(minutes=30))
    return e, nine


def test_backdated_starts_never_admits_an_occurrence_before_signing():
    w = World().bootstrap({"tom": "member"})
    grant_addon(w)
    uid = w.ticket(owner="tom")
    e, nine = backdated_daily(w, uid)
    assert w.state()._core.tickets[uid].schedules[e["schedule"]].set_at == e["at"]
    # 09:00 is inside the late window and after `starts`, but before the person signed: never fired nor settled.
    w.clock = nine + timedelta(minutes=31)
    result = fire(w, uid, e, nine)[1]
    assert result.code == Code.SCHEDULE_DENIED and "before_set" in result.detail
    _, result = w.try_(
        uid,
        "schedule.outcome",
        w.HOST,
        schedule=e["schedule"],
        occurrence=stamp(nine),
        outcome="did_not_run",
        reason="missed",
    )
    assert result.code == Code.SCHEDULE_DENIED and "before_set" in result.detail
    assert w.state()._core.tickets[uid].schedules[e["schedule"]].occurrences == {}
    nxt = nine + timedelta(days=1)
    w.clock = nxt
    assert not hasattr(fire(w, uid, e, nxt)[1], "code")


def test_run_claims_without_takeover_only_after_its_firing_released_the_setters_claim():
    w, uid, e, due = setup({"bob": "member"})
    # Bob worked the ticket and released it through a follow-up he then cancelled: in_progress, no claim.
    bob = w.claim(uid, "bob")
    fu, result = w.try_(
        uid,
        "followup.set",
        bob,
        followup="fu_" + w.nid(),
        trigger="time",
        due=stamp(w.clock + timedelta(hours=2)),
        brief="Check later",
    )
    assert not hasattr(result, "code"), result
    w.tev(uid, "followup.cancelled", "bob", followup=fu["followup"], reason="person")
    v = w.view(uid)
    assert v.status == "in_progress" and v.claim is None
    w.clock = due
    f, result = fire(w, uid, e, due)
    assert not hasattr(result, "code"), result
    row = w.state()._core.tickets[uid].schedules[e["schedule"]].occurrences[stamp(due)]
    assert "released" not in row
    # Nothing of Tom's was released: his run follows the rule Tom himself would (a takeover from nobody is refused).
    assert refused(w, uid, "claim.taken", w.agent("tom", f["grant"])) == Code.CLAIM_EXISTS


def test_released_row_admits_only_its_live_grant():
    w, uid, e, due = setup()
    w.claim(uid, "tom", gid=w.grant("tom", "workable", hours=1))
    w.clock = due
    f, result = fire(w, uid, e, due)
    assert not hasattr(result, "code"), result
    w.tev(
        uid,
        "schedule.outcome",
        w.HOST,
        schedule=e["schedule"],
        occurrence=stamp(due),
        outcome="did_not_run",
        reason="runner_unavailable",
    )
    core = w.state()._core
    from orch.model import claims

    e2 = {"actor": w.agent("tom", f["grant"]), "at": w.at(), "id": "x"}
    assert claims.taken(core.ws, core.tickets[uid], e2).code == Code.CLAIM_EXISTS


def test_live_claim_of_the_setter_under_an_ordinary_grant_is_busy():
    from orch.model.schedules_wire import blocked

    w, uid, e, due = setup()
    w.clock = due - timedelta(minutes=1)
    a = w.claim(uid, "tom")  # an interactive session's claim under an ordinary grant, live at the occurrence
    w.clock = due
    result = fire(w, uid, e, due)[1]
    assert result.code == Code.SCHEDULE_DENIED and result.detail == "busy"
    core = w.state()._core
    t = core.tickets[uid]
    assert blocked(core, t, t.schedules[e["schedule"]], ts(w.at())) == "busy"
    assert w.view(uid).claim.session == a["session"]


def test_schedule_view_row_carries_set_at_outcomes_and_grant_hours_cap():
    from orch.store.render import thaw

    w, uid, e, due = setup()
    row = w.view(uid).schedules[0]
    assert row["set_at"] == e["at"] and row["outcomes"] == () and row["grant_hours_cap"] == 8
    w.clock = due
    fire(w, uid, e, due)
    w.tev(uid, "schedule.outcome", w.HOST, schedule=e["schedule"], occurrence=stamp(due), outcome="ran")
    row = w.view(uid).schedules[0]
    assert thaw(row["outcomes"]) == [{"occurrence": stamp(due), "outcome": "ran", "reason": None}]
    w.wev("settings.changed", "sev", set={"grant_hours": 3})
    assert w.view(uid).schedules[0]["grant_hours_cap"] == 3
    w.wev("role.changed", "sev", person=w.people["tom"], role="maintainer")
    assert w.view(uid).schedules[0]["grant_hours_cap"] == 8


def test_ended_run_grant_lapses_the_runs_claim():
    from orch.model import claims

    for end in ("outcome", "cancelled", "addon_off"):
        w, uid, e, due = setup()
        w.clock = due
        f, result = fire(w, uid, e, due)
        assert not hasattr(result, "code"), result
        run = w.claim(uid, "tom", gid=f["grant"])
        assert refused(w, uid, "log.added", run, text="working") is None
        if end == "outcome":
            w.tev(uid, "schedule.outcome", w.HOST, schedule=e["schedule"], occurrence=stamp(due), outcome="ran")
        elif end == "cancelled":
            w.tev(uid, "schedule.cancelled", "tom", schedule=e["schedule"])
        else:
            w.wev("addon.disabled", "sev", name="schedules")
        core = w.state()._core
        t = core.tickets[uid]
        # t.claim stays recorded until the host releases it lazily, but it is not live: the grant ended.
        assert t.claim is not None and t.claim.session == run["session"]
        assert claims.lapse_reason(core.ws, t, ts(w.at())) == "grant_ended", end
        assert refused(w, uid, "log.added", run, text="after end") is not None


def test_ticket_people_changes_do_not_remove_the_right_to_act():
    """`blocked` uses the model's acting predicate for an agent's person (authorize_agent): a role that may run
    agents plus `can_see`. Ticket people (assignees, reviewers) never gate writes, so dropping the setter from the
    assignees of a ticket restricted to them leaves both the firing and the run's writes admitted; a viewer role or
    leaving the restricted list is what removes it (role_lost, access_lost)."""
    from orch.model.schedules_wire import blocked

    w, uid, e, due = setup()
    w.tev(uid, "visibility.changed", "sev", visibility={"restricted": [w.people["sev"], w.people["tom"]]})
    w.tev(uid, "people.changed", "sev", role="assignees", add=[w.people["tom"]], remove=[])
    w.tev(uid, "people.changed", "sev", role="assignees", add=[], remove=[w.people["tom"]])
    w.clock = due
    core = w.state()._core
    t = core.tickets[uid]
    assert blocked(core, t, t.schedules[e["schedule"]], ts(w.at())) is None
    f, result = fire(w, uid, e, due)
    assert not hasattr(result, "code"), result
    run = w.agent("tom", f["grant"])
    assert refused(w, uid, "claim.taken", run) is None
    w.wev("role.changed", "sev", person=w.people["tom"], role="viewer")
    core = w.state()._core
    t = core.tickets[uid]
    assert blocked(core, t, t.schedules[e["schedule"]], ts(w.at())) == "role_lost"
    assert refused(w, uid, "log.added", run, text="as a viewer") is not None


def test_schedule_target_validation_in_model():
    w, uid, old, due = setup()
    w.validate = False  # exercise the model independently of the wire schema
    payload = {k: old[k] for k in ("brief", "rule", "starts", "ends", "run_hours")}
    for extra in (
        {"target": "other"},
        {"target": None},
        {"title": "Mail"},
        {"target": "new"},
        {"target": "new", "title": "a\u200bb"},
        {"target": "new", "title": "x" * 101},
    ):
        _, result = w.try_(uid, "schedule.set", "tom", schedule="sc_" + w.nid(), **payload, **extra)
        assert result.code == Code.SCHEDULE_DENIED


def setup_new():
    w = World().bootstrap({"tom": "member"})
    grant_addon(w)
    uid, sid = w.nid(), "sc_" + w.nid()
    w.tev(
        uid,
        "ticket.created",
        "tom",
        key="DEMO-0001",
        ticket_type="chore",
        title="Mail {literal}",
        owner=w.people["tom"],
        schedule=sid,
    )
    due = w.clock.replace(second=0) + timedelta(hours=2)
    event, result = w.try_(
        uid,
        "schedule.set",
        "tom",
        schedule=sid,
        target="new",
        title="Mail {literal}",
        brief="Agent text must not become a title",
        rule={"every": "day", "at": due.strftime("%H:%M"), "tz": "UTC"},
        starts=w.at(),
        ends=stamp(due + timedelta(days=3)),
        run_hours=2,
    )
    assert not hasattr(result, "code"), result
    return w, uid, event, due


def fire_new(w, uid, event, due):
    from orch.model.schedules import child_uid

    return w.try_(
        uid,
        "schedule.fired",
        w.HOST,
        schedule=event["schedule"],
        occurrence=stamp(due),
        child=child_uid(event["schedule"], stamp(due)),
        grant="gr_" + w.nid(),
        secret_hash=canon.grant_secret_hash(b"s" * 32),
        run="r_" + w.nid(),
    )


def create_child(w, uid, event, fired, **over):
    from orch.model import admit
    from orch.model.schedules import child_title
    from tests.schema.examples import sig

    rec = w.state()._core.tickets[uid].schedules[event["schedule"]]
    child = fired["child"]
    payload = {
        "key": "DEMO-0002",
        "ticket_type": "chore",
        "title": child_title(rec, fired["occurrence"]),
        "owner": rec.person,
        "via": {
            "anchor": uid,
            "schedule_id": rec.id,
            "occurrence": fired["occurrence"],
            "set_sig_hash": rec.set_sig_hash,
        },
        **over,
    }
    e = w.build_unappended(child, "ticket.created", "tom", **payload)
    for key in ("sig", "auth", "roster_v"):
        e.pop(key)
    result = admit(w.state(), e, log=child)
    if not hasattr(result, "code"):
        e["host_sig"] = sig("child")
        w._append(child, e)
    return e, result


def test_new_child_identity_title_authority_and_replay():
    from orch.model.schedules import child_uid

    w, uid, event, due = setup_new()
    w.clock = due
    fired, result = fire_new(w, uid, event, due)
    assert not hasattr(result, "code"), result
    assert fired["child"] == child_uid(event["schedule"], stamp(due))
    before = w.state()  # crash here, reload the fired intent before creating
    assert fired["child"] not in before._core.tickets
    child_event, result = create_child(w, uid, event, fired)
    assert not hasattr(result, "code"), result
    assert advance(before, child_event, log=fired["child"])._core == w.state()._core
    assert hasattr(create_child(w, uid, event, fired)[1], "code")
    child = w.state()._core.tickets[fired["child"]]
    assert child.fields["title"] == f"Mail {{literal}} {due:%Y-%m-%d}"
    assert child.fields["parent"] == "DEMO-0001" and child.ticket_type == "chore" and child.status == "open"
    assert len(w.state()._core.tickets) == 2
    agent = w.agent("tom", fired["grant"])
    assert refused(w, child.uid, "log.added", agent, text="mail contents") is None
    assert refused(w, uid, "log.added", agent, text="touch anchor") == Code.GRANT_SCOPE
    assert (
        refused(
            w,
            w.nid(),
            "ticket.created",
            agent,
            key="DEMO-0003",
            title="Untrusted",
            ticket_type="chore",
            owner=w.people["tom"],
        )
        == Code.GRANT_SCOPE
    )
    assert refused(w, uid, "ticket.closed", agent, resolution="other") == Code.GRANT_SCOPE


def test_new_fire_rechecks_role_device_close_and_cancel():
    for change, reason in (
        ("role", "role_lost"),
        ("device", "device_lost"),
        ("close", "ticket_closed"),
        ("cancel", "live"),
    ):
        w, uid, event, due = setup_new()
        if change == "role":
            w.wev("role.changed", "sev", person=w.people["tom"], role="viewer")
        elif change == "device":
            w.wev("device.removed", "tom", device=w.dev["tom"])
        elif change == "close":
            w.tev(uid, "ticket.closed", "tom", resolution="other")
        else:
            w.tev(uid, "schedule.cancelled", "tom", schedule=event["schedule"])
        w.clock = due
        _, result = fire_new(w, uid, event, due)
        assert result.code == Code.SCHEDULE_DENIED and reason in result.detail
        assert len(w.state()._core.tickets) == 1


def test_new_child_creation_rechecks_authority_after_crash():
    w, uid, event, due = setup_new()
    w.clock = due
    fired, _ = fire_new(w, uid, event, due)
    w.wev("role.changed", "sev", person=w.people["tom"], role="viewer")
    assert create_child(w, uid, event, fired)[1].detail == "role_lost"
    assert fired["child"] not in w.state()._core.tickets


def test_new_child_nothing_to_do_and_open_limit():
    w, uid, event, due = setup_new()
    assert refused(w, uid, "ticket.closed", "tom", resolution="nothing_to_do") == Code.STATUS_TRANSITION
    w.clock = due
    fired, _ = fire_new(w, uid, event, due)
    assert not hasattr(create_child(w, uid, event, fired)[1], "code")
    agent = w.agent("tom", fired["grant"])
    _, result = w.try_(
        uid, "schedule.outcome", w.HOST, schedule=event["schedule"], occurrence=stamp(due), outcome="ran_nothing"
    )
    assert hasattr(result, "code")
    _, result = w.try_(fired["child"], "ticket.closed", agent, resolution="nothing_to_do")
    assert not hasattr(result, "code"), result
    w.tev(uid, "schedule.outcome", w.HOST, schedule=event["schedule"], occurrence=stamp(due), outcome="ran_nothing")
    assert w.state()._core.tickets[fired["child"]].status == "closed"
    w.clock = due + timedelta(days=1)
    assert not hasattr(fire_new(w, uid, event, w.clock)[1], "code")

    w, uid, event, due = setup_new()
    w.clock = due
    fired, _ = fire_new(w, uid, event, due)
    create_child(w, uid, event, fired)
    w.tev(uid, "schedule.outcome", w.HOST, schedule=event["schedule"], occurrence=stamp(due), outcome="ran")
    w.clock = due + timedelta(days=1)
    assert fire_new(w, uid, event, w.clock)[1].detail == "skipped_open_child"


def test_new_cancel_or_close_keeps_inflight_child_grant():
    for close in (False, True):
        w, uid, event, due = setup_new()
        w.clock = due
        fired, _ = fire_new(w, uid, event, due)
        create_child(w, uid, event, fired)
        if close:
            w.tev(uid, "ticket.closed", "tom", resolution="other")
        else:
            w.tev(uid, "schedule.cancelled", "tom", schedule=event["schedule"])
        assert refused(w, fired["child"], "log.added", w.agent("tom", fired["grant"]), text="finish child") is None


def test_new_child_rejects_tampered_title_and_via():
    import pytest

    from orch import schema

    w, uid, event, due = setup_new()
    w.clock = due
    fired, _ = fire_new(w, uid, event, due)
    assert hasattr(create_child(w, uid, event, fired, title="Agent text")[1], "code")
    good, _ = create_child(w, uid, event, fired)
    for key, value in (
        ("set_sig_hash", digest("wrong")),
        ("occurrence", stamp(due + timedelta(days=1))),
        ("extra", "no"),
    ):
        bad = {**good, "via": {**good["via"], key: value}}
        if key == "extra":
            with pytest.raises(schema.SchemaError):
                schema.validate("event.ticket.created", bad, log="ticket")
        else:
            from orch.model.schedules_wire import child_creation

            assert child_creation(w.state()._core, fired["child"], bad) is not None


def test_new_child_date_is_in_the_signed_rule_timezone():
    from types import SimpleNamespace

    from orch.model.schedules import child_title

    rec = SimpleNamespace(title="Mail {literal}", rule={"tz": "Pacific/Kiritimati"})
    assert child_title(rec, "2026-10-09T23:30:00Z") == "Mail {literal} 2026-10-10"


def test_reopened_previous_child_blocks_creation_after_fired():
    w, uid, event, due = setup_new()
    w.clock = due
    first, _ = fire_new(w, uid, event, due)
    create_child(w, uid, event, first)
    w.tev(first["child"], "ticket.closed", "tom", resolution="other")
    w.tev(uid, "schedule.outcome", w.HOST, schedule=event["schedule"], occurrence=stamp(due), outcome="ran")
    w.clock = due + timedelta(days=1)
    second, result = fire_new(w, uid, event, w.clock)
    assert not hasattr(result, "code"), result
    w.tev(first["child"], "ticket.reopened", "tom")
    assert create_child(w, uid, event, second, key="DEMO-0003")[1].detail == "skipped_open_child"
    assert second["child"] not in w.state()._core.tickets

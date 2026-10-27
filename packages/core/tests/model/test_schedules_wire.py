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


def setup():
    w = World().bootstrap({"tom": "member"})
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
    w.clock = due - timedelta(minutes=1)
    w.claim(uid, "tom")
    w.clock = due
    assert not hasattr(fire(w, uid, e, due)[1], "code")
    assert w.view(uid).claim is None

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

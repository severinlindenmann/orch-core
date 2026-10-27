"""Schedule authority is checked identically at append and replay."""

from datetime import timedelta

from orch import canon
from orch.model import advance
from orch.model.types import ts
from tests.model.world import World, stamp


def setup():
    w = World().bootstrap({"tom": "member"})
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
    from tests.model.world import refused

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

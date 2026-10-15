"""Claims, takeovers, leases (T7, A4, §5.2, §5.9)."""

from datetime import timedelta

import pytest

from orch.model import Code
from tests.model.world import World, refused, stamp, ulid


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member"})


def test_one_claim_per_ticket_a_takeover_is_logged(w):
    uid = w.ticket()
    a = w.claim(uid, "sev", "s_" + ulid(1))
    b = w.agent("mara", w.grant("mara"), "s_" + ulid(2))
    assert refused(w, uid, "claim.taken", b) == Code.CLAIM_EXISTS
    assert (
        refused(w, uid, "claim.taken", b, takeover={"from_session": "s_" + ulid(9), "reason": "x"})
        == Code.CLAIM_NOT_LIVE
    )
    w.tev(uid, "claim.taken", b, takeover={"from_session": a["session"], "reason": "stuck"})
    v = w.view(uid)
    assert v.claim.session == b["session"] and v.claim.for_person == w.people["mara"]
    assert v.takeovers[0]["from_session"] == a["session"] and v.takeovers[0]["reason"] == "stuck"
    assert v.status == "in_progress"


def test_takeover_of_nothing_is_refused(w):
    uid = w.ticket()
    b = w.agent("mara", w.grant("mara"))
    assert (
        refused(w, uid, "claim.taken", b, takeover={"from_session": b["session"], "reason": "x"}) == Code.CLAIM_NOT_LIVE
    )


def test_claim_lapses_by_inactivity_and_the_host_records_it(w):
    uid = w.ticket()
    a = w.claim(uid)
    w.clock += timedelta(minutes=121)
    now = stamp(w.clock)
    assert w.view(uid, now).claim.lapsed == "expired" and not w.view(uid, now).claim.live
    assert (
        refused(w, uid, "claim.released", w.HOST, session=a["session"], reason="grant_ended", at=now)
        == Code.CLAIM_NOT_LIVE
    )
    w.tev(uid, "claim.released", w.HOST, session=a["session"], reason="expired", at=now)
    assert w.view(uid).claim is None and w.view(uid).status == "open"


def test_activity_keeps_the_claim_alive(w):
    uid = w.ticket()
    a = w.claim(uid)
    w.clock += timedelta(minutes=100)
    w.tev(uid, "log.added", a, text="still here", at=stamp(w.clock))
    assert w.state(stamp(w.clock + timedelta(minutes=100))).tickets[uid].claim.live  # 100 idle minutes
    assert not w.state(stamp(w.clock + timedelta(minutes=130))).tickets[uid].claim.live


def test_claim_ends_with_the_grant_and_with_the_member(w):
    uid = w.ticket()
    g = w.grant("tom", "workable", hours=1)
    a = w.claim(uid, "tom", gid=g)
    w.wev("grant.revoked", "tom", grant=g)
    assert w.view(uid).claim.lapsed == "grant_ended"
    w.tev(uid, "claim.released", w.HOST, session=a["session"], reason="grant_ended")
    uid2 = w.ticket()
    a2 = w.claim(uid2, "tom", gid=w.grant("tom", "workable"))
    w.wev("member.removed", "sev", person=w.people["tom"])
    assert w.view(uid2).claim.lapsed == "member_removed"
    w.tev(uid2, "claim.released", w.HOST, session=a2["session"], reason="member_removed")
    assert not w.state().workspace.invalid


def test_release_rules(w):
    uid = w.ticket()
    a = w.claim(uid, "sev", "s_" + ulid(1))
    sess = a["session"]
    other = w.agent("mara", w.grant("mara"), "s_" + ulid(2))
    assert (
        refused(w, uid, "claim.released", other, session=sess, reason="released") is not None
    )  # schema: own session only
    assert refused(w, uid, "claim.released", "tom", session=sess, reason="released") == Code.ROLE_DENIED
    assert refused(w, uid, "claim.released", "mara", session=sess, reason="released") is None
    assert refused(w, uid, "claim.released", "mara", session="s_" + ulid(5), reason="released") == Code.CLAIM_NOT_LIVE
    assert refused(w, uid, "claim.released", w.HOST, session=sess, reason="expired") == Code.CLAIM_NOT_LIVE


def test_subagents_act_under_the_parents_claim_and_other_sessions_do_not(w):
    uid = w.ticket()
    w.edit(uid, "sev", {"ticket.tasks": [{"id": "T1", "text": "a", "verify": None, "proves": []}]})
    g = w.grant("sev")
    parent = w.agent("sev", g, "s_" + ulid(1))
    w.tev(uid, "claim.taken", parent)
    sub = w.agent("sev", g, "s_" + ulid(1) + ".1")
    stranger = w.agent("sev", g, "s_" + ulid(3))
    assert refused(w, uid, "task.started", sub, task="T1") is None
    assert refused(w, uid, "task.started", stranger, task="T1") == Code.CLAIM_NOT_HOLDER
    w.tev(uid, "task.started", sub, task="T1")
    assert w.view(uid).tasks[0].leased_by == sub["session"]
    # a second session on the same task is refused (A4); the manager is a different session than the subagent
    sub2 = w.agent("sev", g, "s_" + ulid(1) + ".2")
    assert refused(w, uid, "task.started", sub2, task="T1") == Code.TASK_STATE
    assert refused(w, uid, "task.done", sub2, task="T1") == Code.TASK_LEASED


def test_lease_lapses_after_lease_ttl_min(w):
    uid = w.ticket()
    w.edit(uid, "sev", {"ticket.tasks": [{"id": "T1", "text": "a", "verify": None, "proves": []}]})
    g = w.grant("sev")
    p = w.agent("sev", g, "s_" + ulid(1))
    w.tev(uid, "claim.taken", p)
    sub1, sub2 = w.agent("sev", g, p["session"] + ".1"), w.agent("sev", g, p["session"] + ".2")
    w.tev(uid, "task.started", sub1, task="T1")
    w.clock += timedelta(minutes=61)
    w.tev(uid, "log.added", p, text="alive", at=stamp(w.clock))
    assert w.view(uid, stamp(w.clock)).tasks[0].leased_by is None
    assert refused(w, uid, "task.done", sub2, task="T1", at=stamp(w.clock)) is None


def test_releasing_the_claim_frees_leases(w):
    uid = w.ticket()
    w.edit(uid, "sev", {"ticket.tasks": [{"id": "T1", "text": "a", "verify": None, "proves": []}]})
    a = w.claim(uid)
    w.tev(uid, "task.started", a, task="T1")
    w.tev(uid, "claim.released", a, session=a["session"], reason="handoff")
    assert w.view(uid).tasks[0].state == "open" and w.view(uid).tasks[0].leased_by is None

"""Needs: what waits for whom; "waiting" is derived, not a status (§5.9)."""

import pytest

from tests.model.world import World


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member"})


def kinds(w, uid):
    return [(n.kind, n.ref, n.who) for n in w.view(uid).needs]


def test_complete_requirements_wait_for_the_owner(w):
    uid = w.ticket()
    assert kinds(w, uid) == []
    w.fill(uid)
    assert ("approve", "requirements", (w.people["sev"],)) in kinds(w, uid)
    assert w.view(uid).waiting and w.view(uid).status == "open"
    w.decide(uid, "sev", "requirements")
    ks = kinds(w, uid)
    assert ("approve", "plan", (w.people["sev"],)) in ks and not any(k[1] == "requirements" for k in ks)


def test_plan_waits_for_requirements_and_verify_for_testing(w):
    uid = w.ticket()
    w.fill(uid)
    assert not any(k[1] == "plan" for k in kinds(w, uid))
    uid2 = w.ticket()
    w.to_testing(uid2)
    w.tev(uid2, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    assert ("approve", "verify", (w.people["mara"],)) in kinds(w, uid2)
    w.decide(uid2, "mara", "verify")
    assert kinds(w, uid2) == [] and not w.view(uid2).waiting


def test_open_blocking_question_makes_the_ticket_wait_for_its_addressee(w):
    from orch import canon

    uid = w.ticket()
    q = {"id": "Q1", "to": w.people["tom"], "text": "which?", "blocking": True}
    qid = canon.question_id(w.workspace_id, uid, "Q1")
    w.tev(uid, "question.asked", "sev", question=q, qid=qid, hash=canon.question_hash(qid, uid, "which?", None))
    assert kinds(w, uid) == [("question", "Q1", (w.people["tom"],))] and w.view(uid).waiting
    assert not w.state().tickets[uid].status == "waiting"


def test_non_blocking_question_does_not_make_the_ticket_wait(w):
    from orch import canon

    uid = w.ticket()
    q = {"id": "Q1", "to": w.people["tom"], "text": "fyi?", "blocking": False}
    qid = canon.question_id(w.workspace_id, uid, "Q1")
    w.tev(uid, "question.asked", "sev", question=q, qid=qid, hash=canon.question_hash(qid, uid, "fyi?", None))
    assert not w.view(uid).waiting and len(w.view(uid).needs) == 1


def test_done_tickets_need_nothing(w):
    uid = w.ticket()
    w.to_testing(uid)
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    w.decide(uid, "mara", "verify")
    assert w.view(uid).needs == ()

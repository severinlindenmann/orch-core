"""Questions (T6): qid and hash derivation, first valid answer wins, who may answer."""

import pytest

from orch import canon
from orch.model import Code
from tests.model.world import World, refused


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member", "ned": "member", "vera": "viewer"})


def ask(w, uid, actor, q, **over):
    qid = canon.question_id(w.workspace_id, uid, q["id"])
    h = canon.question_hash(qid, uid, q["text"], q.get("options"))
    return w.try_(uid, "question.asked", actor, question=q, qid=over.get("qid", qid), hash=over.get("hash", h))


def Q(w, to, qid="Q1", text="Which?", blocking=True, options=None):
    q = {"id": qid, "to": to, "text": text, "blocking": blocking}
    if options:
        q["options"] = options
        q["recommended"] = options[0]["key"]
    return q


def test_asked_and_answered_first_valid_answer_wins(w):
    uid = w.ticket()
    q = Q(w, w.people["tom"], options=[{"key": "csv", "label": "CSV"}, {"key": "api", "label": "API"}])
    ask(w, uid, "sev", q)
    v = w.view(uid)
    assert not v.questions[0].answered and v.waiting and v.needs[0].who == (w.people["tom"],)
    h = v.questions[0].hash
    assert (
        w.try_(uid, "question.answered", "tom", question="Q1", hash=h, option="nope")[1].code == Code.ANSWER_BAD_OPTION
    )
    w.tev(uid, "question.answered", "tom", question="Q1", hash=h, option="csv")
    assert w.view(uid).questions[0].answer["option"] == "csv" and not w.view(uid).waiting
    assert (
        w.try_(uid, "question.answered", "mara", question="Q1", hash=h, option="api")[1].code == Code.QUESTION_ANSWERED
    )


def test_qid_and_hash_must_be_derived(w):
    uid = w.ticket()
    q = Q(w, w.people["tom"])
    assert ask(w, uid, "sev", q, qid="0" * 32)[1].code == Code.QUESTION_BAD_ID
    assert ask(w, uid, "sev", q, hash="sha256:" + "0" * 64)[1].code == Code.QUESTION_BAD_HASH


def test_who_may_answer(w):
    uid = w.ticket()
    ask(w, uid, "sev", Q(w, w.people["tom"]))
    h = w.view(uid).questions[0].hash
    assert refused(w, uid, "question.answered", "ned", question="Q1", hash=h, text="x") == Code.ANSWER_NOT_ALLOWED
    assert refused(w, uid, "question.answered", "mara", question="Q1", hash=h, text="x") is None  # maintainers always
    assert refused(w, uid, "question.answered", "vera", question="Q1", hash=h, text="x") == Code.ANSWER_NOT_ALLOWED


def test_role_addressee_and_orphan_questions(w):
    uid = w.ticket()
    ask(w, uid, "sev", Q(w, "reviewers"))
    h = w.view(uid).questions[0].hash
    assert w.view(uid).questions[0].addressees == tuple(
        sorted([w.people["mara"], w.people["sev"]])
    )  # nobody is a reviewer yet
    assert refused(w, uid, "question.answered", "tom", question="Q1", hash=h, text="x") == Code.ANSWER_NOT_ALLOWED
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["tom"]], remove=[])
    assert refused(w, uid, "question.answered", "tom", question="Q1", hash=h, text="x") is None
    assert w.view(uid).questions[0].addressees == (w.people["tom"],)


def test_reask_changes_the_hash_and_a_stale_answer_is_refused(w):
    uid = w.ticket()
    ask(w, uid, "sev", Q(w, w.people["tom"]))
    old = w.view(uid).questions[0].hash
    ask(w, uid, "sev", Q(w, w.people["tom"], text="Which one, really?"))
    assert w.view(uid).questions[0].hash != old
    assert refused(w, uid, "question.answered", "tom", question="Q1", hash=old, text="x") == Code.QUESTION_STALE
    assert refused(w, uid, "question.answered", "tom", question="Q9", hash=old, text="x") == Code.QUESTION_UNKNOWN


def test_unattended_asks_only_new_ids_and_not_reasks(w):
    uid = w.ticket()
    u = w.unattended()
    assert ask(w, uid, u, Q(w, w.people["tom"]))[1].__class__.__name__ == "Ok"
    assert ask(w, uid, u, Q(w, w.people["tom"], text="again"))[1].code == Code.QUESTION_REASK
    assert ask(w, uid, u, Q(w, w.people["tom"], qid="Q2"))[1].__class__.__name__ == "Ok"


def test_questions_are_not_gate_bound(w):
    uid = w.ticket()
    w.fill(uid)
    h = w.view(uid).gates["requirements"].hash
    ask(w, uid, "sev", Q(w, w.people["tom"]))
    assert w.view(uid).gates["requirements"].hash == h

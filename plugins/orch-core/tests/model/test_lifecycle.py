"""§5.9: every row of the status table, and that events outside the table leave the status alone."""

import pytest

from orch.model import Code
from tests.model.world import SHA2, World, pol, refused


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member"})


def status(w, uid):
    return w.view(uid).status


def test_created_is_open(w):
    assert status(w, w.ticket()) == "open"


def test_status_changed_between_open_and_backlog(w):
    uid = w.ticket()
    w.tev(uid, "status.changed", "sev", **{"from": "open", "to": "backlog"})
    assert status(w, uid) == "backlog"
    w.tev(uid, "status.changed", "sev", **{"from": "backlog", "to": "open"})
    assert status(w, uid) == "open"
    assert refused(w, uid, "status.changed", "sev", **{"from": "backlog", "to": "open"}) == Code.STATUS_TRANSITION


def test_status_changed_refused_while_claimed(w):
    uid = w.ticket()
    w.claim(uid)
    assert refused(w, uid, "status.changed", "sev", **{"from": "in_progress", "to": "open"}) == Code.STATUS_TRANSITION


def test_claim_taken_from_open_and_backlog(w):
    for first in ("open", "backlog"):
        uid = w.ticket()
        if first == "backlog":
            w.tev(uid, "status.changed", "sev", **{"from": "open", "to": "backlog"})
        w.claim(uid)
        assert status(w, uid) == "in_progress"


def test_claim_taken_in_testing_and_done_refused(w):
    uid = w.ticket()
    w.to_testing(uid)
    g = w.grant("sev")
    other = w.agent("sev", g, "s_01J9ZK0000000000000000ZZZZ")
    assert refused(w, uid, "claim.taken", other) == Code.STATUS_TRANSITION


def test_claim_released_in_progress_to_open_other_states_unchanged(w):
    uid = w.ticket()
    a = w.claim(uid)
    w.tev(uid, "claim.released", a, session=a["session"], reason="released")
    assert status(w, uid) == "open"
    uid2 = w.ticket()
    a2 = w.to_testing(uid2)
    w.tev(uid2, "claim.released", a2, session=a2["session"], reason="handoff")
    assert status(w, uid2) == "testing"


def test_submit_needs_in_progress(w):
    uid = w.ticket()
    assert refused(w, uid, "ticket.submitted", "sev") == Code.STATUS_TRANSITION


def test_submit_needs_gates_and_evidence(w):
    uid = w.ticket()
    w.fill(uid)
    a = w.claim(uid)
    e = w.try_(uid, "ticket.submitted", a)[1]
    assert e.code == Code.SUBMIT_INCOMPLETE
    assert (
        "requirements not approved" in e.detail
        and "plan not approved" in e.detail
        and "AC1 has no evidence" in e.detail
    )
    w.decide(uid, "sev", "requirements")
    w.decide(uid, "sev", "plan")
    w.tev(
        uid, "task.done", a, task="T1", receipt={"cmd": "make test", "exit": 0, "ms": 1, "repo": None, "commit": None}
    )
    w.tev(uid, "ticket.submitted", a)
    assert status(w, uid) == "testing"


def test_requirements_and_plan_approvals_leave_status_in_any_state_but_done_closed(w):
    for first in ("backlog", "open"):
        uid = w.ticket()
        w.fill(uid)
        if first == "backlog":
            w.tev(uid, "status.changed", "sev", **{"from": "open", "to": "backlog"})
        w.decide(uid, "sev", "requirements")
        assert status(w, uid) == first
    uid = w.ticket()
    w.fill(uid)
    w.claim(uid)
    w.decide(uid, "sev", "requirements")
    assert status(w, uid) == "in_progress"
    done = w.ticket()
    w.to_testing(done)
    w.tev(done, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    w.decide(done, "mara", "verify")
    assert status(w, done) == "done"
    assert (
        w.try_(
            done,
            "gate.approved",
            "sev",
            gate="requirements",
            gate_gen=0,
            hash=w.view(done).gates["requirements"].hash,
            policy_hash=w.view(done).gates["requirements"].policy_hash,
        )[1].code
        == Code.GATE_STATUS
    )


def test_verdicts_and_code_decisions_only_in_testing(w):
    uid = w.ticket()
    w.fill(uid)
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    e, r = w.decide(uid, "mara", "verify", try_=True)
    assert r.code == Code.GATE_STATUS


def test_non_completing_pass_leaves_testing_completing_pass_makes_done(w):
    uid = w.ticket()
    w.to_testing(uid)
    w.wev("policy.changed", "sev", gates={"verify": pol(["owner", "maintainer"], 2)})
    w.decide(uid, "sev", "verify")
    assert status(w, uid) == "testing"
    w.decide(uid, "mara", "verify")
    assert status(w, uid) == "done"


def test_code_approval_completing_the_done_rule(w):
    w.wev(
        "policy.changed",
        "sev",
        gates={
            "code": pol(["maintainer", "owner"], 1, ["assignees"], "all", True),
            "verify": pol(["maintainer", "owner"]),
        },
    )
    uid = w.ticket()
    w.to_testing(uid)
    w.decide(uid, "sev", "verify")
    assert status(w, uid) == "testing"  # code applies and has no approval yet
    w.decide(uid, "mara", "code")
    assert status(w, uid) == "done"
    assert not w.state().workspace.invalid


def test_fail_verdict_and_code_changes_requested_go_back_to_in_progress(w):
    uid = w.ticket()
    w.to_testing(uid)
    w.tev(uid, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    w.decide(uid, "mara", "verify", kind="fail")
    assert status(w, uid) == "in_progress"
    w.wev("policy.changed", "sev", gates={"code": pol(["maintainer", "owner"], 1, ["assignees"], "all", True)})
    uid2 = w.ticket()
    w.to_testing(uid2)
    w.decide(uid2, "mara", "code", kind="changes")
    assert status(w, uid2) == "in_progress"


def test_changes_requested_on_requirements_or_plan(w):
    open_ = w.ticket()
    w.fill(open_)
    w.decide(open_, "sev", "requirements", kind="changes")
    assert status(w, open_) == "open"
    testing = w.ticket()
    w.to_testing(testing)
    w.decide(testing, "sev", "plan", kind="changes")
    assert status(w, testing) == "in_progress"
    prog = w.ticket()
    w.fill(prog)
    w.claim(prog)
    w.decide(prog, "sev", "requirements", kind="changes")
    assert status(w, prog) == "in_progress"


def test_branch_pushed_done_to_testing_other_states_unchanged(w):
    uid = w.ticket()
    w.to_testing(uid)
    w.push(uid, SHA2)
    assert status(w, uid) == "testing"
    uid2 = w.ticket()
    w.fill(uid2)
    w.with_repo(uid2)
    w.push(uid2, SHA2)
    assert status(w, uid2) == "open"
    w.tev(uid2, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    done = w.ticket()
    w.to_testing(done)
    w.tev(done, "people.changed", "sev", role="reviewers", add=[w.people["mara"]], remove=[])
    w.decide(done, "mara", "verify")
    assert status(w, done) == "done"
    w.push(done, SHA2)
    assert status(w, done) == "testing"


def test_closed_from_any_but_closed_and_reopen_from_done_or_closed(w):
    uid = w.ticket()
    w.tev(uid, "ticket.closed", "sev", resolution="wont_do")
    assert status(w, uid) == "closed"
    assert refused(w, uid, "ticket.closed", "sev", resolution="obsolete") == Code.STATUS_TRANSITION
    w.tev(uid, "ticket.reopened", "sev")
    assert status(w, uid) == "open"
    assert refused(w, uid, "ticket.reopened", "sev") == Code.STATUS_TRANSITION
    a = w.ticket()
    w.claim(a)
    w.tev(a, "ticket.closed", "mara", resolution="other")
    assert status(w, a) == "closed"


def test_events_outside_the_table_do_not_change_status(w):
    uid = w.ticket()
    a = w.claim(uid)
    for typ, payload in (("log.added", {"text": "hi"}), ("handoff.written", {"text": "state"})):
        w.tev(uid, typ, a, **payload)
        assert status(w, uid) == "in_progress"
    w.tev(uid, "people.changed", "sev", role="watchers", add=[w.people["tom"]], remove=[])
    w.tev(uid, "visibility.changed", "sev", visibility="workspace")
    assert status(w, uid) == "in_progress"


def test_closing_ends_the_claim_and_host_records_it(w):
    uid = w.ticket()
    a = w.claim(uid)
    w.tev(uid, "ticket.closed", "sev", resolution="other")
    assert not w.view(uid).claim.live
    assert w.view(uid).claim.lapsed == "ticket_closed"
    w.tev(uid, "claim.released", w.HOST, session=a["session"], reason="ticket_closed")
    assert w.view(uid).claim is None
    assert w.view(uid).status == "closed"
    assert not w.state().workspace.invalid


def test_closed_ticket_refuses_claims_and_task_events(w):
    uid = w.ticket()
    w.fill(uid)
    w.tev(uid, "ticket.closed", "sev", resolution="other")
    g = w.grant("sev")
    assert refused(w, uid, "claim.taken", w.agent("sev", g)) == Code.TICKET_FROZEN

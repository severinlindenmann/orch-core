import pytest

from orch.core.constants import STATUSES
from orch.core.events import Actor
from orch.core.gates import record_approval
from orch.core.lifecycle import check_move, unanswered_blocking
from orch.core.model import new_ticket
from orch.errors import HumanOnlyError, TransitionError, ValidationError

AGENT = Actor("agent", "claude-code", "cli", "s1")
HUMAN = Actor("human", "you", "tty")
SKIP = ("xs",)
PAIRS = [(a, b) for a in STATUSES for b in STATUSES if a != b]
AGENT_MOVES = {("in-progress", "waiting"), ("in-progress", "testing")}
HUMAN_MOVES = {
    ("backlog", "open"), ("open", "in-progress"), ("in-progress", "waiting"),
    ("waiting", "in-progress"), ("in-progress", "testing"),
} | {(s, "backlog") for s in STATUSES if s != "backlog"}


def ready(ws, frm, to):
    """A ticket in `frm` that satisfies every precondition for `to`."""
    t = new_ticket("L-0001", "x", type="feature", priority="normal", size="m", created="2026-09-30T08:00Z")
    t.meta["status"] = frm
    for name, text in (("Requirements", "r"), ("Acceptance criteria", "- [ ] a"), ("Plan", "p"), ("Verification", "ok"),
                       ("Tasks", "- [x] T1 the work")):
        t.set_section(name, text)
    record_approval(ws, t, "requirements", HUMAN)
    record_approval(ws, t, "plan", HUMAN)
    if to == "waiting":
        t.meta["questions"] = [{"id": "Q1", "blocking": True, "answer": None}]
    return t


@pytest.mark.parametrize("frm,to", PAIRS)
def test_matrix_agent(ws, frm, to):
    t = ready(ws, frm, to)
    if (frm, to) in AGENT_MOVES:
        check_move(t, to, AGENT, plan_skip_sizes=SKIP)
    else:
        with pytest.raises(TransitionError):
            check_move(t, to, AGENT, plan_skip_sizes=SKIP)


@pytest.mark.parametrize("frm,to", PAIRS)
def test_matrix_human(ws, frm, to):
    t = ready(ws, frm, to)
    if (frm, to) in HUMAN_MOVES:
        check_move(t, to, HUMAN, plan_skip_sizes=SKIP)
    else:
        with pytest.raises(TransitionError):
            check_move(t, to, HUMAN, plan_skip_sizes=SKIP)


def test_claim_auto_and_verdict_commands(ws):
    check_move(ready(ws, "open", "in-progress"), "in-progress", AGENT, plan_skip_sizes=SKIP, command="claim")
    check_move(ready(ws, "waiting", "in-progress"), "in-progress", AGENT, plan_skip_sizes=SKIP, command="auto")
    check_move(ready(ws, "testing", "done"), "done", HUMAN, plan_skip_sizes=SKIP, command="verdict")
    check_move(ready(ws, "testing", "in-progress"), "in-progress", HUMAN, plan_skip_sizes=SKIP, command="verdict")
    with pytest.raises(HumanOnlyError):
        check_move(ready(ws, "testing", "done"), "done", AGENT, plan_skip_sizes=SKIP, command="verdict")


def test_testing_preconditions(ws):
    t = ready(ws, "in-progress", "testing")
    t.set_section("Verification", "")
    with pytest.raises(ValidationError, match="Verification"):
        check_move(t, "testing", AGENT, plan_skip_sizes=SKIP)

    t = ready(ws, "in-progress", "testing")
    t.set_section("Plan", "changed after approval")
    with pytest.raises(ValidationError, match="plan"):
        check_move(t, "testing", AGENT, plan_skip_sizes=SKIP)
    t.meta["size"] = "xs"
    check_move(t, "testing", AGENT, plan_skip_sizes=SKIP)  # xs skips the plan gate

    t = ready(ws, "in-progress", "testing")
    t.meta["questions"] = [{"id": "Q1", "blocking": True, "answer": None}]
    with pytest.raises(ValidationError, match="blocking"):
        check_move(t, "testing", AGENT, plan_skip_sizes=SKIP)


def test_open_needs_valid_requirements_gate(ws):
    t = ready(ws, "backlog", "open")
    t.set_section("Requirements", "changed")
    with pytest.raises(ValidationError, match="requirements"):
        check_move(t, "open", HUMAN, plan_skip_sizes=SKIP)


def test_blockers(ws):
    with pytest.raises(ValidationError, match="L-0009"):
        check_move(ready(ws, "open", "in-progress"), "in-progress", HUMAN, plan_skip_sizes=SKIP, open_blockers=["L-0009"])


def test_waiting_needs_question(ws):
    with pytest.raises(ValidationError):
        check_move(ready(ws, "in-progress", "testing"), "waiting", AGENT, plan_skip_sizes=SKIP)


def test_unanswered_blocking():
    t = new_ticket("L-1", "x", type="feature", priority="normal", size="m", created="t")
    t.meta["questions"] = [
        {"id": "Q1", "blocking": True, "answer": None},
        {"id": "Q2", "blocking": False, "answer": None},
        {"id": "Q3", "blocking": True, "answer": "A"},
    ]
    assert [q["id"] for q in unanswered_blocking(t)] == ["Q1"]


def test_unanswered_blocking_ignores_non_dict_entries():
    t = new_ticket("L-1", "x", type="feature", priority="normal", size="m", created="t")
    t.meta["questions"] = [None, "oops", {"id": "Q1", "blocking": True, "answer": None}]
    assert [q["id"] for q in unanswered_blocking(t)] == ["Q1"]

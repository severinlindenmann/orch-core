import pytest

from orch.core import tasks as tk
from orch.core.events import Actor, read_events
from orch.core.gates import record_approval
from orch.core.lifecycle import allowed_targets, check_move
from orch.core.model import new_ticket
from orch.errors import ValidationError

AGENT = Actor("agent", "claude-code", "cli", "s1")
HUMAN = Actor("human", "you", "tty")
SKIP = ("xs",)


def _ticket(ws, size="m", tasks=""):
    t = new_ticket("L-0001", "x", type="feature", priority="normal", size=size, created="2026-10-02T08:00Z")
    t.meta["status"] = "in-progress"
    for name, text in (("Requirements", "r"), ("Acceptance criteria", "- [ ] a"), ("Plan", "p"),
                       ("Verification", "ok"), ("Tasks", tasks)):
        t.set_section(name, text)
    record_approval(ws, t, "requirements", HUMAN)
    record_approval(ws, t, "plan", HUMAN)
    return t


@pytest.mark.parametrize("size", ["xs", "m"])
def test_every_size_needs_a_task(ws, size):
    with pytest.raises(ValidationError, match="no tasks"):
        check_move(_ticket(ws, size), "testing", AGENT, plan_skip_sizes=SKIP)


def test_open_tasks_block_testing(ws):
    t = _ticket(ws, tasks="- [x] T1 a\n- [/] T2 b\n- [!] T3 c\n  - why: w\n- [ ] T4 d")
    with pytest.raises(ValidationError, match="open tasks: T2, T3, T4"):
        check_move(t, "testing", AGENT, plan_skip_sizes=SKIP)


def test_done_and_skipped_tasks_allow_testing(ws):
    check_move(_ticket(ws, tasks="- [x] T1 a\n- [-] T2 b\n  - why: not needed"), "testing", AGENT, plan_skip_sizes=SKIP)


def test_broken_tasks_section_is_a_validation_error(ws):
    with pytest.raises(ValidationError, match="Tasks line 1"):
        check_move(_ticket(ws, tasks="nonsense"), "testing", AGENT, plan_skip_sizes=SKIP)
    assert allowed_targets(_ticket(ws, tasks="nonsense"), HUMAN, plan_skip_sizes=SKIP) == ["backlog"]


def test_blocking_question_blocks_the_doing_task_and_the_answer_restarts_it(ws, aops, hops, working, plan_approved):
    aops.task_add(working, [{"text": "a"}, {"text": "b"}])
    plan_approved(working)
    aops.task_start(working, "T1")
    t, _ = aops.ask(working, [{"text": "Which schedule?", "options": ["2am", "4am"]}])
    assert t.status == "waiting"
    t1 = tk.find(tk.ticket_tasks(t), "T1")
    assert (t1.state, t1.on, t1.why) == ("blocked", "Q1", "waits for the answer to Q1")
    assert [e for e in read_events(ws, working) if e.kind == "question.asked"][-1].data["task_blocked"] == "T1"
    assert "T1 blocked on Q1" in t.section("Log")
    t = hops.answer(working, "Q1", "A")
    t1 = tk.find(tk.ticket_tasks(t), "T1")
    assert t.status == "in-progress" and (t1.state, t1.on, t1.why) == ("doing", None, None)
    assert [e for e in read_events(ws, working) if e.kind == "question.answered"][-1].data["task_restarted"] == "T1"


def test_restart_waits_for_every_blocking_answer(aops, hops, working, plan_approved):
    aops.task_add(working, [{"text": "a"}])
    plan_approved(working)
    aops.task_start(working, "T1")
    aops.ask(working, [{"text": "One?", "type": "confirm"}, {"text": "Two?", "type": "confirm"}])
    t = hops.answer(working, "Q1", "yes")
    assert tk.ticket_tasks(t)[0].state == "blocked"
    t = hops.answer(working, "Q2", "no")
    assert tk.ticket_tasks(t)[0].state == "doing"


def test_questions_without_a_doing_task_leave_tasks_alone(ws, aops, working):
    aops.task_add(working, [{"text": "a"}])
    t, _ = aops.ask(working, [{"text": "FYI?", "type": "confirm", "blocking": False}])
    assert tk.ticket_tasks(t)[0].state == "todo"
    t, _ = aops.ask(working, [{"text": "Block?", "type": "confirm"}])
    assert tk.ticket_tasks(t)[0].state == "todo"
    assert "task_blocked" not in [e for e in read_events(ws, working) if e.kind == "question.asked"][-1].data


def test_two_tasks_on_one_question_restart_one(aops, hops, working, plan_approved):
    aops.task_add(working, [{"text": "a"}, {"text": "b"}])
    plan_approved(working)
    aops.task_start(working, "T1")
    aops.ask(working, [{"text": "Which?", "type": "confirm"}])
    aops.task_block(working, "T2", "same question", on="Q1")
    t = hops.answer(working, "Q1", "yes")
    assert [(x.id, x.state) for x in tk.ticket_tasks(t)] == [("T1", "doing"), ("T2", "todo")]


def test_broken_tasks_do_not_stop_a_question(aops, hops, put):
    tid = put("in-progress", sections={"Tasks": "nonsense"})
    t, _ = aops.ask(tid, [{"text": "Still?", "type": "confirm"}])
    assert t.status == "waiting" and t.section("Tasks") == "nonsense"
    assert hops.answer(tid, "Q1", "yes").status == "in-progress"

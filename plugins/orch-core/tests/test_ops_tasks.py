import pytest

from orch.core import store
from orch.core import tasks as tk
from orch.core.check import _latest_status_change
from orch.core.events import read_events
from orch.core.gates import gate_state
from orch.core.ops import Ops
from orch.errors import ClaimError, HumanOnlyError, NotFoundError, TransitionError, UsageError, ValidationError


def tasks_of(ws, tid):
    return tk.ticket_tasks(store.load(ws, tid)[1])


def task_events(ws, tid, kind):
    return [e.data for e in read_events(ws, tid) if e.kind == kind]


def test_add_one_and_many(ws, aops, working):
    _, ids = aops.task_add(working, [{"text": "Inventory jobs", "refs": ["file:hub/jobs/"]}])
    assert ids == ["T1"]
    _, ids = aops.task_add(working, [{"text": "Migrate", "needs": ["T1", "T3"]}, {"text": "Check"}])
    assert ids == ["T2", "T3"]  # needs may name a task of the same batch
    assert [x.id for x in tasks_of(ws, working)] == ["T1", "T2", "T3"]
    assert task_events(ws, working, "task.added")[-1] == {"tasks": ["T2", "T3"], "after_approval": False}
    assert "added T2, T3" in store.load(ws, working)[1].section("Log")


def test_add_after_positions_and_ids_are_never_reused(ws, aops, hops, working):
    aops.task_add(working, [{"text": "a"}, {"text": "b"}])
    aops.task_add(working, [{"text": "between"}], after="T1")
    assert [(x.id, x.text) for x in tasks_of(ws, working)] == [("T1", "a"), ("T3", "between"), ("T2", "b")]
    path = store.resolve(ws, working).path
    hops.replace_raw(working, path.read_text(encoding="utf-8").replace("- [ ] T3 between\n", ""))
    _, ids = aops.task_add(working, [{"text": "c"}])
    assert ids == ["T4"]


def test_added_after_plan_approval_is_marked_and_keeps_the_gate(ws, aops, hops, working):
    aops.task_add(working, [{"text": "a"}])
    aops.set_section(working, "Plan", "Do it in one go.")
    hops.approve(working, "plan")
    _, ids = aops.task_add(working, [{"text": "late"}])
    items = tasks_of(ws, working)
    assert tk.find(items, ids[0]).added_after_approval and tk.find(items, "T1").added is None
    assert gate_state(store.load(ws, working)[1], "plan") == "approved"
    assert task_events(ws, working, "task.added")[-1]["after_approval"] is True


def test_add_rejects_unknown_needs_and_cycles(aops, working):
    with pytest.raises(ValidationError, match="unknown"):
        aops.task_add(working, [{"text": "a", "needs": ["T9"]}])
    with pytest.raises(ValidationError, match="cycle"):
        aops.task_add(working, [{"text": "a", "needs": ["T2"]}, {"text": "b", "needs": ["T1"]}])
    with pytest.raises(NotFoundError):
        aops.task_add(working, [{"text": "a"}], after="T7")


def test_state_machine_and_event_shape(ws, aops, working, plan_approved):
    aops.task_add(working, [{"text": "a", "verify": "pytest -q"}, {"text": "b"}, {"text": "c", "needs": ["T1"]}])
    plan_approved(working)
    aops.task_start(working, "T1")
    with pytest.raises(ValidationError, match="T1 is in progress"):
        aops.task_start(working, "T2")
    with pytest.raises(UsageError, match="verify"):
        aops.task_done(working, "T1")
    aops.task_done(working, "T1", "12 passed")
    aops.task_skip(working, "T2", "not needed after T1")
    with pytest.raises(UsageError):
        aops.task_skip(working, "T3", " ")
    aops.task_block(working, "T3", "waits for access", on="T2")
    aops.task_reopen(working, "T2")
    items = tasks_of(ws, working)
    assert [(x.id, x.state) for x in items] == [("T1", "done"), ("T2", "todo"), ("T3", "blocked")]
    assert tk.find(items, "T1").note == "12 passed" and tk.find(items, "T3").on == "T2"
    moved = task_events(ws, working, "task.moved")
    assert moved[0] == {"task": "T1", "was": "todo", "now": "doing"}
    assert moved[1] == {"task": "T1", "was": "doing", "now": "done", "note": "12 passed"}
    assert moved[3] == {"task": "T3", "was": "todo", "now": "blocked", "why": "waits for access", "on": "T2"}
    assert all("from" not in d and "to" not in d for kind in ("task.added", "task.edited", "task.moved")
               for d in task_events(ws, working, kind))


def test_start_refuses_open_needs_and_closed_tasks(aops, working, plan_approved):
    aops.task_add(working, [{"text": "a"}, {"text": "b", "needs": ["T1"]}])
    plan_approved(working)
    with pytest.raises(ValidationError, match="needs T1"):
        aops.task_start(working, "T2")
    aops.task_start(working, "T1")
    aops.task_done(working, "T1")
    with pytest.raises(ValidationError, match="reopen"):
        aops.task_start(working, "T1")
    with pytest.raises(NotFoundError):
        aops.task_start(working, "T9")


def test_done_without_start_is_logged(ws, aops, working, plan_approved):
    aops.task_add(working, [{"text": "a"}])
    plan_approved(working)
    aops.task_done(working, "T1")
    assert "T1 done (done without start)" in store.load(ws, working)[1].section("Log")


def test_block_on_checks_the_target(aops, working):
    aops.task_add(working, [{"text": "a"}])
    with pytest.raises(NotFoundError):
        aops.task_block(working, "T1", "why", on="Q9")
    with pytest.raises(UsageError):
        aops.task_block(working, "T1", "why", on="whenever")
    with pytest.raises(UsageError, match="itself"):
        aops.task_block(working, "T1", "why", on="T1")
    t = aops.task_block(working, "T1", "waits for the platform team", on="demo-0042")
    assert tk.ticket_tasks(t)[0].on == "DEMO-0042"


def test_agent_needs_its_claim_and_a_working_status(ws, aops, hops, other_agent, working):
    aops.task_add(working, [{"text": "a"}])
    with pytest.raises(ClaimError):
        Ops(ws, other_agent).task_start(working, "T1")
    backlog = aops.new("later").id
    with pytest.raises(TransitionError, match="backlog"):
        aops.task_add(backlog, [{"text": "x"}])
    with pytest.raises(TransitionError):
        hops.task_add(backlog, [{"text": "x"}])


def test_human_tasks_and_the_humans_limits(ws, aops, hops, working):
    aops.task_add(working, [{"text": "agent work"}, {"text": "grant access", "owner": "human"}])
    with pytest.raises(HumanOnlyError):
        aops.task_done(working, "T2")
    with pytest.raises(HumanOnlyError):
        aops.task_skip(working, "T2", "no")
    with pytest.raises(TransitionError, match="agent's task"):
        hops.task_done(working, "T1")  # decision 4
    hops.task_skip(working, "T1", "done by hand last week")
    hops.task_reopen(working, "T1")
    hops.task_done(working, "T2", "granted")
    assert [(x.id, x.state) for x in tasks_of(ws, working)] == [("T1", "todo"), ("T2", "done")]


def test_edit_fields_and_owner_is_human_only(ws, aops, hops, working):
    aops.task_add(working, [{"text": "a", "refs": ["ac:1"]}, {"text": "b"}])
    aops.task_edit(working, "T1", text="a, better", add_refs=["file:hub/x.py"], drop_refs=["ac:1"],
                   verify="pytest", needs=["T2"])
    t1 = tk.find(tasks_of(ws, working), "T1")
    assert (t1.text, [str(r) for r in t1.refs], t1.verify, t1.needs) == ("a, better", ["file:hub/x.py"], "pytest", ["T2"])
    assert task_events(ws, working, "task.edited")[-1] == {"task": "T1", "fields": ["text", "ref", "verify", "needs"]}
    with pytest.raises(HumanOnlyError):
        aops.task_edit(working, "T2", owner="human")
    hops.task_edit(working, "T2", owner="human")
    with pytest.raises(HumanOnlyError):
        aops.task_edit(working, "T2", text="mine now")
    aops.task_edit(working, "T1", clear_verify=True, clear_needs=True)
    assert (tk.find(tasks_of(ws, working), "T1").verify, tk.find(tasks_of(ws, working), "T1").needs) == (None, [])
    with pytest.raises(UsageError):
        aops.task_edit(working, "T1")
    aops.task_skip(working, "T1", "dropped")
    with pytest.raises(ValidationError, match="reopen"):
        aops.task_edit(working, "T1", text="x")


def test_section_set_tasks_is_refused(aops, working):
    with pytest.raises(UsageError, match="orch task"):
        aops.set_section(working, "Tasks", "- [x] T1 sneaky")


def test_raw_edit_validates_tasks_and_leaves_agent_ticks_to_the_agent(ws, aops, hops, working):
    aops.task_add(working, [{"text": "a"}])
    text = store.resolve(ws, working).path.read_text(encoding="utf-8")
    with pytest.raises(ValidationError, match="Tasks line 2"):
        hops.replace_raw(working, text.replace("- [ ] T1 a", "- [ ] T1 a\nfree text"))
    with pytest.raises(ValidationError, match="agent's"):
        hops.replace_raw(working, text.replace("- [ ] T1 a", "- [x] T1 a"))
    hops.replace_raw(working, text.replace("- [ ] T1 a", "- [-] T1 a\n  - why: not needed"))
    assert tasks_of(ws, working)[0].state == "skipped"


def test_task_events_never_look_like_status_changes(ws, aops, working, plan_approved):
    from orch.dashboard.data.metrics import done_per_weekday
    aops.task_add(working, [{"text": "a"}])
    plan_approved(working)
    aops.task_start(working, "T1")
    aops.task_done(working, "T1")
    events = read_events(ws, working)
    assert _latest_status_change(events, working).kind == "claim.taken"
    assert sum(d["n"] for d in done_per_weekday(ws, events=events)) == 0


def test_raw_edit_cannot_tick_an_agent_task_by_flipping_its_owner(ws, aops, hops, working):
    aops.task_add(working, [{"text": "a"}, {"text": "grant", "owner": "human"}])
    text = store.resolve(ws, working).path.read_text(encoding="utf-8")
    with pytest.raises(ValidationError, match="T1: the agent's"):
        hops.replace_raw(working, text.replace("- [ ] T1 a", "- [x] T1 a\n  - owner: human"))
    hops.replace_raw(working, text.replace("- [ ] T2 grant", "- [x] T2 grant"))  # the human's own task
    assert [x.state for x in tasks_of(ws, working)] == ["todo", "done"]


def test_raw_edit_cannot_take_over_an_open_agent_task(ws, aops, hops, working):
    aops.task_add(working, [{"text": "a"}])
    text = store.resolve(ws, working).path.read_text(encoding="utf-8")
    with pytest.raises(ValidationError, match="taken over with orch"):
        hops.replace_raw(working, text.replace("- [ ] T1 a", "- [ ] T1 a\n  - owner: human"))
    assert tasks_of(ws, working)[0].owner == "agent"


def test_cli_owner_change_is_a_logged_takeover(ws, aops, hops, working):
    aops.task_add(working, [{"text": "a"}])
    hops.task_edit(working, "T1", owner="human")
    assert "T1 taken over by you" in store.load(ws, working)[1].section("Log")
    hops.task_done(working, "T1", "did it myself")
    assert tasks_of(ws, working)[0].state == "done"


def test_blocking_question_never_blocks_a_human_doing_task(ws, aops, hops, working):
    aops.task_add(working, [{"text": "grant", "owner": "human"}])
    hops.task_start(working, "T1")
    aops.ask(working, [{"text": "Which env?", "options": ["dev", "prod"]}])
    assert tasks_of(ws, working)[0].state == "doing"
    assert "task_blocked" not in task_events(ws, working, "question.asked")[-1]


def test_human_done_from_todo_is_not_logged_as_without_start(ws, aops, hops, working, plan_approved):
    aops.task_add(working, [{"text": "grant", "owner": "human"}, {"text": "a"}])
    plan_approved(working)
    hops.task_done(working, "T1", "granted")
    aops.task_done(working, "T2")
    log = store.load(ws, working)[1].section("Log")
    assert "T1 done: granted" in log and "T1 done: granted (done without start)" not in log
    assert "T2 done (done without start)" in log

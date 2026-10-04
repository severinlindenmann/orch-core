"""#9: an agent does not start or finish work while the plan gate it needs is not approved."""
import pytest

from orch.errors import ValidationError


def _plan(aops, tid):
    aops.set_section(tid, "Plan", "1. do it")
    _, ids = aops.task_add(tid, [{"text": "the work"}])
    return ids[0]


def test_agent_task_start_refused_while_plan_pending(working, aops):
    task = _plan(aops, working)
    with pytest.raises(ValidationError, match="plan gate is pending") as e:
        aops.task_start(working, task)
    assert f"orch approve {working} plan" in (e.value.hint or "")


def test_agent_task_done_refused_while_plan_pending(working, aops):
    task = _plan(aops, working)
    with pytest.raises(ValidationError, match="plan gate is pending"):
        aops.task_done(working, task)


def test_task_add_stays_allowed_while_plan_pending(working, aops):
    assert _plan(aops, working)


def test_agent_task_start_allowed_after_plan_approval(working, aops, hops):
    task = _plan(aops, working)
    hops.approve(working, "plan")
    aops.task_start(working, task)
    aops.task_done(working, task)


def test_agent_task_start_refused_when_plan_invalidated(working, aops, hops):
    task = _plan(aops, working)
    hops.approve(working, "plan")
    aops.set_section(working, "Plan", "1. do something else")
    with pytest.raises(ValidationError, match="plan gate is invalidated"):
        aops.task_start(working, task)


def test_sizes_without_plan_gate_are_unaffected(ws, aops, hops):
    t = aops.new("tiny", size="xs")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    hops.approve(t.id, "requirements")
    aops.claim(t.id)
    _, ids = aops.task_add(t.id, [{"text": "the work"}])
    aops.task_start(t.id, ids[0])
    aops.task_done(t.id, ids[0])


def test_human_task_actions_unaffected(working, aops, hops):
    aops.set_section(working, "Plan", "1. do it")
    _, ids = aops.task_add(working, [{"text": "approve the service principal", "owner": "human"}])
    hops.task_done(working, ids[0])

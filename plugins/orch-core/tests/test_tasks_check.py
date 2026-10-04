from datetime import timedelta

from orch.clock import now, stamp
from orch.core.check import run_checks
from orch.core.rules import render_rules
from orch.instructions.render import agents_rules


def codes(ws, tid):
    return {(f.code, f.level) for f in run_checks(ws, emit_events=False) if f.ticket == tid}


def test_parse_error_and_open_tasks_at_testing_are_errors(ws, put):
    assert ("tasks-parse", "error") in codes(ws, put("in-progress", sections={"Tasks": "nonsense"}))
    assert ("tasks-open-at-testing", "error") in codes(ws, put("testing", sections={"Tasks": "- [ ] T1 a"}))


def test_task_warnings(ws, put):
    tid = put("in-progress", sections={"Tasks": (
        "- [/] T1 a\n- [/] T2 b\n- [-] T3 c\n- [ ] T4 d\n  - needs: T9\n- [ ] T5 e\n  - needs: T6\n"
        "- [ ] T6 f\n  - needs: T5\n- [ ] T7 g\n  - ref: file:nowhere/x.py\n  - ref: ac:4")})
    got = codes(ws, tid)
    for code in ("tasks-multiple-doing", "task-reason-missing", "task-needs-unknown", "task-needs-cycle", "task-ref-missing"):
        assert (code, "warning") in got, code


def test_stale_doing_and_missing_list(ws, put):
    old = stamp(now() - timedelta(hours=9))
    stale = put("in-progress", sections={"Tasks": "- [/] T1 a"}, claim={"session": "x", "harness": "h", "at": old})
    assert ("task-doing-stale", "warning") in codes(ws, stale)
    recent = stamp(now() - timedelta(hours=1))
    missing = put("in-progress", size="xs", claim={"session": "x", "harness": "h", "at": recent})
    assert ("tasks-missing", "warning") in codes(ws, missing)


def test_a_plan_checklist_without_tasks_is_not_a_finding(ws, put):
    for tid in (put("testing"), put("done", sections={"Plan": "- [x] a"}), put("open", sections={"Plan": "- [ ] a"})):
        assert not {c for c, _ in codes(ws, tid) if c.startswith(("task", "plan-checklist"))}


def test_rules_and_agent_instructions_mention_tasks(ws):
    assert "tasks: write the list with `orch task add` right after claiming" in render_rules(ws.config)
    assert "orch task list <id>" in agents_rules(ws.config)


def test_ref_missing_is_not_reported_for_closed_tasks(ws, put):
    tid = put("in-progress", sections={"Tasks": (
        "- [x] T1 a\n  - ref: file:nowhere/x.py\n- [-] T2 b\n  - ref: ac:4\n  - why: dropped")})
    assert ("task-ref-missing", "warning") not in codes(ws, tid)

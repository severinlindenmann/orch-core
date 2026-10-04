"""`orch task done --run`: orch runs the task's verify line (or the project's named check) itself, keeps a receipt
artifact and draws the result as a `gates` widget in Verification; the task is ticked only when every step passed."""
import json

import pytest

from orch.core import store, tasks as tk
from orch.core.artifacts import doc_items
from orch.errors import UsageError, ValidationError
from orch.widgets.blocks import parse_blocks


@pytest.fixture
def ticket(working, plan_approved):
    plan_approved(working)
    return working


def _task(aops, tid, verify):
    _, ids = aops.task_add(tid, [{"text": "Prove it", "verify": verify}])
    aops.task_start(tid, ids[0])
    return ids[0]


def _state(ws, tid, task):
    return tk.find(tk.ticket_tasks(store.load(ws, tid)[1]), task).state


def _receipts(ws, tid):
    return [i for i in doc_items(store.load(ws, tid)[1]) if i["kind"] == "receipt"]


def _gates(ws, tid):
    blocks = parse_blocks(store.load(ws, tid)[1].section("Verification"))
    return [b.data for b in blocks if (b.data or {}).get("type") == "gates"]


def test_a_passing_line_ticks_the_task_and_keeps_a_receipt(ws, aops, ticket, tmp_path):
    task = _task(aops, ticket, "cmd: echo ok")
    rec = aops.task_done_run(ticket, task, cwd=tmp_path)
    assert rec["exit"] == 0 and _state(ws, ticket, task) == "done"
    [item] = _receipts(ws, ticket)
    assert item["task"] == task and item["run"]["exit"] == 0
    assert item["run"]["steps"] == [{"name": "verify", "status": "pass", "seconds": 0}]  # no command on the phone
    entry = store.load(ws, ticket)[1].meta["artifacts"][0]
    assert entry["run"]["steps"][0]["run"] == "echo ok"  # the ticket file keeps what ran
    assert (ws.artifacts_dir / ticket / item["name"]).read_bytes().startswith(b"$ echo ok\n")
    t = store.load(ws, ticket)[1]
    note = tk.find(tk.ticket_tasks(t), task).note
    assert note.startswith(f"receipt {item['name']}: exit 0")


def test_a_failing_line_keeps_the_receipt_and_leaves_the_task_open(ws, aops, ticket, tmp_path):
    task = _task(aops, ticket, "cmd: exit 1")
    with pytest.raises(ValidationError, match="exit 1"):
        aops.task_done_run(ticket, task, cwd=tmp_path)
    assert _state(ws, ticket, task) == "doing"
    assert [r["run"]["exit"] for r in _receipts(ws, ticket)] == [1]


def test_a_named_check_runs_the_projects_steps_and_draws_a_gates_widget(ws, aops, ticket, tmp_path, configure):
    aops.ws = configure(checks={"verify": {"steps": [{"name": "build", "run": "true"}, {"name": "test", "run": "exit 4"},
                                           {"name": "lint", "run": "true"}]}})
    task = _task(aops, ticket, "check:verify")
    with pytest.raises(ValidationError, match="test"):
        aops.task_done_run(ticket, task, cwd=tmp_path)
    [g] = _gates(ws, ticket)
    assert g["id"] == f"receipt-{task.lower()}" and g["title"] == f"{task} verify"
    assert [(i["name"], i["status"]) for i in g["items"]] == [("build", "pass"), ("test", "fail"), ("lint", "skip")]
    assert g["source"].startswith("artifact:receipt-")
    assert _receipts(ws, ticket)[0]["run"]["check"] == "verify"

    # the project fixes it; the next run replaces the widget instead of adding another, and ticks the task
    aops.ws = configure(checks={"verify": {"steps": [{"name": "build", "run": "true"}, {"name": "test", "run": "true"}]}})
    aops.task_done_run(ticket, task, cwd=tmp_path)
    [g] = _gates(ws, ticket)
    assert [(i["name"], i["status"]) for i in g["items"]] == [("build", "pass"), ("test", "pass")]
    assert _state(ws, ticket, task) == "done" and len(_receipts(ws, ticket)) == 2


def test_the_widget_keeps_other_verification_text(ws, aops, ticket, tmp_path):
    aops.set_section(ticket, "Verification", "- AC1 measured by hand: 412 ms")
    task = _task(aops, ticket, "cmd: true")
    aops.task_done_run(ticket, task, cwd=tmp_path)
    text = store.load(ws, ticket)[1].section("Verification")
    assert text.startswith("- AC1 measured by hand: 412 ms") and len(_gates(ws, ticket)) == 1


def test_the_receipt_widget_passes_widget_check_and_renders(ws, aops, ticket, tmp_path):
    from orch.widgets import Ctx, render_text
    from orch.widgets.blocks import ticket_blocks
    from orch.widgets.validate import validate
    task = _task(aops, ticket, "cmd: true")
    aops.task_done_run(ticket, task, cwd=tmp_path)
    path, t = store.load(ws, ticket)
    [b] = ticket_blocks(t, path.read_text(encoding="utf-8"))
    assert b.section == "Verification" and validate(b, t, ws=ws) == []
    assert "verify: Pass" in render_text(b, Ctx.of(ws, t))


def test_a_plain_line_is_one_step_named_verify(ws, aops, ticket, tmp_path):
    task = _task(aops, ticket, "cmd: true")
    aops.task_done_run(ticket, task, cwd=tmp_path)
    assert [(i["name"], i["status"]) for i in _gates(ws, ticket)[0]["items"]] == [("verify", "pass")]
    assert _receipts(ws, ticket)[0]["run"]["check"] is None


def test_an_unknown_check_names_the_configured_ones(aops, ticket, tmp_path, configure):
    aops.ws = configure(checks={"unit": {"steps": [{"name": "pytest", "run": "true"}]}})
    task = _task(aops, ticket, "check:nope")
    with pytest.raises(UsageError, match="unit"):
        aops.task_done_run(ticket, task, cwd=tmp_path)


def test_no_verify_line_refuses(aops, ticket, tmp_path):
    _, ids = aops.task_add(ticket, [{"text": "No check"}])
    with pytest.raises(UsageError, match="verify line"):
        aops.task_done_run(ticket, ids[0], cwd=tmp_path)


def test_nothing_runs_before_the_plan_is_approved(ws, aops, working, tmp_path):
    _, ids = aops.task_add(working, [{"text": "Prove it", "verify": f"cmd: touch {tmp_path / 'ran'}"}])
    with pytest.raises(Exception):
        aops.task_done_run(working, ids[0], cwd=tmp_path)
    assert not (tmp_path / "ran").exists()


def test_the_receipt_kind_cannot_be_added_by_hand(aops, ticket, tmp_path):
    f = tmp_path / "fake.log"
    f.write_text("$ npm test\nall green")
    with pytest.raises(UsageError, match="receipt"):
        aops.artifact_add(ticket, f, kind="receipt")


def test_a_bad_check_in_the_config_is_refused_on_use(aops, ticket, tmp_path, configure):
    aops.ws = configure(checks={"verify": {"steps": [{"name": "build"}]}})
    task = _task(aops, ticket, "check:verify")
    with pytest.raises(UsageError, match="run"):
        aops.task_done_run(ticket, task, cwd=tmp_path)


def test_another_sessions_agent_runs_nothing(ws, aops, other_agent, ticket, tmp_path):
    from orch.core.ops import Ops
    from orch.errors import ClaimError
    _, ids = aops.task_add(ticket, [{"text": "Prove it", "verify": f"cmd: touch {tmp_path / 'ran'}"}])
    aops.task_start(ticket, ids[0])
    with pytest.raises(ClaimError):
        Ops(ws, other_agent).task_done_run(ticket, ids[0], cwd=tmp_path)
    assert not (tmp_path / "ran").exists()


def test_a_closed_task_runs_nothing(aops, ticket, tmp_path):
    _, ids = aops.task_add(ticket, [{"text": "Prove it", "verify": f"cmd: touch {tmp_path / 'ran'}"}])
    aops.task_skip(ticket, ids[0], "not needed")
    with pytest.raises(ValidationError, match="skipped"):
        aops.task_done_run(ticket, ids[0], cwd=tmp_path)
    assert not (tmp_path / "ran").exists()

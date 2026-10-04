import re

import pytest

pytest.importorskip("fastapi")


def _page(dash, tid):
    return dash.get(f"/t/{tid}").text


def _four(aops, tid):
    aops.task_add(tid, [{"text": "a", "verify": "pytest"}, {"text": "b"}, {"text": "grant", "owner": "human"}, {"text": "c"}])
    aops.task_start(tid, "T1")
    aops.task_block(tid, "T2", "waits for access", on="DEMO-0042")
    aops.task_skip(tid, "T4", "not needed")


def test_tasks_card_comes_first_while_in_work(dash, aops, working, plan_approved):
    plan_approved(working)
    _four(aops, working)
    html = _page(dash, working)
    # story page: the Tasks card lives in the Doing chapter, which is the open one while in work
    assert html.index('id="agreed"') < html.index('id="tasks"') < html.index('id="proven"')
    assert re.search(r'<details class="chapter chapter-doing chapter-current" id="doing" open', html)
    assert re.search(r'id="task-T1"[^>]*aria-current="step"', html)
    assert "0 of 4 done · 1 skipped · 1 blocked" in html
    assert 'role="img" aria-label="1 of 4 tasks closed: 1 skipped, 1 in progress, 1 blocked, 1 to do"' in html
    assert "Reason: not needed" in html and "Blocked by DEMO-0042" in html
    assert "Testing needs T1, T2, T3 closed" in html
    assert html.count('name="action" value="done"') == 1  # only the human task offers Done
    assert f'action="/t/{working}/task/add"' in html


def test_tasks_card_after_sections_outside_work_and_read_only(dash, put):
    tid = put("testing", sections={"Tasks": "- [x] T1 a", "Verification": "ok"})
    html = _page(dash, tid)
    assert html.index('id="doing"') < html.index('id="tasks"') < html.index('id="proven"')
    assert "/task/add" not in html and 'name="action"' not in html


def test_legacy_plan_checklist_only_without_tasks(dash, aops, put, working):
    legacy = put("in-progress", sections={"Plan": "- [x] a\n- [ ] b"})
    html = _page(dash, legacy)
    # a plan waiting for approval shows its text exactly as hashed (Markdown); the checklist is for settled plans
    assert 'class="md-task"' in html and "No tasks yet" in html
    aops.set_section(working, "Plan", "- [x] a\n- [ ] b")
    aops.task_add(working, [{"text": "a"}])
    html = _page(dash, working)
    assert 'class="checklist"' not in html and "Progress is tracked in Tasks" in html


def test_plan_shows_tasks_added_since_approval(dash, aops, hops, working):
    aops.task_add(working, [{"text": "a"}])
    aops.set_section(working, "Plan", "one step")
    hops.approve(working, "plan")
    aops.task_add(working, [{"text": "late"}])
    html = _page(dash, working)
    assert "1 task added since approval" in html and "added after approval" in html


def test_broken_tasks_card(dash, put):
    tid = put("in-progress", sections={"Tasks": "nonsense"})
    html = _page(dash, tid)
    assert "Tasks section cannot be read" in html and f'href="/t/{tid}/raw"' in html


def test_task_text_is_escaped(dash, aops, working):
    aops.task_add(working, [{"text": "<script>alert(1)</script> run `pytest`", "verify": "<b>x</b>"}])
    html = _page(dash, working)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html and "<code>pytest</code>" in html
    assert "<b>x</b>" not in html


def test_your_move_links_to_the_human_task(dash, aops, working):
    aops.task_add(working, [{"text": "Grant SELECT", "owner": "human"}])
    html = _page(dash, working)
    assert 'href="#task-T1"' in html and "T1 is yours: Grant SELECT" in html


def test_board_card_progress(dash, aops, working, plan_approved):
    aops.task_add(working, [{"text": "a"}, {"text": "b"}])
    plan_approved(working)
    aops.task_start(working, "T1")
    aops.new("no tasks")
    html = dash.get("/board").text
    assert html.count('class="task-bar"') == 1 and "Tasks 0/2" in html  # M: the board card says it in words, with the bar
    assert 'aria-label="0 of 2 tasks closed: 1 in progress, 1 to do"' in html


def test_today_in_flight_shows_the_current_task(dash, aops, working, plan_approved):
    aops.task_add(working, [{"text": "Switch the jobs"}, {"text": "b"}])
    plan_approved(working)
    aops.task_start(working, "T1")
    html = dash.get("/").text
    flight = html.split('class="panel in-flight"', 1)[1]
    assert "Now" in flight and "T1" in flight and "Switch the jobs" in flight and "0 of 2 done" in flight


def test_today_decision_card_for_a_human_task(dash, aops, working):
    aops.task_add(working, [{"text": "Grant SELECT", "owner": "human", "verify": "SHOW GRANTS"}])
    html = dash.get("/").text
    assert "Your task" in html and f'action="/t/{working}/task"' in html and "What proved it?" in html


def test_phone_layout_and_stylesheet(dash):
    css = dash.get("/static/tasks.css")
    assert css.status_code == 200
    assert "@media (max-width: 720px)" in css.text and "min-height: 44px" in css.text and "min-height: 24px" in css.text
    assert re.search(r'<link rel="stylesheet" href="/static/tasks\.css\?v=[0-9a-f]+">', dash.get("/board").text)

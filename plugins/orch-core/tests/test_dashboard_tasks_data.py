import pytest

pytest.importorskip("fastapi")

from orch.core import query, store, tasks_view  # noqa: E402
from orch.core import tasks as tk  # noqa: E402
from orch.core.events import read_events  # noqa: E402
from orch.dashboard.data import decisions as decisions_data  # noqa: E402
from orch.dashboard.data import steps, timeline  # noqa: E402
from orch.dashboard.data import tasks as tasks_data  # noqa: E402


def _view(ws, tid):
    return tasks_view.view(ws, store.load(ws, tid)[1])


def _four(aops, tid):
    aops.task_add(tid, [{"text": "a", "verify": "pytest", "refs": ["ac:1", "file:hub/missing.py"]}, {"text": "b"},
                        {"text": "grant", "owner": "human"}, {"text": "c"}])
    aops.task_start(tid, "T1")
    aops.task_block(tid, "T2", "waits for access", on="DEMO-0042")
    aops.task_skip(tid, "T4", "not needed")


def test_card_rows_chips_and_actions(ws, aops, working, plan_approved):
    plan_approved(working)
    _four(aops, working)
    c = tasks_data.card(_view(ws, working), can_edit=True)
    rows = {r["id"]: r for r in c["rows"]}
    assert (rows["T1"]["chip"]["role"], rows["T1"]["chip"]["text"]) == ("info", "Now")
    assert (rows["T2"]["chip"]["role"], rows["T2"]["chip"]["text"]) == ("warn", "Blocked by DEMO-0042")
    assert (rows["T3"]["chip"]["role"], rows["T3"]["chip"]["text"]) == ("you", "Your task")
    assert rows["T4"]["chip"]["text"] == "Skipped"
    assert [a["action"] for a in rows["T1"]["actions"]] == ["skip"]  # decision 4: no Done on agent work
    assert [a["action"] for a in rows["T3"]["actions"]] == ["done", "skip"]
    assert [a["action"] for a in rows["T2"]["actions"]] == ["skip", "reopen"]
    assert [a["action"] for a in rows["T4"]["actions"]] == ["reopen"]
    assert c["progress"]["text"] == "0 of 4 done · 1 skipped · 1 blocked"
    assert c["progress"]["aria"] == "1 of 4 tasks closed: 1 skipped, 1 in progress, 1 blocked, 1 to do"
    assert c["segments"] == ["doing", "blocked", "todo", "skipped"] and c["open"] == ["T1", "T2", "T3"]
    assert [r["text"] for r in rows["T1"]["refs"] if r["missing"]] == ["hub/missing.py"]
    assert tasks_data.card(_view(ws, working), can_edit=False)["rows"][0]["actions"] == []


def test_blocked_on_an_open_question_needs_you(ws, aops, working):
    aops.ask(working, [{"text": "FYI?", "type": "confirm", "blocking": False}])
    aops.task_add(working, [{"text": "a"}])
    aops.task_block(working, "T1", "needs the answer", on="Q1")
    chip = tasks_data.card(_view(ws, working), can_edit=True)["rows"][0]["chip"]
    assert (chip["role"], chip["text"], chip["url"]) == ("you", "Needs you · Q1", "#q-Q1")


def test_continuous_bar_above_twelve_tasks(ws, aops, working):
    aops.task_add(working, [{"text": f"t{i}"} for i in range(13)])
    c = tasks_data.card(_view(ws, working), can_edit=True)
    assert c["segments"] is None and c["bar"] == [("todo", 13)]


def test_board_and_flight_summaries(ws, aops, working, plan_approved):
    aops.task_add(working, [{"text": "Switch the jobs"}, {"text": "b"}])
    plan_approved(working)
    aops.task_start(working, "T1")
    aops.task_block(working, "T2", "access", on="DEMO-0042")
    b = tasks_data.board_progress(store.resolve(ws, working))
    assert (b["progress"]["short"], b["blocked"]) == ("0/2", 1)
    f = tasks_data.flight(_view(ws, working))
    assert f["now"] == {"id": "T1", "text": "Switch the jobs"} and f["label"] == "Now"
    assert f["blocked"] == "T2 blocked by DEMO-0042"
    assert tasks_data.board_progress(store.resolve(ws, aops.new("empty").id)) is None


def test_activity_texts_and_category(ws, aops, working, plan_approved):
    aops.task_add(working, [{"text": "a", "verify": "v"}, {"text": "b"}])
    plan_approved(working)
    aops.task_start(working, "T1")
    aops.task_done(working, "T1", "validate OK")
    aops.task_skip(working, "T2", "no init scripts")
    task_events = [e for e in read_events(ws, working) if e.kind.startswith("task.")]
    assert [timeline.describe(e) for e in task_events] == [
        "added T1, T2", "started T1", "finished T1: validate OK", "skipped T2: no init scripts"]
    assert {timeline.category(e) for e in task_events} == {"agents"}


def test_your_move_and_decision_for_a_human_task(ws, aops, working):
    aops.task_add(working, [{"text": "Grant SELECT", "owner": "human"}])
    move = steps.your_move(store.load(ws, working)[1], query.needs_you(ws))
    assert move["kind"] == "human" and "Grant SELECT" in move["text"]
    assert move["action"] == {"kind": "task", "task": "T1", "label": "Go to T1", "href": "#task-T1"}
    d = next(x for x in decisions_data.decisions(ws) if x.kind == "task")
    assert (d.category, d.task["id"], d.excerpt) == ("tasks", "T1", "Grant SELECT")


def test_dashboard_task_posts(dash, ws, aops, working):
    aops.task_add(working, [{"text": "agent work"}, {"text": "grant", "owner": "human", "verify": "SHOW GRANTS"}])

    def state():
        return [(x.id, x.state) for x in tk.ticket_tasks(store.load(ws, working)[1])]

    dash.post(f"/t/{working}/task", data={"task": "T1", "action": "done"})          # decision 4
    dash.post(f"/t/{working}/task", data={"task": "T2", "action": "done"})          # verify needs a note
    assert state() == [("T1", "todo"), ("T2", "todo")]
    dash.post(f"/t/{working}/task", data={"task": "T2", "action": "done", "message": "granted"})
    dash.post(f"/t/{working}/task", data={"task": "T1", "action": "skip", "message": "done by hand"})
    dash.post(f"/t/{working}/task", data={"task": "T1", "action": "reopen"})
    dash.post(f"/t/{working}/task/add", data={"text": "check the cost", "owner": "agent"})
    r = dash.post(f"/t/{working}/task", data={"task": "T1", "action": "explode"})
    assert r.status_code == 200 and state() == [("T1", "todo"), ("T2", "done"), ("T3", "todo")]
    humans = [e for e in read_events(ws, working) if e.kind.startswith("task.") and e.actor == "human:you"]
    assert humans and {e.via for e in humans} == {"dashboard"}


def test_task_posts_need_the_token_and_same_origin(dash, ws, aops, working):
    aops.task_add(working, [{"text": "grant", "owner": "human"}])
    for path, data in ((f"/t/{working}/task", {"task": "T1", "action": "done"}),
                       (f"/t/{working}/task/add", {"text": "x"})):
        r = dash.post(path, data=data, headers={"Origin": "http://evil.example"}, follow_redirects=False)
        assert r.status_code == 403
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    anon = TestClient(create_app(ws, "tok"))
    assert anon.post(f"/t/{working}/task", data={"task": "T1", "action": "done"}).status_code == 401
    assert [x.state for x in tk.ticket_tasks(store.load(ws, working)[1])] == ["todo"]


def test_ready_human_task_does_not_hide_a_working_agent(ws, aops, working, plan_approved):
    from orch.dashboard.data import agents, today

    aops.task_add(working, [{"text": "build"}, {"text": "grant", "owner": "human"}])
    plan_approved(working)
    aops.task_start(working, "T1")

    def state():
        entries = store.scan(ws)
        needs = query.needs_you(ws, entries=entries)
        events = read_events(ws)
        rows = agents.agent_rows(ws, entries=entries, needs=needs, events=events)
        flight = today.in_flight(ws, needs=needs, entries=entries, events=events, rows=rows)
        return needs, rows[0].status, [c["id"] for c in flight]

    needs, status, flight = state()
    assert [i["kind"] for i in needs] == ["task"]  # the badge still counts the human's task
    assert status == "working" and flight == [working]
    aops.task_done(working, "T1")
    needs, status, flight = state()
    assert [i["kind"] for i in needs] == ["task"]
    assert status == "waiting" and flight == []


def test_board_does_not_reread_unchanged_ticket_files(dash, aops, working, monkeypatch):
    aops.task_add(working, [{"text": "a"}])
    dash.get("/board")
    calls = []
    real = tasks_data.parse_body
    monkeypatch.setattr(tasks_data, "parse_body", lambda text: calls.append(1) or real(text))
    assert dash.get("/board").status_code == 200
    assert calls == []
    aops.task_add(working, [{"text": "b"}])  # a changed file is read again
    assert dash.get("/board").status_code == 200
    assert calls == [1]


def test_your_move_for_a_broken_tasks_section_names_the_section(ws, put):
    tid = put("in-progress", sections={"Tasks": "- [ ] T1 a\nfree text"})
    t = store.load(ws, tid)[1]
    move = steps.your_move(t, query.needs_you(ws))
    assert move["text"].startswith("The Tasks section cannot be read") and "Tasks line 2" in move["text"]
    assert "ticket file" not in move["text"]

import re
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")


def test_steps_names_and_single_current():
    from orch.core.model import new_ticket
    from orch.dashboard.data.steps import steps
    t = new_ticket("L-0001", "x", type="feature", priority="normal", size="m", created="2026-10-01T10:00Z")
    t.meta["status"] = "backlog"
    s = steps(t, plan_skip_sizes=["xs", "s"])
    assert [x["name"] for x in s] == ["Requirements", "Plan", "Work", "Testing", "Done"]
    assert sum(x["state"] == "current" for x in s) == 1


def test_plan_skipped_for_small_tickets():
    from orch.core.model import new_ticket
    from orch.dashboard.data.steps import steps
    t = new_ticket("L-0002", "x", type="chore", priority="normal", size="xs", created="2026-10-01T10:00Z")
    t.meta["status"] = "in-progress"
    plan = next(x for x in steps(t, plan_skip_sizes=["xs", "s"]) if x["name"] == "Plan")
    assert plan["state"] == "skipped"


def test_plan_checklist():
    from orch.dashboard.data.steps import plan_checklist
    items = plan_checklist("- [x] inventory\n- [ ] bronze\n- [ ] gold")
    assert [i["state"] for i in items] == ["done", "now", "todo"]
    assert plan_checklist("1. plain list") is None


def test_status_card_on_ticket(dash, put):
    """Story page (#16): the L header (h1, steps) first, then the status card, which names the move in pink."""
    tid = put("testing", sections={"Verification": "0 duplicates"})
    html = dash.get(f"/t/{tid}").text
    first = html.index("<h1")
    assert html.index('aria-label="Journey"', first) < html.index('class="status-card', first)  # M: the journey
    assert '<span class="chip chip-you"><svg class="i" aria-hidden="true"><use href="#i-you"/></svg> Verdict</span>' in _bar(html)
    assert 'value="done"' in html


def test_no_empty_line_any_more(dash, put):
    tid = put("open")
    assert "Empty:" not in dash.get(f"/t/{tid}").text  # #16: the "Empty: …" line is gone


def test_ticket_keeps_every_action(dash, put):
    tid = put("open")
    html = dash.get(f"/t/{tid}").text
    for action in ("/comment", "/move", "/artifacts", "/edit"):
        assert f'/t/{tid}{action}"' in html


# ---------- steps(): more rules ----------

def _t(status="backlog", size="m", **sections):
    from orch.core.model import new_ticket
    t = new_ticket("L-0009", "x", type="feature", priority="normal", size=size, created="2026-10-01T10:00Z")
    t.meta["status"] = status
    for name, text in sections.items():
        t.set_section(name.replace("_", " "), text)
    return t


def test_skipped_plan_is_not_current_and_work_is():
    from orch.dashboard.data.steps import steps
    s = steps(_t("in-progress", size="xs"), plan_skip_sizes=("xs",))
    assert [(x["name"], x["state"]) for x in s] == [("Requirements", "done"), ("Plan", "skipped"), ("Work", "current"),
                                                    ("Testing", "todo"), ("Done", "todo")]
    assert s[1]["note"] == "skipped"


def test_changes_requested_is_noted_on_the_step():
    from orch.core.gates import gate_hash
    from orch.dashboard.data.steps import steps
    t = _t("in-progress", Plan="1. do it")
    t.meta["gates"]["plan"]["changes_requested"] = {"at": "2026-10-01T11:00Z", "by": "human", "message": "smaller",
                                                    "hash": gate_hash(t, "plan")}
    plan = steps(t)[1]
    assert plan["state"] == "current" and plan["note"] == "Changes requested"


def test_approved_note_carries_the_date():
    from orch.core.gates import gate_hash
    from orch.dashboard.data.steps import steps
    t = _t("open", Requirements="- r", Acceptance_criteria="- a")
    t.meta["gates"]["requirements"] = {"approved": "2026-09-30T09:00Z", "via": "dashboard",
                                       "hash": gate_hash(t, "requirements")}
    assert steps(t)[0]["note"].startswith("Approved ")


def test_small_ticket_with_edited_approved_plan_is_not_skipped():
    from orch.dashboard.data.steps import steps
    t = _approved(_t("in-progress", size="xs", Plan="1. a"), "plan")
    t.set_section("Plan", "1. b")
    plan = steps(t, plan_skip_sizes=("xs",))[1]
    assert plan["state"] == "current" and plan["note"] != "skipped"


# ---------- plan_checklist() ----------

def test_plan_checklist_sub_bullets_stay_sub_lines():
    from orch.dashboard.data.steps import plan_checklist
    items = plan_checklist("- [ ] bronze\n  - raw table\n    continued\n  - silver\n- [ ] gold")
    assert items[0]["text"] == "bronze" and items[0]["subs"] == ["raw table continued", "silver"]
    assert items[1]["subs"] == []


def test_plan_checklist_all_done_has_no_now():
    from orch.dashboard.data.steps import plan_checklist
    assert [i["state"] for i in plan_checklist("- [x] a\n- [X] b")] == ["done", "done"]


def test_plan_checklist_keeps_text_and_continuations():
    from orch.dashboard.data.steps import plan_checklist
    items = plan_checklist("* [ ] add `job`\n  at 02:00\n\n+ [x] check")
    assert [i["text"] for i in items] == ["add `job` at 02:00", "check"]
    assert [i["state"] for i in items] == ["now", "done"]


def test_plan_checklist_mixed_text_falls_back():
    from orch.dashboard.data.steps import plan_checklist
    assert plan_checklist("Some intro\n- [ ] a") is None
    assert plan_checklist("") is None


# ---------- your_move() ----------

def _need(kind, detail="", ticket="L-0009"):
    return {"ticket": ticket, "title": "x", "status": "backlog", "kind": kind, "detail": detail}


def _approved(t, *gates):
    from orch.core.gates import gate_hash
    for g in gates:
        t.meta["gates"][g] = {"approved": "2026-09-30T09:00Z", "via": "dashboard", "hash": gate_hash(t, g)}
    return t


def _ready(kind):
    """A ticket in which the needs kind is really actionable."""
    if kind == "approve-requirements":
        return _t("backlog", Requirements="- r", Acceptance_criteria="- a")
    if kind == "approve-plan":
        return _t("in-progress", Plan="1. do it")
    if kind == "re-approve":
        t = _approved(_t("in-progress", Plan="1. do it"), "plan")
        t.set_section("Plan", "1. do it differently")
        return t
    return _t("testing" if kind == "verdict" else "backlog")


@pytest.mark.parametrize("kind,detail,want", [
    ("approve-requirements", "", {"kind": "approve", "gate": "requirements", "label": "Approve requirements"}),
    ("approve-plan", "", {"kind": "approve", "gate": "plan", "label": "Approve plan"}),
    ("re-approve", "plan", {"kind": "approve", "gate": "plan", "label": "Re-approve plan"}),
    ("answer", "Q2, Q3", {"kind": "answer", "qid": "Q2", "label": "Answer Q2", "href": "#q-Q2"}),
    ("verdict", "", {"kind": "verdict", "label": "Accept"}),
    ("broken", "bad yaml", {"kind": "repair", "label": "Repair the file", "href": "/t/L-0009/raw"}),
])
def test_your_move_per_needs_kind(kind, detail, want):
    from orch.dashboard.data.steps import your_move
    move = your_move(_ready(kind), [_need(kind, detail)])
    assert move["kind"] == "human" and move["text"]
    assert {k: move["action"].get(k) for k in want} == want


def test_reapprove_requirements_outside_backlog_offers_move_back():
    from orch.dashboard.data.steps import your_move
    t = _approved(_t("open", Requirements="- r", Acceptance_criteria="- a"), "requirements")
    t.set_section("Requirements", "- r changed")
    move = your_move(t, [_need("re-approve", "requirements")], moves=("backlog",))
    assert move["action"] == {"kind": "move", "gate": "requirements", "to": "backlog", "label": "Move back to backlog"}
    assert move["text"].startswith("Requirements changed after approval")


def test_reapprove_plan_in_testing_lets_the_verdict_lead():
    from orch.dashboard.data.steps import your_move
    t = _approved(_t("testing", Plan="1. a"), "plan")
    t.set_section("Plan", "1. b")
    move = your_move(t, [_need("re-approve", "plan"), _need("verdict")], moves=("backlog",))
    assert move["action"]["kind"] == "verdict"
    assert [m["action"]["kind"] for m in move["more"]] == ["hint"] and "Plan changed after approval" in move["more"][0]["text"]


def test_reapprove_plan_in_open_offers_move_to_in_progress():
    from orch.dashboard.data.steps import your_move
    t = _approved(_t("open", Plan="1. a"), "plan")
    t.set_section("Plan", "1. b")
    move = your_move(t, [_need("re-approve", "plan")], moves=("backlog", "in-progress"))
    assert move["action"]["to"] == "in-progress" and move["action"]["label"] == "Move back to in progress"


def test_can_approve_mirrors_ops_and_change_requests():
    from orch.core.gates import gate_hash
    from orch.dashboard.data.steps import can_approve
    t = _t("backlog", Requirements="- r", Acceptance_criteria="- a")
    assert can_approve(t, "requirements")
    t.meta["gates"]["requirements"]["changes_requested"] = {"message": "x", "hash": gate_hash(t, "requirements")}
    assert not can_approve(t, "requirements")
    assert not can_approve(_t("backlog", Requirements="- r"), "requirements")       # AC empty
    assert not can_approve(_t("open", Plan="1. x"), "plan")                          # wrong status
    assert not can_approve(_t("in-progress", size="xs", Plan="1. x"), "plan", plan_skip_sizes=("xs",))
    assert can_approve(_t("in-progress", Plan="1. x"), "plan")


def test_your_move_ignores_other_tickets_and_falls_back_to_agent():
    from orch.dashboard.data.steps import your_move
    move = your_move(_t("backlog"), [_need("verdict", ticket="L-0001")])
    assert move["kind"] == "agent" and move["action"] is None and move["text"]


def test_agent_move_names_the_requested_changes():
    from orch.core.gates import gate_hash
    from orch.dashboard.data.steps import your_move
    t = _t("in-progress", Plan="1. do it")
    t.meta["gates"]["plan"]["changes_requested"] = {"at": "2026-10-01T11:00Z", "by": "human", "message": "split step 2",
                                                    "hash": gate_hash(t, "plan")}
    move = your_move(t, [])
    assert move["kind"] == "agent" and "split step 2" in move["text"]


# ---------- meta_line() ----------

def test_meta_line_only_facts_that_apply():
    from orch.core.questions import build_questions
    from orch.dashboard.data.steps import meta_line
    now = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
    t = _t("in-progress")
    t.meta["created"] = "2026-10-01T10:00Z"
    t.meta["updated"] = "2026-10-03T10:00Z"
    t.meta["questions"] = build_questions([{"text": "Which?", "type": "text"}], [], "2026-10-03T10:00Z")
    line = meta_line(t, [_need("answer", "Q1")], now)
    assert line == "open 2 d · waiting on you 5 h · 1 open question"
    t.meta["questions"] = []
    assert meta_line(t, [], now) == "open 2 d"
    assert meta_line(t, [], datetime(2026, 10, 1, 10, 12, tzinfo=timezone.utc)) == "open 12 min"


def test_meta_line_waits_since_the_event_that_created_the_need():
    from orch.core.events import Event
    from orch.dashboard.data.steps import meta_line
    now = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)
    t = _t("testing")
    t.meta["created"], t.meta["updated"] = "2026-10-01T10:00Z", "2026-10-03T14:00Z"
    events = [Event(1, "2026-10-02T15:00Z", t.id, "ticket.moved", "agent:x", "cli", {"from": "in-progress", "to": "testing"}),
              Event(2, "2026-10-03T14:00Z", t.id, "log.added", "agent:x", "cli", {})]
    assert "waiting on you 1 d" in meta_line(t, [_need("verdict")], now, events=events)
    assert "waiting on you 1 h" in meta_line(t, [_need("verdict")], now, events=[])


# ---------- the page ----------

def _page(dash, tid):
    return dash.get(f"/t/{tid}").text


def test_header_has_at_most_two_chips(dash, put):
    tid = put("in-progress", external=[{"key": "ABC-1", "url": "https://jira.example/ABC-1"},
                                       {"key": "ABC-2", "url": None}])
    html = _page(dash, tid)
    head = html.split('class="ticket-head', 1)[1].split("</header>", 1)[0]
    assert len(re.findall(r'class="chip[ "]', head)) <= 2
    assert "In progress" in head and "ABC-1" in head and "created " in head  # one external key in the header
    assert "ABC-2" in html.split("<aside", 1)[1]  # every key in the aside's Links
    assert 'class="meta-line' in head


def test_approve_bar_has_seen_and_request_changes(dash, put):
    from orch.core import store
    tid = put("backlog", sections={"Requirements": "- r1", "Acceptance criteria": "- [ ] a1"})
    html = _page(dash, tid)
    assert "Approve requirements" in _bar(html) and f'action="/t/{tid}/approve"' not in _bar(html)
    bar = _gate(html, "requirements")  # the Approve sits under the full gated text, in Agreed
    assert "Approve requirements" in bar
    assert f'action="/t/{tid}/approve"' in bar and 'name="seen" value="sha256:' in bar
    assert f'action="/t/{tid}/request-changes"' in bar and 'name="message"' in bar
    _, t = store.load(dash.app.state.ws, tid)
    from orch.core.gates import gate_hash
    assert gate_hash(t, "requirements") in bar


def test_answer_bar_links_to_the_question(dash, put):
    from orch.core.questions import build_questions
    q = build_questions([{"text": "Which schema?", "options": [{"key": "A", "label": "ops"}, {"key": "B", "label": "tmp"}]}],
                        [], "2026-09-30T09:00Z")[0]
    tid = put("backlog", questions=[q])
    html = _page(dash, tid)
    assert f'id="q-{q["id"]}"' in _bar(html) and f'action="/t/{tid}/answer"' in _bar(html)  # answered in the card
    assert html.count(f'id="q-{q["id"]}"') == 1


def test_agent_bar_when_nothing_waits(dash, put):
    tid = put("open")
    bar = _bar(_page(dash, tid))
    assert "Agent's move" in bar and "chip-you" not in bar


def test_skipped_plan_on_page(dash, ws, put):
    tid = put("in-progress", size="xs")
    html = _page(dash, tid)
    agreed = html.split('id="agreed"', 1)[1].split("</summary>", 1)[0]  # M: the journey folds Plan into Agreed
    assert "– plan" in agreed


def test_plan_renders_as_checklist(dash, ws, put):
    """A settled (approved) checklist plan shows its progress; one waiting for approval shows the hashed text."""
    tid = put("in-progress", sections={"Plan": "- [x] inventory\n- [ ] bronze `job`\n- [ ] gold"})
    _store_edit(ws, tid, approve=("plan",))
    html = _page(dash, tid)
    checklist = html.split('class="checklist"', 1)[1].split("</ul>", 1)[0]
    assert checklist.count("<li") == 3 and "<code>job</code>" in checklist
    assert "check-done" in checklist and "check-now" in checklist and "check-todo" in checklist


def test_plain_plan_renders_as_markdown(dash, put):
    tid = put("in-progress", sections={"Plan": "1. first\n2. second"})
    html = _page(dash, tid)
    assert 'class="checklist"' not in html and "<ol>" in html


def test_unwritten_gates_say_so(dash, put):
    tid = put("open", sections={"Ask": "please"})
    html = _page(dash, tid)
    assert "Not written yet." in _gate(html, "requirements") and "Not written yet." in _gate(html, "plan")
    assert "please" in html.split('id="asked"', 1)[1].split("</details>", 1)[0]


def test_single_activity_list_and_log_behind_show_log(dash, aops):
    t = aops.new("Backup")
    aops.log(t.id, "started on it")
    html = _page(dash, t.id)
    assert html.count('<ol class="act" ') == 1
    details = html.split("<summary>Raw log</summary>", 1)[1].split("</details>", 1)[0]
    assert "started on it" in details


def test_aside_has_timeline_and_files(dash, put):
    tid = put("open")
    html = _page(dash, tid)
    aside = html.split("<aside", 1)[1]
    assert aside.index('id="artifacts"') < aside.index('id="log"')  # M: the Artifacts panel first
    assert 'id="code"' in html.split('id="doing"', 1)[1].split("</details>", 1)[0]


def test_start_agent_slot_renders_nothing():
    from orch.dashboard.views import TEMPLATES
    assert TEMPLATES.env.get_template("_start_agent.html").render(t=None).strip() == ""


def test_pr_before_plan_approval_warns(dash, put):
    tid = put("in-progress", sections={"Plan": "1. x"}, prs=[{"repo": "dbt", "state": "open", "url": None}])
    assert "PR before plan approval" in _page(dash, tid)
    small = put("in-progress", size="xs", prs=[{"repo": "dbt", "state": "open", "url": None}])
    assert "PR before plan approval" not in _page(dash, small)


def _bar(html):
    return html.split('class="status-card', 1)[1].split("</section>", 1)[0]


def _gate(html, gate):
    return html.split(f'id="gate-{gate}"', 1)[1].split("</section>", 1)[0]


def _store_edit(ws, tid, approve=(), **sections):
    from orch.core import store
    from orch.core.gates import gate_hash
    _, t = store.load(ws, tid)
    for g in approve:
        t.meta["gates"][g] = {"approved": "2026-09-30T09:00Z", "via": "dashboard", "hash": gate_hash(t, g)}
    for name, text in sections.items():
        t.set_section(name.replace("_", " "), text)
    store.save(ws, t)


def test_testing_with_edited_plan_offers_neither_approve_nor_accept(dash, ws, put):
    tid = put("testing", sections={"Requirements": "- r", "Acceptance criteria": "- a", "Plan": "- [ ] a"})
    _store_edit(ws, tid, approve=("requirements", "plan"))
    _store_edit(ws, tid, Plan="- [ ] a\n- [ ] new step")
    html = _page(dash, tid)
    bar = _bar(html)
    assert f'action="/t/{tid}/approve"' not in html
    # final review C1: a changed plan is re-approved before any verdict; sending it back stays possible
    assert not re.search(r'>\s*Accept, mark done</button>', html) and 'id="send-back"' in html
    assert "Plan changed after approval" in bar and f'action="/t/{tid}/request-changes"' in bar


def test_open_with_edited_requirements_offers_move_back(dash, ws, put):
    tid = put("open", sections={"Requirements": "- r", "Acceptance criteria": "- a"})
    _store_edit(ws, tid, approve=("requirements",))
    _store_edit(ws, tid, Requirements="- r changed")
    html = _page(dash, tid)
    bar = _bar(html)
    assert f'action="/t/{tid}/approve"' not in html
    assert f'action="/t/{tid}/move"' in bar and 'name="to" value="backlog"' in bar and "Move back to backlog" in bar
    assert 'data-inline-confirm="Confirm · move to backlog"' in bar  # no browser popup


def test_no_approve_while_changes_requested(dash, ws, put):
    from orch.core import store
    from orch.core.gates import gate_hash
    tid = put("backlog", sections={"Requirements": "- r", "Acceptance criteria": "- a"})
    _, t = store.load(ws, tid)
    t.meta["gates"]["requirements"]["changes_requested"] = {"at": "2026-10-01T11:00Z", "by": "you", "message": "split it",
                                                            "hash": gate_hash(t, "requirements")}
    store.save(ws, t)
    html = _page(dash, tid)
    assert f'action="/t/{tid}/approve"' not in html and "split it" in _bar(html)


@pytest.mark.parametrize("priority,external,words", [
    ("high", [{"key": "ABC-1", "url": None}], ("#i-warn", "high", "ABC-1")),
    ("urgent", [], ("#i-warn", "urgent")),
    ("normal", [{"key": "ABC-1", "url": None}], ("normal", "ABC-1")),
])
def test_header_meta_line_has_priority_and_external(dash, put, priority, external, words):
    """The L header: one meta line (key · size · priority · external · created), then h1, then one move chip."""
    tid = put("open", priority=priority, external=external)
    head = _page(dash, tid).split('class="ticket-head', 1)[1].split("</header>", 1)[0]
    meta = head.split('class="tc-metaline', 1)[1].split("</p>", 1)[0]
    assert len(re.findall(r'class="chip[ "]', head)) == 1
    assert all(w in meta for w in words)


@pytest.mark.parametrize("setup,role,label", [
    ("approved", "ok", "approved"),
    ("changes", "warn", "changes requested"),
    ("waits", None, "waits for you"),  # neutral here: the status card's move chip owns the pink
    ("invalidated", None, "waits for you"),  # in backlog a changed gate is yours to re-approve
    ("pending", "neu", "not approved yet"),
])
def test_gate_chip_variants(dash, ws, put, setup, role, label):
    from orch.core import store
    from orch.core.gates import gate_hash
    tid = put("backlog", sections={"Requirements": "- r", "Acceptance criteria": "- a" if setup != "pending" else ""})
    _, t = store.load(ws, tid)
    if setup in ("approved", "invalidated"):
        t.meta["gates"]["requirements"] = {"approved": "2026-09-30T09:00Z", "via": "dashboard",
                                           "hash": gate_hash(t, "requirements")}
    if setup == "invalidated":
        t.set_section("Requirements", "- r changed")
    if setup == "changes":
        t.meta["gates"]["requirements"]["changes_requested"] = {"message": "x", "hash": gate_hash(t, "requirements")}
    store.save(ws, t)
    html = _page(dash, tid)
    head = _gate(html, "requirements").split('class="gate-head"', 1)[1].split("</div>", 1)[0]
    assert label in head
    assert (f'class="chip chip-{role}"' in head) if role else ("chip-" not in head)


def test_checklist_sub_lines_render(dash, ws, put):
    tid = put("in-progress", sections={"Plan": "- [ ] bronze\n  - raw table\n- [ ] gold"})
    _store_edit(ws, tid, approve=("plan",))
    checklist = _page(dash, tid).split('class="checklist"', 1)[1]
    assert '<ul class="check-subs"><li>raw table</li></ul>' in checklist and "- raw" not in checklist


def test_ticket_page_scans_tickets_once(dash, ws, put, monkeypatch):
    from orch.core import store
    tid = put("in-progress", sections={"Plan": "- [ ] a"}, blocked_by=["L-9999"])
    dash.get(f"/t/{tid}")  # warm caches
    real, calls = store.scan, []
    monkeypatch.setattr(store, "scan", lambda ws_: calls.append(1) or real(ws_))
    html = dash.get(f"/t/{tid}").text
    assert tid in html and len(calls) == 1


def test_markdown_checkbox_items_are_not_the_task_grid(dash, put):
    """`- [ ]` items in a section are plain list rows: the `.task` class belongs to the Tasks card, whose
    two-column grid squeezed acceptance criteria to one word per line."""
    tid = put("open", sections={"Requirements": "- [ ] All staging models build into stg_acme\n- [x] Old schema kept"})
    html = dash.get(f"/t/{tid}").text
    assert '<li class="md-task">☐ All staging models build into stg_acme' in html
    assert '<li class="md-task done">☑ Old schema kept' in html
    assert 'class="task"' not in html and 'class="task done"' not in html
    tasks_css = dash.get("/static/tasks.css").text
    app_css = dash.get("/static/app.css").text
    assert "md-task" not in tasks_css  # the Tasks grid never reaches markdown checkbox items
    assert re.search(r"li\.md-task \{[^}]*list-style: none", app_css)
    assert not re.search(r"\.md-task[^{]*\{[^}]*grid-template-columns", app_css)


def test_code_panel_does_not_show_the_link_time_draft_placeholder(dash, put):
    """`orch link --pr` stores state "draft" and nothing refreshes it, so a ready PR must not read "draft"."""
    tid = put("in-progress", repos=["dbt"], prs=[{"repo": "dbt", "url": "https://github.com/o/dbt/pull/1", "state": "draft"}])
    code = dash.get(f"/t/{tid}").text.split('id="code"', 1)[1].split("</section>", 1)[0]
    assert ">dbt #1 ↗</a>" in code
    assert "draft" not in code

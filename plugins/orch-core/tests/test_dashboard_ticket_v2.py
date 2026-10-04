import re

import pytest

pytest.importorskip("fastapi")



def _qh(ws, tid, qid="Q1"):
    """The question_hash the answer form posts as qhash (the question as shown)."""
    from orch.core import store
    from orch.core.questions import find_question, question_hash
    return question_hash(find_question(store.load(ws, tid)[1], qid))

def test_step_bar_marks_current(dash, put):
    tid = put("testing", sections={"Verification": "ok"})
    html = dash.get(f"/t/{tid}").text
    assert 'aria-label="Journey"' in html and html.count('aria-current="step"') == 1


def test_ticket_page_keeps_all_actions(dash, put):
    tid = put("open")
    html = dash.get(f"/t/{tid}").text
    for action in ("/comment", "/move", "/artifacts", "/edit"):
        assert f'action="/t/{tid}{action}"' in html or f'href="/t/{tid}{action}"' in html


def test_new_ticket_form_fields(dash):
    html = dash.get("/new").text
    for field in ('name="title"', 'name="type"', 'name="priority"', 'name="size"', 'name="external"', 'name="ask"', 'name="files"'):
        assert field in html
    assert 'type="radio"' in html   # type as a radio group


# ---------- steps() ----------

def _ticket(ws, put, status, approve=(), **meta):
    from orch.core import store
    from orch.core.gates import gate_hash
    sections = meta.pop("sections", {"Requirements": "- r1", "Acceptance criteria": "- [ ] a1", "Plan": "1. do it"})
    tid = put(status, sections=sections, **meta)
    _, t = store.load(ws, tid)
    for gate in approve:
        t.meta.setdefault("gates", {})[gate] = {"approved": "2026-09-30T09:00Z", "via": "dashboard",
                                                "hash": gate_hash(t, gate)}
    return t


def _states(steps):
    return [(s["name"], s["state"]) for s in steps]


def test_steps_backlog_requirements_are_your_turn(ws, put):
    from orch.dashboard.data.steps import steps
    out = steps(_ticket(ws, put, "backlog"))
    assert [s["name"] for s in out] == ["Requirements", "Plan", "Work", "Testing", "Done"]
    assert _states(out)[0] == ("Requirements", "current") and out[0]["note"] == "Your turn"
    assert [s["state"] for s in out[1:]] == ["todo"] * 4


def test_steps_plan_waits_for_human(ws, put):
    from orch.dashboard.data.steps import steps
    out = steps(_ticket(ws, put, "in-progress", approve=("requirements",)))
    assert _states(out)[:3] == [("Requirements", "done"), ("Plan", "current"), ("Work", "todo")]
    assert out[1]["note"] == "Your turn"


def test_steps_plan_skipped_for_small_sizes(ws, put):
    from orch.dashboard.data.steps import steps
    out = steps(_ticket(ws, put, "in-progress", approve=("requirements",), size="xs"), plan_skip_sizes=("xs",))
    assert out[1]["state"] == "skipped" and out[1]["note"] == "skipped"
    assert out[2]["state"] == "current"


def test_steps_testing_and_done(ws, put):
    from orch.dashboard.data.steps import steps
    out = steps(_ticket(ws, put, "testing", approve=("requirements", "plan")))
    assert _states(out) == [("Requirements", "done"), ("Plan", "done"), ("Work", "done"),
                            ("Testing", "current"), ("Done", "todo")]
    assert out[3]["note"] == "Your turn"
    out = steps(_ticket(ws, put, "done", approve=("requirements", "plan")))
    assert [s["state"] for s in out] == ["done"] * 4 + ["current"]


@pytest.mark.parametrize("status,approve,current", [
    ("backlog", (), "Requirements"),
    ("open", (), "Plan"),
    ("open", ("requirements",), "Plan"),
    ("in-progress", (), "Plan"),
    ("in-progress", ("requirements",), "Plan"),
    ("waiting", ("requirements", "plan"), "Work"),
    ("testing", (), "Testing"),
    ("done", (), "Done"),
])
def test_steps_follow_the_status(ws, put, status, approve, current):
    from orch.dashboard.data.steps import steps
    out = steps(_ticket(ws, put, status, approve=approve))
    assert [s["name"] for s in out if s["state"] == "current"] == [current]
    names = [s["name"] for s in out]
    assert all(s["state"] == "done" for s in out[:names.index(current)])
    assert all(s["state"] == "todo" for s in out[names.index(current) + 1:])


def test_steps_in_progress_plan_pending_is_your_turn(ws, put):
    from orch.dashboard.data.steps import steps
    out = steps(_ticket(ws, put, "in-progress"))
    assert out[0]["state"] == "done" and out[1]["state"] == "current" and out[1]["note"] == "Your turn"


@pytest.mark.parametrize("status", ["backlog", "open", "in-progress", "waiting", "testing", "done"])
def test_page_has_exactly_one_current_step(dash, put, status):
    tid = put(status)
    assert dash.get(f"/t/{tid}").text.count('aria-current="step"') == 1


def test_steps_backlog_with_only_an_ask_is_not_your_turn(ws, put):
    from orch.dashboard.data.steps import steps
    out = steps(_ticket(ws, put, "backlog", sections={"Ask": "please"}))
    assert out[0]["state"] == "current" and out[0]["note"] != "Your turn"


def test_steps_backlog_with_open_blocking_question_is_not_your_turn(ws, put):
    from orch.core.questions import build_questions
    from orch.dashboard.data.steps import steps
    q = build_questions([{"text": "Which schema?", "options": [{"key": "A", "label": "ops"}, {"key": "B", "label": "tmp"}]}],
                        [], "2026-09-30T09:00Z")[0]
    out = steps(_ticket(ws, put, "backlog", questions=[q]))
    assert out[0]["state"] == "current" and out[0]["note"] != "Your turn"


def test_steps_testing_note_only_while_testing(ws, put):
    from orch.dashboard.data.steps import steps
    for status in ("backlog", "open", "in-progress", "waiting", "done"):
        assert steps(_ticket(ws, put, status))[3]["note"] == ""
    assert steps(_ticket(ws, put, "testing"))[3]["note"] == "Your turn"


def test_release_asks_and_times_are_local(dash, put):
    from orch.clock import parse_stamp
    from orch.dashboard.views import local
    tid = put("open", claim={"harness": "claude-code", "session": "7f3a0000", "at": "2026-09-30T09:00Z"})
    html = dash.get(f"/t/{tid}").text
    # a stale claim: in the status card, confirmed in a dialog (careful tier), never a browser popup
    assert f'data-dialog="Release claude-code\'s claim on {tid}?"' in html
    assert f'title="{local(parse_stamp("2026-09-30T09:00Z"))}"' in html


def test_steps_invalidated_gate_is_not_done(ws, put):
    from orch.dashboard.data.steps import steps
    t = _ticket(ws, put, "backlog", approve=("requirements",))
    t.set_section("Requirements", "- changed")
    out = steps(t)
    assert out[0]["state"] == "current" and out[0]["note"] == "Your turn"


def test_step_bar_on_page_names_the_current_step(dash, ws, put):
    from orch.core import store
    t = _ticket(ws, put, "testing", approve=("requirements", "plan"))
    store.save(ws, t)
    html = dash.get(f"/t/{t.id}").text
    bar = html.split('aria-label="Journey"', 1)[1].split("</ol>", 1)[0]
    current = re.findall(r'<li[^>]*aria-current="step"[^>]*>(.*?)</li>', bar, re.S)
    assert len(current) == 1 and "Proven" in current[0] and "Your turn" in current[0]


# ---------- ticket page keeps every capability ----------

def test_ticket_page_keeps_forms_and_links(dash, ws, put, aops, tmp_path):
    from orch.core.questions import build_questions
    q = build_questions([{"text": "Which schema?", "options": [{"key": "A", "label": "ops"}, {"key": "B", "label": "tmp"}],
                          "recommended": "A"}], [], "2026-09-30T09:00Z")[0]
    tid = put("backlog", sections={"Ask": "Please **do it**."}, questions=[q],
              external=[{"key": "ABC-1", "url": "https://jira.example/ABC-1"}],
              branches={"dbt": "feature/x"}, prs=[{"repo": "dbt", "state": "open", "url": "https://git.example/pr/1"}],
              claim={"harness": "claude-code", "session": "7f3a0000", "at": "2026-09-30T09:00Z"})
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    aops.artifact_add(tid, img)
    html = dash.get(f"/t/{tid}").text
    # the blocking question means Ops.approve would refuse the requirements, so no Approve form (fix round 1)
    assert f'action="/t/{tid}/approve"' not in html
    for needle in (f'action="/t/{tid}/answer"',
                   'name="qid"', 'name="value"', 'name="note"', f'action="/t/{tid}/release"', f'action="/t/{tid}/comment"', 'name="text"', f'action="/t/{tid}/artifacts"',
                   'name="files"', f'href="/t/{tid}/raw"', f'href="/t/{tid}/edit"', "<strong>do it</strong>",
                   "ABC-1", "https://jira.example/ABC-1", "feature/x", "https://git.example/pr/1", f"/a/{tid}/shot.png",
                   "claude-code"):
        assert needle in html, needle


def test_ticket_page_move_select(dash, put):
    tid = put("open")
    html = dash.get(f"/t/{tid}").text
    assert f'action="/t/{tid}/move"' in html and '<select id="move-to" name="to"' in html and "<option>" in html


def test_ticket_page_verdict_forms(dash, put):
    tid = put("testing")
    html = dash.get(f"/t/{tid}").text
    assert f'action="/t/{tid}/verdict"' in html and 'value="done"' in html and 'value="follow-up"' in html
    assert 'name="message"' in html


def test_ticket_title_is_escaped(dash, put):
    tid = put("open", title='<b>x&"</b>')
    html = dash.get(f"/t/{tid}").text
    assert "<b>x&" not in html and "&lt;b&gt;x&amp;" in html


# ---------- new ticket ----------

def test_new_ticket_type_radio_checked_and_aside(dash):
    html = dash.get("/new").text
    assert "<legend>Type</legend>" in html
    assert re.search(r'<input[^>]*type="radio"[^>]*name="type"[^>]*value="feature"[^>]*checked', html)
    assert "What happens next" in html and "Create ticket" in html and "PR" in html


def test_new_ticket_review_term_mr(configure, ws):
    from fastapi.testclient import TestClient
    from orch.core.workspace import Workspace
    from orch.dashboard.app import create_app
    configure(git={"review_term": "MR"})
    c = TestClient(create_app(Workspace.open(ws.root), "tok"))
    html = c.get("/new?token=tok", follow_redirects=True).text
    aside = html.split("What happens next", 1)[1]
    assert "MR" in aside and " PR" not in aside


def test_new_ticket_keeps_values_on_error(dash):
    r = dash.post("/new", data={"title": "   ", "type": "bug", "size": "s", "priority": "high"})
    assert r.status_code == 422
    assert re.search(r'value="bug"[^>]*checked', r.text) and "title must not be empty" in r.text


def test_edit_raw_error_pages_restyled(dash, ws, put):
    tid = put("open")
    edit = dash.get(f"/t/{tid}/edit").text
    assert f'action="/t/{tid}/edit"' in edit and 'name="mtime"' in edit and 'name="text"' in edit
    assert 'class="page-head"' in edit
    raw = dash.get(f"/t/{tid}/raw").text
    assert 'class="page-head"' in raw and f'href="/t/{tid}/edit"' in raw
    err = dash.get("/t/L-0404")
    assert err.status_code == 404 and 'class="page-head"' in err.text


def test_answered_stamp_shown_in_local_time(dash, ws, put, aops):
    from orch.core import store
    from orch.dashboard.views import local
    tid = put("open")
    aops.ask(tid, [{"text": "Which schema?", "type": "text"}])
    assert dash.post(f"/t/{tid}/answer", data={"qhash": _qh(aops.ws, tid), "qid": "Q1", "value": "raw"}, follow_redirects=False).status_code == 303
    _, t = store.load(ws, tid)
    raw = t.meta["questions"][0]["answered"]
    html = dash.get(f"/t/{tid}").text
    assert f", {raw})" not in html and f", {local(raw)})" in html


def test_local_filter_accepts_stamp_strings():
    from datetime import datetime, timezone
    from orch.dashboard.views import local
    assert local("2026-10-01T08:00:00Z") == datetime(2026, 10, 1, 8, tzinfo=timezone.utc).astimezone().strftime("%d.%m. %H:%M")
    assert local("yesterday-ish") == "yesterday-ish" and local(None) == "" and local("") == ""


def test_log_section_says_times_are_utc(dash, put):
    tid = put("open")
    assert "Times in the log are UTC" in dash.get(f"/t/{tid}").text


def test_activity_uses_timeline_wording(dash, ws, aops):
    t = aops.new("Backup")
    aops.log(t.id, "started on it")
    html = dash.get(f"/t/{t.id}").text
    activity = html[html.index('class="timeline activity"'):]
    assert "<b>claude-code</b>" in activity and "logged: started on it" in activity
    assert "agent:claude-code" not in activity and "log.added" not in activity


def test_sections_come_before_aside_in_source(dash, put):
    tid = put("open")
    html = dash.get(f"/t/{tid}").text
    # story page (#16): h1 → steps → status card → chapters (code in Doing) → aside with timeline, links, files
    assert (html.index("<h1") < html.index('aria-label="Journey"') < html.index('class="status-card')
            < html.index('id="asked"') < html.index('id="agreed"') < html.index('id="doing"') < html.index('id="code"')
            < html.index('id="proven"') < html.index('id="left"') < html.index("<aside"))
    assert html.index("<aside") < html.index('id="artifacts"') < html.index('id="log"')  # M: Artifacts panel first

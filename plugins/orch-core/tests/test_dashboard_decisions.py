import re
import shutil

import pytest

pytest.importorskip("fastapi")



def _qh(ws, tid, qid="Q1"):
    """The question_hash the answer form posts as qhash (the question as shown)."""
    from orch.core import store
    from orch.core.questions import find_question, question_hash
    return question_hash(find_question(store.load(ws, tid)[1], qid))

def test_verdict_card_has_accept_and_send_back(dash, put):
    tid = put("testing", title="Fix duplicates", sections={"Verification": "0 duplicates in 1.2 M rows"})
    html = dash.get("/").text
    assert "Fix duplicates" in html and "0 duplicates in 1.2 M rows" in html
    assert f'action="/t/{tid}/verdict"' in html
    assert 'name="verdict" value="done"' in html and 'name="verdict" value="follow-up"' in html


def test_accept_from_decisions_marks_done(dash, ws, put):
    from orch.core import store
    from orch.core.epics import verdict_hash
    tid = put("testing", sections={"Verification": "ok"})
    r = dash.post(f"/t/{tid}/verdict", data={"verdict": "done"}, follow_redirects=False)
    assert store.resolve(ws, tid).status == "testing"  # final review I2: a verdict binds what the card showed
    r = dash.post(f"/t/{tid}/verdict", data={"verdict": "done", "seen": verdict_hash([store.load(ws, tid)[1]], ws)},
                  follow_redirects=False)
    assert r.status_code == 303
    assert store.resolve(ws, tid).status == "done"


def test_plan_card_posts_gate(dash, ws, put, configure):
    tid = put("in-progress", size="l", sections={"Plan": "1. Step one\n2. Step two", "Requirements": "r", "Acceptance criteria": "a"})
    html = dash.get("/").text
    if f'action="/t/{tid}/approve"' not in html:
        pytest.fail("approve-plan card missing; check plan_required / gate state for a size l in-progress ticket")
    # the full plan, rendered as Markdown (#20), not a plain-text excerpt
    assert 'name="gate" value="plan"' in html and "<li>Step one</li>" in html and "<li>Step two</li>" in html


def test_question_without_options_gets_text_field(ws, put, aops, dash):
    tid = put("open")
    aops.ask(tid, [{"text": "Which schema?", "type": "text"}])
    html = dash.get("/").text
    assert f'action="/t/{tid}/answer"' in html and 'name="value"' in html and 'name="qid"' in html


def test_question_with_options_shows_recommended_first_as_buttons(ws, put, aops, dash):
    tid = put("open")
    aops.ask(tid, [{
        "text": "Which database?",
        "type": "single",
        "options": [{"key": "pg", "label": "Postgres"}, {"key": "mysql", "label": "MySQL"}],
        "recommended": "pg",
    }])
    html = dash.get("/").text
    assert f'action="/t/{tid}/answer"' in html
    assert 'name="value" value="pg"' in html and 'name="value" value="mysql"' in html
    assert '<span class="opt-n">pg</span> · Postgres</button> <span class="chip chip-ok"><svg class="i" aria-hidden="true"><use href="#i-ok"/></svg> recommended</span>' in html
    # the recommended option comes first
    assert html.index('value="pg"') < html.index('value="mysql"')


def test_question_multi_shows_checkboxes_and_one_submit(ws, put, aops, dash):
    tid = put("open")
    aops.ask(tid, [{
        "text": "Which environments?",
        "type": "multi",
        "options": [{"key": "dev", "label": "Dev"}, {"key": "prod", "label": "Prod"}],
        "recommended": "dev",
    }])
    html = dash.get("/").text
    assert f'action="/t/{tid}/answer"' in html
    assert 'type="checkbox"' in html and 'name="value" value="dev"' in html and 'name="value" value="prod"' in html
    assert '<use href="#i-ok"/></svg> recommended' in html and html.count("Send answer</button>") == 1


def test_question_option_labels_shown_with_auto_keys(ws, put, aops, dash):
    """Options given as plain strings get auto keys A/B; the button shows the label, posts the key."""
    from orch.core import store
    tid = put("open")
    aops.ask(tid, [{
        "text": "Which date format?",
        "type": "single",
        "options": ["ISO 8601", "Swiss format"],
        "recommended": "A",
    }])
    html = dash.get("/").text
    assert "ISO 8601" in html and "Swiss format" in html
    assert 'name="value" value="A"' in html and 'name="value" value="B"' in html
    r = dash.post(f"/t/{tid}/answer", data={"qhash": _qh(aops.ws, tid), "qid": "Q1", "value": ["", "B"]}, follow_redirects=False)
    assert r.status_code == 303
    _, t = store.load(ws, tid)
    assert t.meta["questions"][0]["answer"] == "B"


def test_old_kind_filter_is_ignored(dash, put):
    """Today is one ordered list now (#17): an old ?kind= bookmark still shows everything."""
    put("testing", title="Needs verdict", sections={"Verification": "ok"})
    for kind in ("questions", "verdicts", "<x>"):
        assert "Needs verdict" in dash.get(f"/?kind={kind}").text


def test_broken_ticket_shows_repair_card(dash, ws, put):
    from orch.core import store
    tid = put("open")
    store.resolve(ws, tid).path.write_text("---\nnot: [valid\n---\n", encoding="utf-8")
    html = dash.get("/").text
    assert "Repair" in html and tid in html


def test_empty_workspace_says_nothing_waits(dash):
    assert "Nothing is waiting on you" in dash.get("/").text


def test_done_per_weekday_shape(ws):
    from orch.dashboard.data.metrics import done_per_weekday
    days = done_per_weekday(ws)
    assert [d["d"] for d in days] == ["Mon", "Tue", "Wed", "Thu", "Fri"] and all(d["n"] == 0 for d in days)


# ---------- Duplicate ticket ids never crash Decisions ----------

def _duplicate(ws, put, status="testing", **meta):
    from orch.core import store
    tid = put(status, title="x", **meta)
    path = store.resolve(ws, tid).path
    assert path.name == f"{tid}-x.md"
    shutil.copy(path, path.with_name(f"{tid}-x-dup.md"))
    return tid


@pytest.mark.parametrize("status,meta", [
    ("testing", {"sections": {"Verification": "ok"}}),
    ("backlog", {"sections": {"Requirements": "r", "Acceptance criteria": "a"}}),
])
def test_duplicate_ids_show_a_repair_card(dash, ws, put, status, meta):
    tid = _duplicate(ws, put, status, **meta)
    r = dash.get("/")
    assert r.status_code == 200
    assert "Repair" in r.text and "is ambiguous" in r.text and f"{tid}-x-dup.md" in r.text
    # one card for the id, not one per file, and no action that would fail on the ambiguous id
    assert r.text.count('<article class="decision-card decision') == 1
    assert f'action="/t/{tid}/verdict"' not in r.text and f'action="/t/{tid}/approve"' not in r.text


def test_duplicate_ids_do_not_break_other_pages(dash, ws, put):
    _duplicate(ws, put, "in-progress", claim={"harness": "claude-code", "session": "s1", "at": "2026-01-01T00:00:00Z"})
    for url in ("/board", "/board?view=list", "/activity", "/reports"):
        assert dash.get(url).status_code == 200, url

def test_done_per_weekday_counts_distinct_tickets_per_day(ws):
    from datetime import datetime, timezone
    from orch.core.events import Event
    from orch.dashboard.data.metrics import done_per_weekday
    now = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)  # a Wednesday
    def done(seq, tid, at):
        return Event(seq, at, tid, "ticket.moved", "human:you", "dashboard", {"from": "testing", "to": "done"})
    events = [done(1, "L-0001", "2026-09-30T08:00:00Z"), done(2, "L-0001", "2026-09-30T09:00:00Z"),  # reopened, done again
              done(3, "L-0002", "2026-09-30T10:00:00Z"), done(4, "L-0003", "2026-09-29T10:00:00Z")]
    days = {d["d"]: d["n"] for d in done_per_weekday(ws, now=now, events=events)}
    assert days == {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 0, "Fri": 0}


def test_decisions_page_scans_and_reads_once(dash, ws, put, aops, monkeypatch):
    from orch.core import events as events_mod
    from orch.core import query, store
    put("testing", sections={"Verification": "ok"})
    put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    q = put("in-progress", claim={"harness": "claude-code", "session": "s1", "at": "2026-01-01T00:00:00Z"})
    aops.ask(q, [{"text": "Which schema?", "type": "text"}])
    calls = {"scan": 0, "read_events": 0, "needs_you": 0, "load": 0}
    for mod, name in ((store, "scan"), (events_mod, "read_events"), (query, "needs_you"), (store, "load")):
        orig = getattr(mod, name)
        def wrapped(*a, _orig=orig, _name=name, **k):
            calls[_name] += 1
            return _orig(*a, **k)
        monkeypatch.setattr(mod, name, wrapped)
    html = dash.get("/").text
    assert "Which schema?" in html
    assert calls == {"scan": 1, "read_events": 1, "needs_you": 1, "load": 0}


def test_accept_asks_for_confirmation_like_the_ticket_page(dash, put):
    tid = put("testing", sections={"Verification": "ok"})
    html = dash.get("/").text
    assert f'data-inline-confirm="Confirm · close {tid} as done"' in html and "data-confirm=" not in html
    assert f'data-inline-confirm="Confirm · close {tid} as done"' in dash.get(f"/t/{tid}").text  # no browser popup


def test_other_answer_has_its_own_form(dash, put, aops):
    import re
    tid = put("open")
    aops.ask(tid, [{"text": "Which database?", "type": "single",
                    "options": [{"key": "pg", "label": "Postgres"}, {"key": "mysql", "label": "MySQL"}]}])
    html = dash.get("/").text
    forms = re.findall(rf'<form method="post" action="/t/{tid}/answer".*?</form>', html, re.S)
    assert len(forms) == 2
    buttons, other = forms
    assert 'value="pg"' in buttons and "<input name=\"value\"" not in buttons  # option buttons never send free text
    assert "Other answer" in other and 'value="pg"' not in other and 'type="submit"' in other


def test_each_decision_card_has_a_heading(dash, put):
    tid = put("testing", title="Fix duplicates", sections={"Verification": "ok"})
    html = dash.get("/").text
    assert re.search(rf'aria-labelledby="(dh-{tid}-verdict)".*?<h3 id="\1">Fix duplicates</h3>', html, re.S)


def test_today_has_no_done_per_day_chart(dash, put):
    put("testing", sections={"Verification": "ok"})
    assert "Done per day" not in dash.get("/").text


def test_excerpt_strips_inline_markdown():
    from orch.dashboard.data.decisions import _excerpt
    assert _excerpt("- Run `orch check` on **all** repos\n- keep *snake_case* names, file_name.py stays") == \
        "Run orch check on all repos\nkeep snake_case names, file_name.py stays"
    assert _excerpt("2 * 3 = 6, _private_ stays a word") == "2 * 3 = 6, private stays a word"

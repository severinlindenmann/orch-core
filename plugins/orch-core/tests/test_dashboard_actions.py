from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")

from orch.core import gates, store
from orch.core.events import read_events
from orch.core.questions import parse_ask_file



def _qh(ws, tid, qid="Q1"):
    """The question_hash the answer form posts as qhash (the question as shown)."""
    from orch.core import store
    from orch.core.questions import find_question, question_hash
    return question_hash(find_question(store.load(ws, tid)[1], qid))

def _vh(ws, tid):
    """The verdict hash the verdict forms post as seen (criteria, Verification and status as shown)."""
    from orch.core.epics import verdict_hash
    return verdict_hash([store.load(ws, tid)[1]], ws)


def _refined(aops, size="m"):
    t = aops.new("Backup", size=size)
    aops.set_section(t.id, "Requirements", "Nightly backup.")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] job runs nightly")
    return t.id


def test_full_human_flow_through_dashboard(dash, ws, aops, close_tasks):
    tid = _refined(aops)
    aops.ask(tid, parse_ask_file("questions:\n  - text: One repo?\n    options: [yes please, no]\n    recommended: A\n"))
    r = dash.post(f"/t/{tid}/answer", data={"qhash": _qh(aops.ws, tid), "qid": "Q1", "value": "A", "note": "fine"})
    assert r.status_code == 200 and "answered Q1" in r.text
    seen = gates.gate_hash(store.load(ws, tid)[1], "requirements")
    r = dash.post(f"/t/{tid}/approve", data={"gate": "requirements", "seen": seen})
    assert "requirements approved" in r.text
    assert store.resolve(ws, tid).status == "open"
    aops.claim(tid)
    aops.set_section(tid, "Plan", "1. write job")
    seen = gates.gate_hash(store.load(ws, tid)[1], "plan")
    dash.post(f"/t/{tid}/approve", data={"gate": "plan", "seen": seen})
    aops.set_section(tid, "Verification", "dbt build ok")
    close_tasks(aops, tid)
    aops.move(tid, "testing")
    r = dash.post(f"/t/{tid}/verdict", data={"verdict": "follow-up", "message": "alert missing", "seen": _vh(ws, tid)})
    assert store.resolve(ws, tid).status == "in-progress" and "verdict follow-up" in r.text
    aops.move(tid, "testing")
    dash.post(f"/t/{tid}/verdict", data={"verdict": "done", "seen": _vh(ws, tid)})
    assert store.resolve(ws, tid).status == "done"
    humans = [e for e in read_events(ws, tid) if e.actor == "human:you"]
    assert {e.via for e in humans} == {"dashboard"}
    assert [e.kind for e in humans] == ["question.answered", "gate.approved", "gate.approved", "verdict.given", "verdict.given"]


def test_rule_violations_come_back_as_messages(dash, ws, aops):
    t = aops.new("empty")
    seen = gates.gate_hash(t, "requirements")
    r = dash.post(f"/t/{t.id}/approve", data={"gate": "requirements", "seen": seen})
    assert r.status_code == 200 and "cannot approve" in r.text and 'class="flash err"' in r.text
    r = dash.post(f"/t/{t.id}/move", data={"to": "done"})
    assert "not an allowed transition" in r.text
    assert dash.post("/t/L-0404/comment", data={"text": "hi"}).status_code == 200  # redirected with an error


def test_move_comment_release(dash, ws, aops, put):
    tid = put("open")
    aops.claim(tid)
    dash.post(f"/t/{tid}/comment", data={"text": "please check the export too"})
    dash.post(f"/t/{tid}/release")
    dash.post(f"/t/{tid}/move", data={"to": "backlog"})
    _, t = store.load(ws, tid)
    assert t.status == "backlog" and t.meta["claim"]["session"] is None
    assert "[you] please check the export too" in t.section("Log")


def test_multi_answer(dash, ws, aops):
    tid = _refined(aops)
    aops.ask(tid, [{"text": "Which envs?", "type": "multi", "options": ["dev", "test", "prod"]}])
    dash.post(f"/t/{tid}/answer", data={"qhash": _qh(aops.ws, tid), "qid": "Q1", "value": ["A", "C"]})
    assert store.load(ws, tid)[1].meta["questions"][0]["answer"] == ["A", "C"]


def test_upload_artifacts(dash, ws, put):
    tid = put("open")
    r = dash.post(f"/t/{tid}/artifacts", files=[("files", ("a.png", b"png", "image/png")),
                                                 ("files", ("../b.txt", b"txt", "text/plain"))])
    assert r.status_code == 200 and "2 file(s) added" in r.text
    assert (ws.artifacts_dir / tid / "a.png").read_bytes() == b"png"
    assert (ws.artifacts_dir / tid / "b.txt").exists()
    assert list(ws.temporary_dir.iterdir()) == []


def test_raw_edit_and_conflict(dash, ws, put):
    tid = put("backlog")
    form = dash.get(f"/t/{tid}/edit")
    assert form.status_code == 200 and 'name="mtime"' in form.text
    path = store.resolve(ws, tid).path
    mtime = str(path.stat().st_mtime_ns)
    text = path.read_text(encoding="utf-8") + "\n## Ask\n\nClarified.\n"  # a new ticket has no empty Ask heading
    r = dash.post(f"/t/{tid}/edit", data={"text": text, "mtime": mtime})
    assert r.status_code == 200 and "Clarified." in r.text
    stale = dash.post(f"/t/{tid}/edit", data={"text": text + "\nmore\n", "mtime": mtime})
    assert stale.status_code == 409 and "changed since you opened it" in stale.text and "more" in stale.text
    fresh = store.resolve(ws, tid).path
    forged = fresh.read_text(encoding="utf-8").replace("status: backlog", "status: done")
    bad = dash.post(f"/t/{tid}/edit", data={"text": forged, "mtime": str(fresh.stat().st_mtime_ns)})
    assert bad.status_code == 409 and "dashboard buttons" in bad.text


def test_multi_answer_with_nothing_ticked_gives_a_message(dash, ws, aops):
    tid = _refined(aops)
    aops.ask(tid, [{"text": "Which envs?", "type": "multi", "options": ["dev", "test", "prod"]}])
    r = dash.post(f"/t/{tid}/answer", data={"qhash": _qh(aops.ws, tid), "qid": "Q1"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert 'class="flash err"' in r.text and '"detail"' not in r.text
    assert store.load(ws, tid)[1].meta["questions"][0].get("answer") in (None, "")


def test_missing_form_field_redirects_with_a_message(dash, ws, put):
    tid = put("open")
    r = dash.post(f"/t/{tid}/comment", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/t/{tid}?")
    r = dash.post(f"/t/{tid}/comment", data={})
    assert r.status_code == 200 and "some form fields were missing or invalid" in r.text
    assert '"detail"' not in r.text


def test_validation_error_goes_back_to_same_origin_referer(dash, put):
    tid = put("open")
    r = dash.post("/new", data={}, headers={"referer": "http://testserver/new"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/new?err=")
    r = dash.post("/new", data={}, headers={"referer": "http://evil.example/x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?err=")


def test_validation_error_never_redirects_off_site(dash):
    r = dash.post("/new", data={}, headers={"referer": "http://testserver//evil.example/x"}, follow_redirects=False)
    assert r.headers["location"].startswith("/?err=")


# ---------- `next`: actions from Decisions return to Decisions ----------

def _location(r):
    assert r.status_code == 303
    return r.headers["location"]


def test_answer_success_returns_to_decisions(dash, put, aops):
    tid = put("open")
    aops.ask(tid, [{"text": "Which schema?", "type": "text"}])
    loc = _location(dash.post(f"/t/{tid}/answer", data={"qhash": _qh(aops.ws, tid), "qid": "Q1", "value": "raw", "next": "/?kind=questions"},
                              follow_redirects=False))
    parts = urlsplit(loc)
    assert parts.path == "/" and parse_qs(parts.query) == {"kind": ["questions"], "msg": ["answered Q1"]}
    assert loc.startswith("/?kind=questions&msg=")


def test_action_error_returns_to_decisions(dash, put):
    tid = put("backlog")  # no requirements: approving fails
    loc = _location(dash.post(f"/t/{tid}/approve", data={"gate": "requirements", "next": "/?kind=questions"},
                              follow_redirects=False))
    assert loc.startswith("/?kind=questions&err=")


@pytest.mark.parametrize("route,data", [
    ("approve", {"gate": "requirements"}),
    ("verdict", {"verdict": "done"}),
    ("move", {"to": "open"}),
    ("release", {}),
    ("comment", {"text": "hi"}),
])
def test_every_action_route_honours_next(dash, put, route, data):
    tid = put("testing", sections={"Verification": "ok", "Requirements": "r", "Acceptance criteria": "a"})
    loc = _location(dash.post(f"/t/{tid}/{route}", data={**data, "next": "/?kind=all"}, follow_redirects=False))
    assert loc.startswith("/?kind=all&")


@pytest.mark.parametrize("bad", ["//evil.com", "/\\evil.com", "https://evil.com", "/%0d%0aX", "/\r\nX",
                                 "evil.com", "/%2F%2Fevil.com/..", "javascript:alert(1)", ""])
def test_unsafe_next_falls_back_to_ticket(dash, put, bad):
    tid = put("backlog")
    loc = _location(dash.post(f"/t/{tid}/comment", data={"text": "hi", "next": bad}, follow_redirects=False))
    assert loc.startswith(f"/t/{tid}?msg="), loc


def test_back_merges_query_strings():
    from orch.dashboard.views import back
    assert back("/t/L-0001", msg="ok").headers["location"] == "/t/L-0001?msg=ok"
    assert back("/?kind=questions", err="no way").headers["location"] == "/?kind=questions&err=no+way"
    assert back("/?kind=all&msg=old", msg="new").headers["location"] == "/?kind=all&msg=new"
    assert back("/board").headers["location"] == "/board"


def test_decision_forms_send_next(dash, put, aops):
    tid = put("testing", sections={"Verification": "ok"})
    q = put("open")
    aops.ask(q, [{"text": "Which schema?", "type": "text"}])
    html = dash.get("/").text
    assert html.count('name="next" value="/"') >= 3  # accept, send back, answer
    assert f'action="/t/{tid}/verdict"' in html

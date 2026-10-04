"""#45: a blocking question with option keys out of order (B, A, C), the recommended option first and text holding a
quoted `?` and an apostrophe is answered from every dashboard surface that posts an answer (Today, the Board's Your
move strip, the ticket page). Each test reads the form the page rendered (qid, qhash, the option's value) and posts
exactly that, as the browser does after the Undo window; the answer must be stored, signed into the ledger, the
blocked task restarted and the ticket back in progress. The browser side (the delayed send itself) is covered by
tests/js/delayed_send.js."""
import html as htmlmod
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from orch.core import ledger, store
from orch.core import tasks as tk
from orch.core.events import read_events
from orch.core.questions import find_question, parse_ask_file, question_hash

ROOT = Path(__file__).resolve().parents[1]

ASK = '''questions:
  - text: "The parser drops it: \\"what's a name?\\" — How should a bare typed name be kept?"
    why: "It's the owner's call: the 'name' field isn't validated?"
    options:
      - {key: B, label: "Keep it as typed ('as is')", cost: "none?"}
      - {key: A, label: "Normalise it", cost: "a migration"}
      - {key: C, label: "Reject it", cost: "users' re-entry"}
    recommended: B
'''


@pytest.fixture
def blocked(aops, working, plan_approved):
    aops.task_add(working, [{"text": "keep names"}, {"text": "other"}])
    plan_approved(working)
    aops.task_start(working, "T1")
    t, _ = aops.ask(working, parse_ask_file(ASK))
    assert t.status == "waiting"
    assert [e for e in read_events(aops.ws, working) if e.kind == "question.asked"][-1].data["task_blocked"] == "T1"
    return working


def _answer_forms(page: str, tid: str) -> list[str]:
    return re.findall(rf'<form method="post" action="/t/{tid}/answer".*?</form>', page, re.S)


def _hidden(form: str, name: str) -> str:
    m = re.search(rf'<input type="hidden" name="{name}" value="([^"]*)"', form)
    assert m, f"no {name} in the form"
    return htmlmod.unescape(m.group(1))


def _option_value(form: str, key: str) -> str:
    values = [htmlmod.unescape(v) for v in re.findall(r'name="value" value="([^"]*)"', form)]
    assert key in values, values
    return key


def _assert_answered(ws, tid, key="B"):
    t = store.load(ws, tid)[1]
    q = find_question(t, "Q1")
    assert q["answer"] == key
    assert t.status == "in-progress"
    assert tk.find(tk.ticket_tasks(t), "T1").state == "doing"
    answered = [e for e in read_events(ws, tid) if e.kind == "question.answered"]
    assert answered and {k: answered[-1].data.get(k) for k in ("qid", "answer", "task_restarted", "to")} == \
        {"qid": "Q1", "answer": key, "task_restarted": "T1", "to": "in-progress"}
    signed = [e for e in ledger.entries(ws) if e.get("ticket") == tid and e.get("kind") == "answer"]
    assert signed and signed[-1]["answer"] == key and signed[-1]["question_hash"] == question_hash(q)


@pytest.mark.parametrize("surface", ["/", "/board", "ticket"])
def test_answer_posted_from_each_surface_is_recorded(dash, ws, blocked, surface):
    url = f"/t/{blocked}" if surface == "ticket" else surface
    page = dash.get(url).text
    forms = _answer_forms(page, blocked)
    assert forms, f"no answer form on {url}"
    form = forms[0]
    assert 'data-delayed-send="Sending your answer to ' in form
    qhash = _hidden(form, "qhash")
    assert qhash == question_hash(find_question(store.load(ws, blocked)[1], "Q1"))
    data = {"qid": _hidden(form, "qid"), "qhash": qhash, "value": _option_value(form, "B")}
    if 'name="next"' in form:
        data["next"] = _hidden(form, "next")
    r = dash.post(f"/t/{blocked}/answer", data=data)
    assert r.status_code == 200 and "err=" not in str(r.url), r.url
    _assert_answered(ws, blocked)


def test_recommended_option_is_first_and_keys_keep_their_letters(dash, blocked):
    form = _answer_forms(dash.get("/").text, blocked)[0]
    assert re.findall(r'name="value" value="([^"]*)"', form) == ["B", "A", "C"]


def test_a_refused_answer_names_the_reason(dash, ws, blocked):
    """The server never refuses silently: a stale qhash comes back as err= on the redirect the JS reads."""
    r = dash.post(f"/t/{blocked}/answer", data={"qid": "Q1", "qhash": "sha256:" + "0" * 64, "value": "B",
                                                "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    assert find_question(store.load(ws, blocked)[1], "Q1")["answer"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_delayed_send_js_posts_after_the_undo_window():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "delayed_send.js"),
                        str(ROOT / "src" / "orch" / "dashboard" / "static" / "app.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "delayed send ok" in r.stdout

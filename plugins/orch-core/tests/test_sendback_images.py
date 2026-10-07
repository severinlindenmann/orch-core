"""Send back with text, criteria and pasted images (issue #202): the dashboard route stores the images as feedback
artifacts, the event and Log line name them, and `orch wait --json` hands the agent message, criteria and paths."""
import json

import pytest

pytest.importorskip("fastapi")

from orch.cli import run
from orch.core import store
from orch.core.epics import verdict_hash
from orch.core.events import read_events

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8


def _testing(put):
    return put("testing", sections={"Acceptance criteria": "- [ ] one\n- [ ] two", "Verification": "- AC1: ok\n- AC2: ok"})


def _send(dash, ws, tid, files=(), **data):
    body = {"verdict": "follow-up", "message": "visual is cropped", "seen": verdict_hash([store.load(ws, tid)[1]], ws), **data}
    return dash.post(f"/t/{tid}/verdict", data=body, files=[("images", f) for f in files])


def test_images_are_stored_as_feedback_and_named_in_the_event(dash, ws, put):
    tid = _testing(put)
    r = _send(dash, ws, tid, [("shot.png", PNG, "image/png"), ("two.jpg", JPG, "image/jpeg")], acs=["2", "1"])
    assert "verdict follow-up" in r.text and store.resolve(ws, tid).status == "in-progress"
    t = store.load(ws, tid)[1]
    feedback = [a for a in t.meta["artifacts"] if a["kind"] == "feedback"]
    assert [a["name"] for a in feedback] == ["sendback-1.png", "sendback-2.jpg"] and all(a["sha256"] for a in feedback)
    assert (ws.artifacts_dir / tid / "sendback-1.png").read_bytes() == PNG
    [event] = [e for e in read_events(ws, tid) if e.kind == "verdict.given"]
    assert event.data["acs"] == [1, 2] and event.data["attachments"] == ["sendback-1.png", "sendback-2.jpg"]
    assert "(AC1, AC2) · 2 images: sendback-1.png, sendback-2.jpg" in t.section("Log")


def test_a_later_round_never_overwrites_an_earlier_one(dash, ws, put):
    tid = _testing(put)
    (ws.artifacts_dir / tid).mkdir(parents=True)
    (ws.artifacts_dir / tid / "sendback-1.png").write_bytes(b"round one")
    _send(dash, ws, tid, [("b.jpg", JPG, "image/jpeg")])
    assert [a["name"] for a in store.load(ws, tid)[1].meta["artifacts"]] == ["sendback-2.jpg"]
    assert (ws.artifacts_dir / tid / "sendback-1.png").read_bytes() == b"round one"


def test_a_file_that_is_not_an_image_is_refused_by_its_bytes(dash, ws, put):
    tid = _testing(put)
    r = _send(dash, ws, tid, [("evil.png", b"<script>alert(1)</script>", "image/png")])
    assert "not a PNG, JPEG, GIF or WebP" in r.text
    assert store.resolve(ws, tid).status == "testing" and not (ws.artifacts_dir / tid).exists()


def test_the_name_comes_from_the_bytes_not_the_upload(dash, ws, put):
    tid = _testing(put)
    _send(dash, ws, tid, [("../../x.html", WEBP, "text/html")])
    assert [a["name"] for a in store.load(ws, tid)[1].meta["artifacts"]] == ["sendback-1.webp"]


def test_more_than_eight_images_are_refused(dash, ws, put):
    tid = _testing(put)
    r = _send(dash, ws, tid, [(f"{i}.png", PNG, "image/png") for i in range(9)])
    assert "at most 8 images" in r.text and store.resolve(ws, tid).status == "testing"


def test_a_stale_page_refuses_the_verdict_and_says_the_images_stay(dash, ws, put):
    tid = _testing(put)
    r = dash.post(f"/t/{tid}/verdict", data={"verdict": "follow-up", "message": "m", "seen": "sha256:stale"},
                  files=[("images", ("a.png", PNG, "image/png"))])
    assert "changed since you read them" in r.text and "images stay on the ticket" in r.text
    assert store.resolve(ws, tid).status == "testing"


def test_accept_ignores_images_and_criteria(dash, ws, put):
    tid = _testing(put)
    r = dash.post(f"/t/{tid}/verdict", data={"verdict": "done", "seen": verdict_hash([store.load(ws, tid)[1]], ws), "acs": "1"},
                  files=[("images", ("a.png", PNG, "image/png"))])
    assert store.resolve(ws, tid).status == "done" and "verdict done" in r.text
    assert not any(a["kind"] == "feedback" for a in (store.load(ws, tid)[1].meta.get("artifacts") or []))


def test_a_plain_text_send_back_still_works(dash, ws, put):
    tid = _testing(put)
    _send(dash, ws, tid)
    [event] = [e for e in read_events(ws, tid) if e.kind == "verdict.given"]
    assert "attachments" not in event.data and "acs" not in event.data


def test_request_changes_takes_images_too(dash, ws, aops):
    from orch.core import gates
    t = aops.new("Backup", size="m")
    aops.set_section(t.id, "Requirements", "Nightly backup.")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] job runs nightly")
    seen = gates.gate_hash(store.load(ws, t.id)[1], "requirements")
    r = dash.post(f"/t/{t.id}/request-changes", data={"gate": "requirements", "message": "see mockup", "seen": seen},
                  files=[("images", ("m.png", PNG, "image/png"))])
    assert "asked for changes on requirements" in r.text
    [event] = [e for e in read_events(ws, t.id) if e.kind == "gate.changes_requested"]
    assert event.data["attachments"] == ["sendback-1.png"]


def test_an_agent_cannot_file_feedback_in_a_humans_name(ws, aops, put, tmp_path):
    from orch.errors import OrchError
    tid = _testing(put)
    f = tmp_path / "x.png"
    f.write_bytes(PNG)
    with pytest.raises(OrchError):
        aops.artifact_add(tid, f, kind="feedback")


def test_wait_hands_the_agent_message_criteria_and_paths(dash, ws, put, capsys):
    tid = _testing(put)
    _send(dash, ws, tid, [("a.png", PNG, "image/png")], acs=["1"])
    assert run(["wait", tid, "--after", "0", "--timeout", "2", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["message"] == "visual is cropped" and out["acs"] == [1]
    [att] = out["attachments"]
    assert att["name"] == "sendback-1.png" and att["path"].endswith(f"artifacts/{tid}/sendback-1.png")
    assert len(att["sha256"]) == 64 and (ws.root / att["path"]).read_bytes() == PNG


def test_the_send_back_forms_are_composers_with_criteria_and_an_image_field(dash, ws, put):
    import re
    tid = _testing(put)
    for html in (dash.get("/board").text, dash.get(f"/t/{tid}").text):
        form = re.search(r'<form method="post" action="/t/%s/verdict" enctype="multipart/form-data"[^>]*data-composer[^>]*>(?:(?!</form>).)*name="verdict" value="follow-up".*?</form>' % tid, html, re.S)
        assert form, "no composer form"
        body = form.group(0)
        assert "<textarea" in body and 'name="message"' in body and "data-composer-text" in body
        assert 'type="file" name="images"' in body and 'accept="image/png,image/jpeg,image/gif,image/webp"' in body
        assert 'name="acs" value="1"' in body and 'name="acs" value="2"' in body
        assert "data-delayed-send" in body  # still held for 5 s with Undo


def test_request_changes_composer_has_images_but_no_criteria(dash, aops):
    t = aops.new("Backup", size="m")
    aops.set_section(t.id, "Requirements", "Nightly backup.")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] job runs nightly")
    html = dash.get(f"/t/{t.id}").text
    i = html.index(f'action="/t/{t.id}/request-changes"')
    form = html[i:html.index("</form>", i)]
    assert 'enctype="multipart/form-data"' in html[i - 120:i + 200] and "<textarea" in form
    assert 'name="images"' in form and 'name="acs"' not in form


def test_an_answer_can_carry_a_note_and_images(dash, ws, aops, capsys):
    from orch.core.questions import find_question, parse_ask_file, question_hash
    t = aops.new("Backup", size="m")
    aops.ask(t.id, parse_ask_file("questions:\n  - text: Which one?\n    options: [left, right]\n    recommended: A\n"))
    qh = question_hash(find_question(store.load(ws, t.id)[1], "Q1"))
    r = dash.post(f"/t/{t.id}/answer", data={"qid": "Q1", "qhash": qh, "value": "B", "note": "like the screenshot"},
                  files=[("images", ("m.png", PNG, "image/png"))])
    assert "answered Q1" in r.text
    [event] = [e for e in read_events(ws, t.id) if e.kind == "question.answered"]
    assert event.data["attachments"] == ["sendback-1.png"] and event.data["note"] == "like the screenshot"
    assert run(["wait", t.id, "--after", "0", "--timeout", "2", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["message"] == "like the screenshot" and out["attachments"][0]["name"] == "sendback-1.png"

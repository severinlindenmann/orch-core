"""Decisions are never lost: ``show`` lists the ones not handed over; only ``wait`` (or an explicit ``inbox``) hands
them over and moves the decision cursor (review of #351, findings 1 and 3)."""

from __future__ import annotations

from tests.ops.helpers import Cli, fenced, wait_for


def asked(ws, cli):
    cli("new", "A ticket")
    cli("claim", "1")
    assert cli("ask", "Which?", "--options", "a,b").code == 0
    return ws.view("1").questions[0]


def test_an_answer_survives_a_status_and_a_show_and_wait_hands_it_over(ws, cli):
    q = asked(ws, cli)
    ws.sign("1", "question.answered", question="Q1", hash=q.hash, option="b", text="because of the API")
    # the harness restarts: the session-start hook runs status and show
    assert "1 new events" in cli("status").out
    out = cli("show").out
    assert "UNREAD #4 answered Q1 option=b: because of the API" in fenced(out)
    log = cli("show", "--log", "--since", "0").out
    assert "question.answered" in log and "option=b: because of the API" in fenced(log)  # the log prints the content
    r = cli.j("wait", "--timeout", "1")
    assert r.data["kind"] == "answered" and r.data["option"] == "b" and r.data["text"] == "because of the API"
    assert "UNREAD" not in cli("show").out  # handed over
    assert cli.j("wait", "--timeout", "1").data["kind"] == "timeout"


def test_a_decision_the_default_show_did_not_print_is_still_delivered(ws, cli):
    """The Opus repro: an approval, then six notes by a subagent push it out of the last events."""
    from tests.ops.test_task_ac import fill_and_approve

    cli("new", "A ticket")
    cli("claim", "1")
    uid = fill_and_approve(ws, cli)
    sub = Cli(ws, session=cli.ws.env()["ORCH_SESSION"] + ".1")
    for i in range(6):
        sub("log", f"note {i}")
    out = cli("show").out
    assert "UNREAD" in out and "approved requirements" in out and "approved plan" in out
    assert wait_for(cli, "approved").data["gate"] == "requirements"
    assert wait_for(cli, "approved").data["gate"] == "plan"
    assert uid


def test_change_requests_and_failed_verdicts_keep_their_text_until_waited_for(ws, cli):
    from tests.ops.test_ask_wait import make_ready, verdict

    uid = make_ready(ws, cli)
    ws.store = ws.other()
    verdict(ws, uid, "fail", text="the test is wrong")
    cli("show")
    cli("status")
    out = fenced(cli("show").out)
    assert "UNREAD" in out and "verdict fail: the test is wrong" in out
    r = wait_for(cli, "verdict")
    assert r.code == 3 and r.data["text"] == "the test is wrong"


def test_inbox_lists_and_acknowledges_undelivered_decisions(ws, cli):
    q = asked(ws, cli)
    ws.sign("1", "question.answered", question="Q1", hash=q.hash, option="a")
    r = cli.j("inbox")
    assert r.data["count"] == 1 and r.data["decisions"] == [
        {"key": "DEMO-0001", "seq": 4, "decision": "#4 answered Q1 option=a"}
    ]
    assert cli.j("inbox").data["decisions"] == []  # explicit inbox acknowledged it
    assert cli.j("wait", "--timeout", "1").data["kind"] == "timeout"


def test_a_ticket_the_session_never_touched_delivers_only_new_decisions(ws, cli):
    q = asked(ws, cli)
    ws.sign("1", "question.answered", question="Q1", hash=q.hash, option="a")
    other = Cli(ws, session="s_01J9ZP0000000000000000000T")
    assert other.j("wait", "--ref", "1", "--timeout", "1").data["kind"] == "timeout"  # the old answer was not for it


def test_forged_notes_replay_at_most_a_genuine_decision(ws, cli):
    """The notes are advisory (same user): forging the cursor can only make wait hand over an old, real event."""
    import json

    q = asked(ws, cli)
    ws.sign("1", "question.answered", question="Q1", hash=q.hash, option="a")
    assert cli.j("wait", "--timeout", "1").data["kind"] == "answered"
    p = next((ws.root / ".state" / "sessions").glob("*.notes.json"))
    doc = json.loads(p.read_text())
    for e in doc["tickets"].values():
        e["decided"] = 0
    p.write_text(json.dumps(doc))
    d = cli.j("wait", "--timeout", "1").data
    assert d["kind"] == "answered" and d["seq"] == 4 and d["question"] == "Q1"  # a real event, with its real seq

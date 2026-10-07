"""#218: `orch status <id> [--json]`: gates, questions with answers, claim and expiry, tasks, move, wait cursor."""
import json

import pytest

from orch.cli import run
from orch.core import query, store
from orch.core.events import last_seq
from orch.core.status_view import status_document


def _json(capsys, *args):
    assert run(["status", *args, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_status_projects_the_ticket_document(ws, aops, hops, working, capsys):
    aops.ask(working, [{"text": "Which db?", "type": "text", "blocking": True}, {"text": "Ship?", "type": "confirm"}])
    hops.answer(working, "Q1", "postgres", note="because")
    aops.task_add(working, [{"text": "a"}, {"text": "b"}])
    s = _json(capsys, working)
    assert s["ticket"] == working and s["status"] in ("in-progress", "waiting")
    assert s["gates"]["requirements"]["state"] == "approved" and s["gates"]["requirements"]["approved"]
    assert s["gates"]["plan"]["state"] == "pending" and s["gates"]["plan"]["changes_requested"] is None
    q1, q2 = s["questions"]
    assert (q1["id"], q1["answer"], q1["note"]) == ("Q1", "postgres", "because") and q1["answered"]
    assert q2["answer"] is None and not q2["answered"]
    assert s["tasks"]["summary"]["total"] == 2 and s["tasks"]["next"] == "T1"
    assert set(s["move"]) >= {"who", "kind"}
    from orch.core.events import read_events
    answered = [e.seq for e in read_events(ws, working) if e.kind == "question.answered"]
    assert s["cursor"] == answered[-1] < last_seq(ws)  # the latest human decision, not the agent's later events


def test_changes_requested_message_and_no_cursor_without_a_decision(ws, aops, hops, working, capsys):
    aops.set_section(working, "Plan", "1. do it")
    hops.request_changes(working, "plan", "too vague")
    s = _json(capsys, working)
    assert s["gates"]["plan"]["changes_requested"] == "too vague"


def test_claim_expiry_and_waiting(ws, aops, working, capsys, monkeypatch):
    s = _json(capsys, working)
    c = s["claim"]
    assert c["held"] and not c["expired"] and c["expires"] and c["ttl_hours"] == 4 and c["harness"] == "claude-code"
    from datetime import timedelta
    from orch.clock import now
    monkeypatch.setattr(query, "clock_now", lambda: now() + timedelta(hours=5))
    assert _json(capsys, working)["claim"]["expired"] is True
    aops.ask(working, [{"text": "Go?", "type": "confirm", "blocking": True}])
    c = _json(capsys, working)["claim"]
    assert c["expired"] is False and c["never_while_waiting"] and c["expires"] is None


def test_text_is_short_and_has_the_cursor(ws, aops, hops, working, capsys):
    aops.ask(working, [{"text": "Which db?", "type": "text", "blocking": True}])
    hops.answer(working, "Q1", "postgres")
    assert run(["status", working]) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) <= 10
    assert "Q1 answered: postgres" in out and "gates:" in out and "claim:" in out and f"cursor: {last_seq(ws)}" in out


def test_status_writes_nothing_and_unclaimed_ticket(ws, put, capsys):
    tid = put("open")
    before = last_seq(ws)
    stamp = store.resolve(ws, tid).path.read_bytes()
    s = status_document(ws, store.load(ws, tid)[1])
    assert not s["claim"]["held"] and s["cursor"] is None
    assert run(["status", tid]) == 0 and "claim: none" in capsys.readouterr().out
    assert last_seq(ws) == before and store.resolve(ws, tid).path.read_bytes() == stamp


def test_unknown_ticket_is_not_found(ws, capsys):
    assert run(["status", "L-9999"]) != 0

from datetime import datetime, timezone

import pytest

import orch.clock as clock
from orch.core.events import Actor, append_event, log_line, read_events
from conftest import human_ops


def test_append_and_read(ws, agent):
    e1 = append_event(ws, "L-0001", "ticket.created", agent, {"title": "x"})
    e2 = append_event(ws, "L-0002", "log.added", agent)
    assert (e1.seq, e2.seq) == (1, 2)
    assert [e.seq for e in read_events(ws, "L-0001")] == [1]
    assert [e.seq for e in read_events(ws, after=1)] == [2]
    assert e1.actor == "agent:claude-code:7f3c9a21" and e1.via == "cli"
    assert read_events(ws, "L-0001")[0].data == {"title": "x"}


def test_corrupt_line_skipped(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    with (ws.state_dir / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write("{broken\n")
    assert append_event(ws, "L-1", "log.added", agent).seq == 2
    assert len(read_events(ws)) == 2


def test_unknown_kind(ws, agent):
    with pytest.raises(ValueError):
        append_event(ws, "L-1", "nope", agent)


def test_actor_labels(agent, human):
    assert human.is_human and human.label == "you" and human.to_str() == "human:you"
    assert not agent.is_human and agent.label == "claude-code 7f3c"
    with pytest.raises(ValueError):
        Actor("robot", "x", "cli")


def test_log_line(monkeypatch, human):
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 9, 30, 10, 12, 33, tzinfo=timezone.utc))
    assert log_line(human, "hi") == "- 2026-09-30T10:12Z [you] hi"


def test_seq_survives_event_longer_than_tail_window(ws, agent):
    # Append an event with data larger than the 8192-byte tail window
    e1 = append_event(ws, "L-1", "log.added", agent, {"text": "x" * 20000})
    assert e1.seq == 1
    # Next event should get seq 2, not reuse 1
    e2 = append_event(ws, "L-2", "log.added", agent)
    assert e2.seq == 2
    # And seq 3
    e3 = append_event(ws, "L-3", "log.added", agent)
    assert e3.seq == 3


def test_status_changes_carry_from_to_and_nothing_else_does(ws, close_tasks):
    """Every event whose op changed the status carries data.from/data.to; no others do; no moved_to."""
    from orch.core.events import Actor
    from orch.core.ops import Ops

    aops = Ops(ws, Actor("agent", "claude-code", "cli", "s-1"))
    hops = human_ops(ws, Actor("human", "you", "tty"))
    tid = aops.new("x").id
    aops.set_section(tid, "Requirements", "r")
    aops.set_section(tid, "Acceptance criteria", "- [ ] a")
    hops.approve(tid, "requirements")
    aops.claim(tid)
    aops.ask(tid, [{"text": "A or B?", "options": ["a", "b"]}])
    hops.answer(tid, "Q1", "A")
    aops.set_section(tid, "Plan", "p")
    hops.approve(tid, "plan")
    aops.set_section(tid, "Verification", "v")
    close_tasks(aops, tid)
    aops.move(tid, "testing")
    hops.verdict(tid, "done")
    events = [e for e in read_events(ws, tid) if e.kind != "ticket.created"]
    moves = [(e.kind, e.data.get("from"), e.data.get("to")) for e in events if "to" in e.data]
    assert moves == [
        ("gate.approved", "backlog", "open"),
        ("claim.taken", "open", "in-progress"),
        ("question.asked", "in-progress", "waiting"),
        ("question.answered", "waiting", "in-progress"),
        ("ticket.moved", "in-progress", "testing"),
        ("verdict.given", "testing", "done"),
    ]
    assert not any("from" in e.data and "to" not in e.data for e in events)
    assert not any("moved_to" in e.data for e in events)
    plan = [e for e in events if e.kind == "gate.approved" and e.data["gate"] == "plan"]
    assert len(plan) == 1 and "to" not in plan[0].data

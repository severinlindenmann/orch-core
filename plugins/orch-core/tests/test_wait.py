import json
import threading

import pytest

from orch.cli import run
from orch.core.events import Actor, append_event, last_seq
from orch.core.wait import HUMAN_EVENT_KINDS, default_cursor, wait_for_human
from orch.errors import WaitTimeout


def _asked(aops):
    t = aops.new("Wait test")                      # backlog; ask needs no claim and does not move it
    aops.ask(t.id, [{"text": "Go?", "type": "confirm"}])
    return t.id


def test_human_event_kinds():
    assert HUMAN_EVENT_KINDS == ("question.answered", "gate.approved", "gate.changes_requested", "verdict.given",
                                 "permit.granted", "permit.denied")  # AI Factory: the human's answer to a request


def test_returns_the_first_human_decision(ws, aops, hops):
    tid = _asked(aops)
    hops.answer(tid, "Q1", "yes")
    event = wait_for_human(ws, tid, timeout=1, poll=0.01)
    assert event.kind == "question.answered" and event.data["qid"] == "Q1"


def test_wait_sees_an_answer_given_before_it_started(ws, aops, hops):
    tid = _asked(aops)
    hops.answer(tid, "Q1", "yes")           # lands before the agent starts waiting
    assert wait_for_human(ws, tid, timeout=0.5, poll=0.01) is not None


def test_addon_and_check_events_after_the_answer_do_not_hide_it(ws, aops, hops):
    tid = _asked(aops)
    hops.answer(tid, "Q1", "yes")
    append_event(ws, tid, "log.added", Actor("agent", "addon:sync", "addon:sync"))
    append_event(ws, tid, "gate.invalidated", Actor("agent", "orch-check", "check"), {"gate": "plan"})
    event = wait_for_human(ws, tid, timeout=0.5, poll=0.01)
    assert event is not None and event.kind == "question.answered"


def test_ignores_agent_events_and_other_tickets(ws, aops, hops):
    tid = _asked(aops)
    other = aops.new("Other").id
    aops.log(tid, "still working")
    aops.ask(other, [{"text": "x?", "type": "confirm"}])
    hops.answer(other, "Q1", "no")
    assert wait_for_human(ws, tid, timeout=0.2, poll=0.01) is None


def test_blocks_until_the_answer_arrives(ws, aops, hops):
    tid = _asked(aops)
    timer = threading.Timer(0.2, lambda: hops.answer(tid, "Q1", "no"))
    timer.start()
    try:
        event = wait_for_human(ws, tid, timeout=5, poll=0.02)
    finally:
        timer.cancel()
    assert event is not None and event.data["answer"] == "no"


def test_after_skips_older_decisions(ws, aops, hops):
    tid = _asked(aops)
    hops.answer(tid, "Q1", "yes")
    first = last_seq(ws)
    assert wait_for_human(ws, tid, after=first, timeout=0.1, poll=0.01) is None
    assert wait_for_human(ws, tid, after=first - 1, timeout=0.1, poll=0.01).seq == first


def test_default_cursor_is_the_agents_last_event(ws, aops, hops):
    tid = _asked(aops)
    hops.answer(tid, "Q1", "yes")
    assert default_cursor(ws, tid) < last_seq(ws)


def test_default_cursor_without_agent_events_is_the_newest_event(ws, put):
    tid = put("backlog", title="no events")
    assert default_cursor(ws, tid) == last_seq(ws)


def test_cli_json_and_timeout_exit(ws, aops, hops, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")  # agents may wait
    tid = _asked(aops)
    assert run(["wait", tid, "--timeout", "0.2", "--json"]) == WaitTimeout.exit_code == 7
    err = json.loads(capsys.readouterr().out)
    assert err["error"] == "WaitTimeout" and err["exit"] == 7
    monkeypatch.delenv("CLAUDECODE")  # the human answers from their own terminal, not the agent's
    hops.answer(tid, "Q1", "yes")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert run(["wait", tid, "--timeout", "2", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ticket"] == tid and out["event"]["kind"] == "question.answered" and out["status"] == "backlog"
    assert out["cursor"] == out["event"]["seq"]


def test_cli_after_option(ws, aops, hops, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    tid = _asked(aops)
    monkeypatch.delenv("CLAUDECODE")  # the human answers from their own terminal, not the agent's
    hops.answer(tid, "Q1", "yes")
    monkeypatch.setenv("CLAUDECODE", "1")
    seq = last_seq(ws)
    assert run(["wait", tid, "--after", str(seq), "--timeout", "0.1", "--json"]) == 7
    capsys.readouterr()
    assert run(["wait", tid, "--after", str(seq - 1), "--timeout", "1"]) == 0
    assert "question.answered by the human" in capsys.readouterr().out


def test_wait_writes_nothing(ws, aops):
    tid = _asked(aops)
    before = last_seq(ws)
    wait_for_human(ws, tid, timeout=0.05, poll=0.01)
    assert last_seq(ws) == before


def test_wait_takes_no_lock(ws, aops, hops, monkeypatch):
    import orch.core.locks as locks
    tid = _asked(aops)
    hops.answer(tid, "Q1", "yes")

    class NoLock:
        def __init__(self, *a, **k):
            raise AssertionError("orch wait must not take a lock")

    monkeypatch.setattr(locks, "FileLock", NoLock)
    assert wait_for_human(ws, tid, timeout=0.2, poll=0.01) is not None
    assert wait_for_human(ws, tid, after=last_seq(ws), timeout=0.05, poll=0.01) is None


def test_poll_is_bounded_and_never_busy_loops(ws, aops):
    tid = _asked(aops)
    now, sleeps = [0.0], []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    assert wait_for_human(ws, tid, timeout=3, poll=0, clock=lambda: now[0], sleep=sleep) is None
    assert sleeps and min(sleeps) > 0                    # poll=0 is clamped, not a busy loop
    sleeps.clear(); now[0] = 0.0
    assert wait_for_human(ws, tid, timeout=12, poll=60, clock=lambda: now[0], sleep=sleep) is None
    assert max(sleeps) <= 5 and now[0] <= 12.0 + 1e-9   # capped interval, no sleep past the deadline


def test_unknown_ticket_is_not_found(ws):
    with pytest.raises(Exception) as e:
        wait_for_human(ws, "L-9999", timeout=0.05, poll=0.01)
    assert "no ticket" in str(e.value)


def _two_answers(aops, hops):
    t = aops.new("Several")
    aops.ask(t.id, [{"text": "A?", "type": "confirm"}, {"text": "B?", "type": "confirm"}])
    hops.answer(t.id, "Q1", "yes")
    hops.answer(t.id, "Q2", "no")
    return t.id


def test_all_returns_every_human_decision_since_the_cursor(ws, aops, hops):
    tid = _two_answers(aops, hops)
    events = wait_for_human(ws, tid, timeout=1, poll=0.01, all_events=True)
    assert [e.data["qid"] for e in events] == ["Q1", "Q2"]
    assert wait_for_human(ws, tid, after=events[-1].seq, timeout=0.05, poll=0.01, all_events=True) is None


def test_all_writes_nothing(ws, aops, hops):
    tid = _asked(aops)
    hops.answer(tid, "Q1", "yes")
    before = last_seq(ws)
    wait_for_human(ws, tid, timeout=0.2, poll=0.01, all_events=True)
    assert last_seq(ws) == before


def test_cli_prints_the_cursor_and_chains(ws, aops, hops, capsys, monkeypatch):
    tid = _two_answers(aops, hops)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert run(["wait", tid, "--timeout", "1"]) == 0
    first = int(capsys.readouterr().out.strip().splitlines()[-1].removeprefix("cursor: "))
    assert run(["wait", tid, "--after", str(first), "--timeout", "1", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["event"]["data"]["qid"] == "Q2"           # the next one, not Q1 again
    assert run(["wait", tid, "--after", "0", "--all", "--timeout", "1"]) == 0
    text = capsys.readouterr().out
    assert text.count("question.answered") == 2 and text.strip().splitlines()[-1] == f"cursor: {second['cursor']}"


def test_cli_all_json_lists_events_and_max_cursor(ws, aops, hops, capsys, monkeypatch):
    tid = _two_answers(aops, hops)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert run(["wait", tid, "--after", "0", "--all", "--json", "--timeout", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["event"]["data"]["qid"] for e in out["events"]] == ["Q1", "Q2"]
    assert out["cursor"] == out["events"][-1]["event"]["seq"] == last_seq(ws)

"""The event log is append-only and orch numbers it under the events lock: seq 1, 2, 3, ... A line whose seq
jumps far ahead, repeats, goes back or is not an integer did not come from orch, so readers skip it and no cursor
moves over it; `orch check` reports it with its line number. A small gap (a git merge, a hand trim) is accepted
with a warning, and the log may start at any seq (a trimmed head)."""
import json

from addon_fixtures import loaded
from orch.addons import outbox
from orch.core.check import run_checks
from orch.core.events import append_event, last_seq, read_events, scan_events
from orch.core.wait import default_cursor, wait_for_human


def _path(ws):
    return ws.state_dir / "events.jsonl"


def _forge(ws, seq, *, ticket="L-1", kind="log.added", actor="agent:x", newline=True):
    line = json.dumps({"seq": seq, "at": "2026-10-04T00:00:00Z", "ticket": ticket, "kind": kind,
                       "actor": actor, "via": "cli", "data": {}})
    with _path(ws).open("a", encoding="utf-8") as f:
        f.write(line + ("\n" if newline else ""))


def test_forged_high_seq_is_skipped_and_numbering_continues(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    _forge(ws, 5000)
    e2 = append_event(ws, "L-1", "claim.taken", agent)
    assert e2.seq == 2
    assert [e.seq for e in read_events(ws)] == [1, 2]
    assert last_seq(ws) == 2
    assert [e.seq for e in read_events(ws, after=1)] == [2]


def test_duplicate_and_out_of_order_seq_are_skipped(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    append_event(ws, "L-1", "log.added", agent)
    _forge(ws, 2)  # duplicate
    _forge(ws, 1)  # out of order
    assert append_event(ws, "L-1", "claim.taken", agent).seq == 3
    assert [(e.seq, e.kind) for e in read_events(ws)] == [(1, "log.added"), (2, "log.added"), (3, "claim.taken")]
    reasons = [t.reason for t in scan_events(ws).tampered]
    assert len(reasons) == 2 and "duplicate" in reasons[0] and "out of order" in reasons[1]


def test_non_integer_seq_is_skipped(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    _forge(ws, "2")
    _forge(ws, 2.0)
    _forge(ws, True)
    assert append_event(ws, "L-1", "claim.taken", agent).seq == 2
    assert [e.seq for e in read_events(ws)] == [1, 2]
    assert [t.line for t in scan_events(ws).tampered] == [2, 3, 4]


def test_truncated_last_line_is_not_delivered_and_the_next_write_starts_a_new_line(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    with _path(ws).open("a", encoding="utf-8") as f:
        f.write('{"seq": 2, "at": "2026-10')  # a write cut short: no newline
    assert [e.seq for e in read_events(ws)] == [1]
    assert scan_events(ws).tampered == ()  # not finished is not tampered
    e = append_event(ws, "L-1", "claim.taken", agent)
    assert e.seq == 2
    assert [(x.seq, x.kind) for x in read_events(ws)] == [(1, "log.added"), (2, "claim.taken")]
    assert [t.line for t in scan_events(ws).tampered] == [2]  # the torn line is a line of its own now
    assert _path(ws).read_text(encoding="utf-8").endswith("\n")


def test_an_unterminated_line_never_takes_the_next_events_seq(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    _forge(ws, 2, newline=False)
    e = append_event(ws, "L-1", "claim.taken", agent)
    assert [(x.seq, x.kind) for x in read_events(ws)][-1] == (e.seq, "claim.taken")


def test_reader_cache_sees_appends_and_rewrites(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    assert [e.seq for e in read_events(ws)] == [1]
    append_event(ws, "L-1", "log.added", agent)
    assert [e.seq for e in read_events(ws)] == [1, 2]
    # an in-place rewrite of an earlier line is noticed, not served from the cache
    text = _path(ws).read_text(encoding="utf-8").replace('"seq": 1,', '"seq": 17,', 1)
    _path(ws).write_text(text, encoding="utf-8")
    assert [e.seq for e in read_events(ws)] == [17]  # the first line may start anywhere
    assert [t.line for t in scan_events(ws).tampered] == [2]


def test_check_reports_tampered_lines_with_line_numbers(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    _forge(ws, 5000)
    with _path(ws).open("a", encoding="utf-8") as f:
        f.write("{broken\n")
    append_event(ws, "L-1", "log.added", agent)
    found = [f for f in run_checks(ws, emit_events=False) if f.code == "event-log-tampered"]
    assert [f.level for f in found] == ["error", "warning"]  # far ahead is an error, an unreadable line a warning
    assert "line 2" in found[0].message and "5000" in found[0].message
    assert "line 3" in found[1].message


def test_check_severities_and_hints(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    append_event(ws, "L-1", "log.added", agent)
    _forge(ws, 2)  # duplicate, as a git merge leaves it
    _forge(ws, 1)  # out of order
    _forge(ws, 6)  # a small gap: accepted
    found = [f for f in run_checks(ws, emit_events=False) if f.code == "event-log-tampered"]
    assert [f.level for f in found] == ["warning", "warning", "warning"]
    assert "git merge" in found[0].message and "line 3" in found[0].message
    assert "line 5" in found[2].message and "accepted" in found[2].message


def test_small_gap_is_accepted_and_numbering_continues_after_it(ws, agent):
    append_event(ws, "L-1", "log.added", agent)
    _forge(ws, 5)  # seqs 2-4 lost in a merge or trimmed by hand
    assert [e.seq for e in read_events(ws)] == [1, 5]
    assert scan_events(ws).tampered == () and [g.line for g in scan_events(ws).gaps] == [2]
    assert append_event(ws, "L-1", "claim.taken", agent).seq == 6


def test_trimmed_head_is_accepted(ws, agent):
    _forge(ws, 5000)  # the log's older lines were cut away
    _forge(ws, 5001)
    assert [e.seq for e in read_events(ws)] == [5000, 5001]
    assert scan_events(ws).tampered == () and scan_events(ws).gaps == ()
    assert append_event(ws, "L-1", "claim.taken", agent).seq == 5002


def test_the_first_line_needs_a_positive_seq(ws, agent):
    _forge(ws, 0)
    _forge(ws, -3)
    assert read_events(ws) == []
    assert [t.line for t in scan_events(ws).tampered] == [1, 2]


def test_jump_just_within_the_limit_is_accepted_beyond_it_rejected(ws, agent):
    from orch.core.events import MAX_SEQ_GAP
    append_event(ws, "L-1", "log.added", agent)
    _forge(ws, 1 + MAX_SEQ_GAP + 1)
    assert [e.seq for e in read_events(ws)] == [1]
    _forge(ws, 1 + MAX_SEQ_GAP)
    assert [e.seq for e in read_events(ws)] == [1, 1 + MAX_SEQ_GAP]


def test_forged_high_seq_does_not_move_the_wait_cursor(ws, aops, hops):
    t = aops.new("Wait")
    aops.ask(t.id, [{"text": "Go?", "type": "confirm"}])
    _forge(ws, 5000, ticket=t.id, actor="agent:claude-code:7f3c9a21")  # looks like the agent's own event
    assert default_cursor(ws, t.id) < 5000
    hops.answer(t.id, "Q1", "yes")
    event = wait_for_human(ws, t.id, timeout=0.5, poll=0.01)
    assert event is not None and event.kind == "question.answered"


def test_forged_human_decision_is_not_returned_by_wait(ws, aops):
    t = aops.new("Wait")
    aops.ask(t.id, [{"text": "Go?", "type": "confirm"}])
    _forge(ws, 5000, ticket=t.id, kind="question.answered", actor="human:you")
    assert wait_for_human(ws, t.id, timeout=0.1, poll=0.01) is None


class _Seen:
    def __init__(self):
        self.seen = []

    def on_event(self, event, box):
        self.seen.append((event.seq, event.kind))


def test_forged_high_seq_does_not_move_the_addon_cursor(ws, agent):
    m = _Seen()
    a = loaded(ws, m, capabilities=["provider", "page", "settings", "events"])
    append_event(ws, "L-1", "log.added", agent)
    outbox.pump(ws, a)  # first pump: cursor at now (1)
    _forge(ws, 5000)
    assert outbox.pump(ws, a) == 0
    append_event(ws, "L-1", "claim.taken", agent)
    append_event(ws, "L-1", "state.updated", agent)
    assert outbox.pump(ws, a) == 2
    assert m.seen == [(2, "claim.taken"), (3, "state.updated")]


def test_an_addon_cannot_change_the_events_other_readers_see(ws, agent):
    class Mutator:
        def on_event(self, event, box):
            event.data["title"] = "changed"

    a = loaded(ws, Mutator(), capabilities=["provider", "page", "settings", "events"])
    outbox.pump(ws, a)
    append_event(ws, "L-1", "ticket.created", agent, {"title": "x"})
    assert outbox.pump(ws, a) == 1
    assert read_events(ws)[-1].data == {"title": "x"}

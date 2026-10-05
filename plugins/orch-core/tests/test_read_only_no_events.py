"""#108: read-only commands never write the tracked events.jsonl; the gate.invalidated event is recorded by the next
write on that ticket (or `orch check --record`), before any later decision."""
import pytest

from orch.cli import run
from orch.core.events import read_events
from orch.core.gates import gate_state
from orch.core import store


def _invalidated(ws, aops, hops):
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    hops.approve(t.id, "requirements")
    aops.claim(t.id)
    aops.set_section(t.id, "Plan", "- step one")
    hops.approve(t.id, "plan")
    aops.set_section(t.id, "Plan", "- step one, changed after approval")
    return t.id


def _log_bytes(ws):
    return (ws.state_dir / "events.jsonl").read_bytes()


def _invalidations(ws, tid):
    return [e for e in read_events(ws, tid) if e.kind == "gate.invalidated"]


@pytest.mark.parametrize("argv", [["check"], ["list"], ["doctor"], ["check", "--json"], ["list", "--json"]])
def test_read_only_commands_leave_the_event_log_alone(ws, aops, hops, argv, capsys):
    tid = _invalidated(ws, aops, hops)
    before = _log_bytes(ws)
    run(argv)
    out = capsys.readouterr().out
    assert _log_bytes(ws) == before
    assert _invalidations(ws, tid) == []
    if argv[0] == "check":
        assert "gate-invalidated" in out  # still reported


def test_check_record_writes_exactly_one_event_even_when_repeated(ws, aops, hops):
    tid = _invalidated(ws, aops, hops)
    run(["check", "--record"])
    run(["check", "--record"])
    run(["check"])
    assert len(_invalidations(ws, tid)) == 1


def test_next_write_records_exactly_one_invalidation(ws, aops, hops):
    tid = _invalidated(ws, aops, hops)
    run(["check"])
    assert _invalidations(ws, tid) == []
    aops.log(tid, "a note")
    aops.log(tid, "another note")
    run(["check"])
    found = _invalidations(ws, tid)
    assert len(found) == 1 and found[0].data["gate"] == "plan"


def test_an_approval_after_invalidation_is_preceded_by_the_invalidation(ws, aops, hops):
    tid = _invalidated(ws, aops, hops)
    run(["check"])
    assert gate_state(store.read_ticket(store.resolve(ws, tid).path), "plan") == "invalidated"
    hops.approve(tid, "plan")
    kinds = [e.kind for e in read_events(ws, tid)]
    assert kinds.count("gate.invalidated") == 1
    assert kinds.index("gate.invalidated") < len(kinds) - 1 - kinds[::-1].index("gate.approved")


def test_a_decision_after_invalidation_still_requires_reapproval(ws, aops, hops):
    tid = _invalidated(ws, aops, hops)
    run(["check"])
    aops.log(tid, "work continues")  # a write that is not a decision
    t = store.read_ticket(store.resolve(ws, tid).path)
    assert gate_state(t, "plan") == "invalidated"  # recording the event is not an approval
    hops.approve(tid, "plan")
    assert gate_state(store.read_ticket(store.resolve(ws, tid).path), "plan") == "approved"

"""`move` (core H6b): backlog <-> open for unclaimed work, a hand-off like the other ticket actions."""

import pytest
from pydantic import TypeAdapter, ValidationError

from orch_dashboard.handoff import Move, PrepareBody, expected, host_call


def parse(body):
    return TypeAdapter(PrepareBody).validate_python(body)


def test_move_is_a_typed_action_with_a_closed_target():
    action = parse({"action": "move", "key": "DEMO-0001", "to": "backlog"})
    assert isinstance(action, Move)
    assert host_call(action) == ("move", {"ref": "DEMO-0001", "to": "backlog"})
    assert (
        host_call(parse({"action": "move", "key": "DEMO-0001", "to": "open", "reason": "ready"}))[1]["reason"]
        == "ready"
    )
    for bad in ({"to": "done"}, {"to": "in-progress"}, {"to": "open", "reason": "a‮b"}, {"to": "open", "extra": 1}):
        with pytest.raises(ValidationError):
            parse({"action": "move", "key": "DEMO-0001", **bad})


def test_the_prepared_event_must_be_ticket_moved_to_the_target():
    check = expected(parse({"action": "move", "key": "DEMO-0001", "to": "backlog"}))
    assert check([{"type": "ticket.moved", "from": "open", "to": "backlog"}])
    assert not check([{"type": "ticket.moved", "from": "open", "to": "open"}])
    assert not check([{"type": "ticket.closed", "to": "backlog"}])
    assert not check([{"type": "ticket.moved", "to": "backlog", "reason": "sneaked in"}])

"""Manual acceptance uses closed intents and checks the exact first event before review."""

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from orch_dashboard.adapters.activity import sentence
from orch_dashboard.adapters.tickets import _manual_acceptance, event
from orch_dashboard.handoff import PrepareBody, expected, host_call


def parse(action, **fields):
    return TypeAdapter(PrepareBody).validate_python({"action": action, "key": "DEMO-0001", **fields})


@pytest.mark.parametrize(
    "action, fields, op, args",
    [
        ("accept_criterion", {"ac": "AC2", "note": " checked "}, "ac.accept", {"ac": "AC2", "note": "checked"}),
        ("accept_criterion", {"ac": "AC2"}, "ac.accept", {"ac": "AC2"}),
        (
            "withdraw_acceptance",
            {"ac": "AC2", "reason": " changed "},
            "ac.unaccept",
            {"ac": "AC2", "reason": "changed"},
        ),
        ("mark_task_done", {"task": "T2"}, "task.mark_done", {"task": "T2"}),
    ],
)
def test_literal_calls(action, fields, op, args):
    assert host_call(parse(action, **fields)) == (op, {"ref": "DEMO-0001", **args})


@pytest.mark.parametrize(
    "action, fields",
    [
        ("accept_criterion", {"ac": "AC0"}),
        ("accept_criterion", {"ac": "AC2", "note": "x" * 1001}),
        ("accept_criterion", {"ac": "AC2", "note": "a\u202eb"}),
        ("accept_criterion", {"ac": "AC2", "op": "ac.accept"}),
        ("withdraw_acceptance", {"ac": "AC2"}),
        ("withdraw_acceptance", {"ac": "AC2", "reason": "   "}),
        ("withdraw_acceptance", {"ac": "AC2", "reason": "x" * 1001}),
        ("mark_task_done", {"task": "T0"}),
        ("mark_task_done", {"task": "T2", "receipt": {}}),
    ],
)
def test_refuses_invalid_intents(action, fields):
    with pytest.raises(ValidationError):
        parse(action, **fields)


@pytest.mark.parametrize(
    "action, fields, good, bad",
    [
        (
            "accept_criterion",
            {"ac": "AC2"},
            {"type": "artifact.added", "kind": "feedback", "ac": "AC2"},
            [{"ac": "AC3"}, {"kind": "receipt"}, {"type": "artifact.replaced"}],
        ),
        (
            "withdraw_acceptance",
            {"ac": "AC2", "reason": "changed"},
            {"type": "artifact.replaced", "kind": "feedback", "label": "withdrawn AC2"},
            [{"label": "withdrawn AC3"}, {"ac": "AC2"}, {"kind": "receipt"}],
        ),
        (
            "mark_task_done",
            {"task": "T2"},
            {"type": "task.done", "task": "T2"},
            [{"task": "T3"}, {"receipt": None}, {"receipt": {"exit": 0}}, {"type": "task.started"}],
        ),
    ],
)
def test_refuses_wrong_or_extra_prepared_events(action, fields, good, bad):
    check = expected(parse(action, **fields))
    assert check([good])
    assert not check([])
    assert not check([good, good])
    for patch in bad:
        assert not check([good | patch])


def test_manual_metadata_keeps_host_proof_and_time():
    at = datetime(2026, 10, 19, 12, 2, tzinfo=UTC)
    assert _manual_acceptance(
        {
            "proof": "proven",
            "accepted": {"by": "p_a", "at": at, "note": None, "auth": "convenience", "statement_ok": True},
        }
    ) == {"proof": "proven", "accepted": {"by": "p_a", "at": at.isoformat(), "note": None, "auth": "convenience"}}
    assert _manual_acceptance({"proof": "pending", "accepted_stale": True}) == {
        "proof": "pending",
        "accepted_stale": True,
    }


@pytest.mark.parametrize(
    "kind, data, words",
    [
        (
            "artifact.added",
            {"kind": "feedback", "ac": "AC2", "label": "accepted AC2 " + "a" * 16},
            "accepted AC2 manually",
        ),
        ("artifact.replaced", {"kind": "feedback", "label": "withdrawn AC2"}, "withdrew the manual acceptance of AC2"),
    ],
)
def test_activity_and_history_words(kind, data, words):
    row = {
        "type": kind,
        "data": data,
        "actor": {"kind": "person", "id": "p_a"},
        "seq": 1,
        "at": "2026-10-19T12:02:00Z",
        "summary": "old",
    }
    assert sentence(row, lambda _: "Severin") == words
    assert event(row)["summary"] == words
    assert sentence(row | {"actor": {"kind": "agent"}}, lambda _: "agent") != words
    assert sentence(row | {"data": data | {"kind": "receipt"}}, lambda _: "Severin") != words

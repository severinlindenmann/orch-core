"""The follow-up event shape: closed lists, --on parsing, matching, ledger mapping, hook ids."""

import pytest

from orch.model import followup_events as fe


def test_parse_on_orch_forms():
    assert fe.parse_on("question.answered") == {"source": "orch", "kind": "question", "status": "answered"}
    assert fe.parse_on("ticket.changed:testing")["ref"] == "testing"
    assert fe.parse_on("gate.approved:requirements")["kind"] == "gate"


@pytest.mark.parametrize(
    "bad", ["", "question", "question.nope", "nope.answered", "ticket.changed:bad ref", "x:" + "a" * 65]
)
def test_parse_on_refuses(bad):
    with pytest.raises(ValueError):
        fe.parse_on(bad)


def test_hook_ids_and_hash():
    a, b = fe.new_hook_id(), fe.new_hook_id()
    assert a != b and fe.HOOK_RE.fullmatch(a)
    m = fe.parse_on("webhook:" + a, "w1")
    assert m["hook"] == fe.hook_hash(a, "w1") and len(m["hook"]) == 64 and a not in str(m)
    with pytest.raises(ValueError):
        fe.parse_on("webhook:hk_short", "w1")


def test_matches_and_from_ledger():
    on = fe.parse_on("question.answered:q1")
    ev = fe.from_ledger(
        {"type": "question.answered", "question": "q1", "text": "secret words", "at": "2026-10-27T10:00:00Z"}
    )
    assert ev == {"source": "orch", "kind": "question", "ref": "q1", "status": "answered", "at": "2026-10-27T10:00:00Z"}
    assert fe.matches(on, ev)
    assert not fe.matches(on, {**ev, "ref": "q2"})
    assert fe.matches(fe.parse_on("question.answered"), {**ev, "ref": "q2"})
    assert fe.from_ledger({"type": "log.added", "at": "x"}) is None
    assert not fe.matches(on, {**ev, "extra": "field"})  # nothing outside the shape


def test_webhook_match_is_by_hash():
    hid = fe.new_hook_id()
    on = fe.parse_on("webhook:" + hid, "w1")
    assert fe.matches(on, fe.webhook_event(fe.hook_hash(hid, "w1"), "2026-10-27T10:00:00Z"))
    assert not fe.matches(on, fe.webhook_event(fe.hook_hash(hid, "w2"), "2026-10-27T10:00:00Z"))  # B3: per workspace
    assert fe.hook_hash(hid, "w1") != fe.hook_hash(hid, "w2")
    assert not fe.matches(on, fe.webhook_event(fe.hook_hash(fe.new_hook_id(), "w1"), "2026-10-27T10:00:00Z"))


def test_wake_line_uses_closed_values_only():
    assert "not the event" in fe.wake_line("deadline", "no_ring", None)
    assert fe.wake_line("time", None, None) is None

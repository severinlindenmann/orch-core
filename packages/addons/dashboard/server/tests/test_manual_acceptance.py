"""Manual acceptance uses closed intents and checks the exact first event before review."""

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from orch_dashboard.adapters.activity import sentence
from orch_dashboard.adapters.tickets import _manual_acceptance, event
from orch_dashboard.signing import PrepareBody, expected, host_call


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


PERSON = "p_" + "a" * 32
OTHER = "p_" + "b" * 32


def prepared_accept(note=None, ac="AC2"):
    """The event and statement a host prepares for ac.accept (core's own builders)."""
    from orch.canon import artifact_digest
    from orch.host import acceptance
    from orch.model.accept import label

    statement = acceptance.build_accept(ac, "It works", note)
    data = statement.encode()
    event = {
        "v": 2,
        "id": "01J9ZK4Q7M3R8T2V6X0B000001",
        "type": "artifact.added",
        "actor": {"kind": "person", "id": PERSON, "device": "d_" + "c" * 32},
        "auth": None,
        "hash_v": 1,
        "roster_v": 1,
        "based_on": None,
        "name": f"accept-{ac}-1.md",
        "kind": "feedback",
        "sha256": artifact_digest(data),
        "bytes": len(data),
        "ac": ac,
        "label": label(ac, "It works"),
    }
    return event, statement


def prepared_withdraw(reason="changed", ac="AC2"):
    from orch.canon import artifact_digest
    from orch.host import acceptance

    statement = acceptance.build_withdraw(ac, "It works", reason)
    data = statement.encode()
    event, _ = prepared_accept(None, ac)
    event = {k: v for k, v in event.items() if k != "ac"} | {
        "type": "artifact.replaced",
        "sha256": artifact_digest(data),
        "bytes": len(data),
        "label": f"withdrawn {ac}",
        "replaces": event["sha256"],
    }
    return event, statement


def test_accept_event_and_statement_must_be_exactly_what_was_asked():
    from orch_dashboard.signing import expected_material

    action = parse("accept_criterion", ac="AC2", note="checked")
    event, statement = prepared_accept("checked")
    check, material = expected(action, PERSON), expected_material(action)
    assert check([event]) and material({"statement": statement}, [event])
    # a mismatched note, another criterion, another person, an extra key or a bad label: refused
    other_note, other_statement = prepared_accept("other")
    assert not material({"statement": other_statement}, [other_note])
    assert not material({"statement": statement}, [event | {"sha256": other_note["sha256"]}])
    assert not material({}, [event]) and not material({"statement": 5}, [event])
    assert not check([event | {"actor": event["actor"] | {"id": OTHER}}])
    assert not check([event | {"actor": {"kind": "agent", "id": PERSON}}])
    assert not check([event | {"task": "T1"}]) and not check([event | {"addon": "x"}])
    assert not check([event | {"label": "accepted AC2 nothex"}]) and not check(
        [event | {"label": "accepted AC3 " + "a" * 16}]
    )
    note_missing, statement_missing = prepared_accept(None)
    assert not material({"statement": statement_missing}, [note_missing])


def test_withdraw_event_and_statement_must_be_exactly_what_was_asked():
    from orch_dashboard.signing import expected_material

    action = parse("withdraw_acceptance", ac="AC2", reason="changed")
    event, statement = prepared_withdraw("changed")
    check, material = expected(action, PERSON), expected_material(action)
    assert check([event]) and material({"statement": statement}, [event])
    other, other_statement = prepared_withdraw("different")
    assert not material({"statement": other_statement}, [other])
    assert not check([event | {"ac": "AC2"}]) and not check([event | {"receipt": None}])
    assert not check([event | {"actor": event["actor"] | {"id": OTHER}}])
    assert not check([event | {"label": "withdrawn AC3"}])
    accepted, accepted_statement = prepared_accept("changed")
    assert not material({"statement": accepted_statement}, [accepted])


@pytest.mark.parametrize(
    "bad",
    [
        {"task": "T3"},
        {"receipt": None},
        {"receipt": {"exit": 0}},
        {"type": "task.started"},
        {"log": "x.md"},
        {"text": "x"},
    ],
)
def test_mark_task_done_event_is_closed(bad):
    check = expected(parse("mark_task_done", task="T2"), PERSON)
    good = {"type": "task.done", "task": "T2", "actor": {"kind": "person", "id": PERSON}}
    assert check([good]) and not check([]) and not check([good, good])
    assert not check([good | bad])
    assert not check([good | {"actor": {"kind": "agent", "id": PERSON}}])
    assert not check([good | {"actor": {"kind": "person", "id": OTHER}}])


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
    assert sentence(row, lambda _: "Severin", lambda _: True) == words
    marker = event(row, can=lambda _: True)["manual_acceptance"]
    assert marker["kind"] == ("accepted" if kind == "artifact.added" else "withdrawn") and marker["ac"] == "AC2"
    assert event(row, can=lambda _: True)["summary"] == "old"  # the summary is the host's, never rewritten
    agent = event(row | {"actor": {"kind": "agent", "id": "x", "session": "s", "for": PERSON}})
    assert "manual_acceptance" not in agent
    spoof = event(row | {"summary": words, "actor": {"kind": "agent", "id": "x", "session": "s", "for": PERSON}})
    assert "manual_acceptance" not in spoof
    assert sentence(row | {"actor": {"kind": "agent"}}, lambda _: "agent") != words
    assert sentence(row | {"data": data | {"kind": "receipt"}}, lambda _: "Severin") != words


@pytest.mark.parametrize(
    "role,owner,reviewer,kind,allowed",
    [
        ("owner", False, False, "person", True),
        ("maintainer", False, False, "person", True),
        ("member", True, False, "person", True),
        ("member", False, True, "person", True),
        ("member", False, False, "person", False),
        ("viewer", True, True, "person", False),
        ("owner", True, True, "agent", False),
    ],
)
def test_manual_marker_requires_authority(role, owner, reviewer, kind, allowed):
    from orch_dashboard.adapters.activity import acceptance_authority, feed
    from orch_dashboard.adapters.tickets import events

    roster = {"members": [{"person": PERSON, "role": role, "name": "Severin"}]}
    doc = {"owner": PERSON if owner else OTHER, "people": {"reviewers": [PERSON] if reviewer else []}}
    row = {
        "type": "artifact.added",
        "data": {"kind": "feedback", "ac": "AC2", "label": "accepted AC2 " + "a" * 16},
        "actor": {"kind": kind, "id": PERSON},
        "seq": 1,
        "id": "event1",
        "at": "2026-10-19T12:02:00Z",
        "summary": "file",
    }
    assert ("manual_acceptance" in events({"log": [row]}, acceptance_authority(roster, doc))[0]) == allowed
    assert "manual_acceptance" not in event(row)
    did = feed({"items": [row]}, roster)["items"][0]["did"]
    assert (did == "accepted AC2 manually") == (kind == "person" and role in ("owner", "maintainer"))

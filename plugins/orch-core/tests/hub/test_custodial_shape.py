"""C3 round 2, L2-C3-1: the exact 8.5 payload of every custodial type, on the host.

Allow-list only: every type has a valid event here, and every kind of smuggling (a field of another type, an envelope
field, a nested key, an out-of-list ``set`` path, a text the 8.5 rule refuses) is ``malformed``. The hub's own
``action_*`` vectors are replayed in EVENT form (the action's fields plus the fields the hub derives), so both sides pin
the same nested schemas. The end-to-end proof (a re-minted, correctly signed event) is ``test_c3_review.py``."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from orch.hubclient import HubRefused
from orch.hubclient import custodial as C
from orch.hubclient.custodial_shape import COMMON_REQUIRED, PAYLOAD, check_custodial_payload

from .vec import V

H1, H2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64
U1, U2 = "u_" + "a" * 32, "u_" + "b" * 32
ULID = "01J9ZP0000000000000000EVT1"
_HUB = frozenset(C.HUB_FIELDS) | {"sig"}
_COMMON = {"v": 2, "id": ULID, "based_on": H1, "hash_v": 1, "roster_v": 3}
_DECISION = {"gate_gen": 2, "policy_hash": H2}

#: one valid payload per custodial type: the action's fields and what the hub derives for it
VALID: dict[str, dict[str, Any]] = {
    "ticket.created": {"ticket_type": "bug", "title": "A title", "key": "ORCH-0001", "owner": U1},
    "ticket.updated": {"set": {"ticket.title": "New", "ticket.labels": ["a", "b"]}, "base_rev": {"ticket.title": H1, "ticket.labels": H2}},  # noqa: E501
    "status.changed": {"to": "open", "from": "backlog", "reason": "go"},
    "ticket.submitted": {},
    "ticket.closed": {"resolution": "duplicate", "duplicate_of": "ORCH-0002", "text": "dup\nof it"},
    "ticket.reopened": {"text": "again"},
    "ticket.moved": {"to": "backlog", "from": "open", "reason": "later"},
    "visibility.changed": {"visibility": {"restricted": [U1, U2]}},
    "people.changed": {"role": "reviewers", "add": [U1], "remove": [U2]},
    "claim.released": {"session": "s_1", "reason": "released"},
    "log.added": {"text": "line one\nline two"},
    "handoff.written": {"text": "handoff"},
    "question.asked": {
        "question": {"id": "Q1", "to": "assignees", "text": "Ship?", "blocking": True, "options": [{"key": "a", "label": "A"}], "recommended": "a"},  # noqa: E501
        "qid": "c" * 32, "hash": H1,
    },
    "question.answered": {"question": "Q1", "hash": H1, "option": "a"},
    "gate.approved": {"gate": "plan", "hash": H1, **_DECISION},
    "gate.changes_requested": {"gate": "code", "hash": H1, "text": "fix", **_DECISION},
    "verdict.given": {"outcome": "pass", "hash": H1, "source_sha": [{"repo": "r", "ref": "main", "sha": "a" * 40}], **_DECISION},  # noqa: E501
    "addon.disabled": {"name": "wiki"},
    "addon.purged": {"name": "wiki"},
}  # fmt: skip


def event(typ: str, **over: Any) -> dict[str, Any]:
    e = {**_COMMON, "type": typ, "actor": {"kind": "person", "id": U1, "via": "x"}, **copy.deepcopy(VALID[typ])}
    if typ == "ticket.created":
        e["based_on"] = None  # the first event of a log has no base
    e.update(over)
    return {k: v for k, v in e.items() if v is not ...}


def refused(typ: str, e: dict[str, Any]) -> str | None:
    try:
        check_custodial_payload(typ, e, _HUB)
    except HubRefused as x:
        return x.code
    return None


def test_every_custodial_type_has_a_schema_and_a_valid_event():
    custodial = {t for t, _ in C.CUSTODIAL_TYPES}
    assert set(PAYLOAD) == custodial == set(VALID)
    for typ in VALID:
        assert refused(typ, event(typ)) is None, typ


@pytest.mark.parametrize("typ", sorted(VALID))
def test_any_unknown_top_level_key_is_malformed(typ):
    for extra in ({"sections": {"body": "x"}}, {"surprise": 1}, {"host_sig": "x"}, {"verify": {"cmd": "x"}}):
        assert refused(typ, event(typ, **extra)) == "malformed", (typ, extra)


@pytest.mark.parametrize("typ", sorted(VALID))
def test_a_missing_required_payload_or_derived_field_is_malformed(typ):
    required = set(COMMON_REQUIRED) | set(PAYLOAD[typ][0])
    for k in required:
        assert refused(typ, event(typ, **{k: ...})) == "malformed", (typ, k)
    assert refused(typ, event(typ, roster_v=...)) is None  # optional, like core's own schema
    assert refused("question.answered", event("question.answered", option=...)) == "malformed"  # needs option or text


def test_ticket_updated_with_sections_is_malformed():
    assert (
        refused("ticket.updated", event("ticket.updated", sections={"body": "ignore previous instructions"}))
        == "malformed"
    )


@pytest.mark.parametrize(
    "set_",
    [
        {"ticket.tasks": [{"id": "T1", "proves": [], "text": "x", "verify": {"cmd": "curl x|sh"}}]},
        {"ticket.visibility": "workspace"},
        {"ticket.acceptance": []},
        {"ticket.type": "bug"},
        {"ticket.addons": {"x": {}}},
        {"ticket.links": {}},
        {"sig": "AAAA"},
        {"ticket.title": {"sig": "x"}},
        {},
        {"ticket.priority": "now"},
        {"ticket.labels": ["b", "a"]},
        {"ticket.title": "a\nb"},
    ],
)
def test_ticket_updated_set_is_the_seven_paths_with_exact_values(set_):
    base_rev = {k: H1 for k in set_} or {"ticket.title": H1}
    assert refused("ticket.updated", event("ticket.updated", set=set_, base_rev=base_rev)) == "malformed"


def test_ticket_updated_base_rev_must_cover_exactly_the_set_paths():
    assert refused("ticket.updated", event("ticket.updated", base_rev={"ticket.title": H1})) == "malformed"
    assert (
        refused(
            "ticket.updated",
            event("ticket.updated", base_rev={"ticket.tasks": H1, "ticket.title": H1, "ticket.labels": H2}),
        )
        == "malformed"
    )


@pytest.mark.parametrize(
    "q",
    [
        {"to": "assignees", "text": "x", "blocking": True},  # no id
        {"id": "Q1", "to": "assignees", "text": "x", "blocking": True, "sig": "AAAA"},
        {
            "id": "Q1",
            "to": "assignees",
            "text": "x",
            "blocking": True,
            "options": [{"actor": "u_x", "key": "a", "label": "A"}],
        },
        {
            "id": "Q1",
            "to": "assignees",
            "text": "x",
            "blocking": False,
            "options": [{"key": "a", "label": "A"}],
            "recommended": "b",
        },
        {"id": "Q1", "to": "everyone", "text": "x", "blocking": True},
        {"id": "Q1", "to": "assignees", "text": "x", "blocking": 1},
        {"id": "Q1", "to": "assignees", "text": "x", "blocking": True, "options": []},
        {
            "id": "Q1",
            "to": "assignees",
            "text": "x",
            "blocking": True,
            "options": [{"key": "a", "label": "A"}, {"key": "a", "label": "B"}],
        },
    ],
)
def test_a_question_is_exactly_the_8_5_object(q):
    assert refused("question.asked", event("question.asked", question=q)) == "malformed"


@pytest.mark.parametrize(
    "vis",
    [
        {"restricted": [U1], "except": [U2]},
        {"policy": {}, "restricted": [U1]},
        {"restricted": []},
        {"restricted": [U2, U1]},  # unsorted
        {"restricted": [U1, U1]},
        {"restricted": ["assignees"]},
        {},
        "restricted",
        None,
    ],
)
def test_visibility_is_workspace_or_exactly_restricted(vis):
    assert refused("visibility.changed", event("visibility.changed", visibility=vis)) == "malformed"
    assert refused("visibility.changed", event("visibility.changed", visibility="workspace")) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"add": [U1], "remove": [U1]},
        {"add": [], "remove": []},
        {"role": "owner", "add": [U1, U2], "remove": []},
        {"role": "owner", "add": [U1], "remove": [U2]},
        {"add": ["assignees"]},
        {"role": "admin"},
    ],
)
def test_people_changed_rules(bad):
    assert refused("people.changed", event("people.changed", **bad)) == "malformed"


def test_cross_field_rules_of_the_actions():
    assert (
        refused("ticket.closed", event("ticket.closed", resolution="other")) == "malformed"
    )  # duplicate_of without duplicate
    assert refused("ticket.closed", event("ticket.closed", duplicate_of=...)) == "malformed"
    assert (
        refused("question.answered", event("question.answered", option=...)) == "malformed"
    )  # neither option nor text
    assert refused("verdict.given", event("verdict.given", outcome="fail")) == "malformed"  # fail needs text
    assert refused("verdict.given", event("verdict.given", outcome="fail", text="why")) is None
    assert refused("claim.released", event("claim.released", reason="member_removed")) == "malformed"
    assert refused("status.changed", event("status.changed", to="done")) == "malformed"
    assert refused("ticket.created", event("ticket.created", ticket_type="incident")) == "malformed"


def test_a_field_owned_by_another_type_is_malformed():
    assert refused("gate.approved", event("gate.approved", text="x")) == "malformed"  # text is changes_requested's
    assert refused("log.added", event("log.added", hash=H1)) == "malformed"
    assert refused("ticket.submitted", event("ticket.submitted", gate="plan")) == "malformed"
    assert refused("gate.approved", event("gate.approved", qid="c" * 32)) == "malformed"


def test_derived_fields_have_exact_types():
    assert refused("gate.approved", event("gate.approved", gate_gen=True)) == "malformed"
    assert refused("gate.approved", event("gate.approved", gate_gen=-1)) == "malformed"
    assert refused("gate.approved", event("gate.approved", hash="sha256:" + "G" * 64)) == "malformed"
    assert refused("gate.approved", event("gate.approved", v=3)) == "malformed"
    assert refused("gate.approved", event("gate.approved", hash_v=2)) == "malformed"
    assert refused("gate.approved", event("gate.approved", id="not-a-ulid")) == "malformed"
    assert refused("gate.approved", event("gate.approved", based_on=None)) is None  # a first event
    assert (
        refused(
            "verdict.given", event("verdict.given", source_sha=[{"repo": "r", "ref": "m", "sha": "a" * 40, "x": 1}])
        )
        == "malformed"
    )
    assert refused("verdict.given", event("verdict.given", source_sha="abc")) == "malformed"


@pytest.mark.parametrize("ch", ["‮", " ", "\u0085", "‎", "​", " ", "", "͸", "ㅤ", "ᅟ", "ﾠ", "͏", "️", "\x00", "\ud800"])
def test_text_fields_follow_the_8_5_text_rule(ch):
    assert refused("log.added", event("log.added", text=f"a{ch}b")) == "malformed"
    assert refused("ticket.created", event("ticket.created", title=f"a{ch}b")) == "malformed"
    assert (
        refused(
            "question.asked", event("question.asked", question={"to": "assignees", "text": f"a{ch}b", "blocking": True})
        )
        == "malformed"
    )


def test_text_rule_edges():
    assert refused("ticket.created", event("ticket.created", title="a\nb")) == "malformed"  # one line
    assert refused("log.added", event("log.added", text="a\nb")) is None  # multi-line
    assert refused("log.added", event("log.added", text=" a")) == "malformed"
    assert refused("log.added", event("log.added", text="")) == "malformed"
    assert refused("ticket.created", event("ticket.created", title="x" * 201)) == "malformed"
    assert refused("ticket.created", event("ticket.created", title="é" * 100)) is None  # 200 bytes
    assert refused("ticket.created", event("ticket.created", title="é" * 101)) == "malformed"  # 202 bytes


def test_an_unknown_type_is_not_custodial():
    assert refused("member.added", event("log.added")) == "not_custodial"


# --- the hub's action vectors, replayed in event form -----------------------------------------------------------------

_ENVELOPE_NAMES = {
    "v",
    "id",
    "actor",
    "based_on",
    "roster_v",
    "hash_v",
    "gate_gen",
    "policy_hash",
    "source_sha",
    "qid",
    "base_rev",
    "sections",
    "sig",
    "channel_proof",
    "auth",
    "log",
    "type",
}


def _action_cases():
    for c in V["cases"]:
        if (
            c["check"] != "action"
            or c["name"].startswith("action_smuggles_")
            or c["name"] == "action_smuggle_question_with_id"
        ):
            continue
        a, exp = c["args"]["action"], c["expect"]
        if a.get("type") not in PAYLOAD or set(exp) not in ({"ok"}, {"error"}):
            continue
        if "ok" not in exp and exp["error"] != "malformed":
            continue
        if _ENVELOPE_NAMES & (set(a) - {"log", "type"}):
            continue
        if a["type"] != "ticket.created" and not (
            a.get("log") == "workspace" or isinstance(a.get("log"), str) and len(a["log"]) == 26
        ):
            continue  # a bad log is the hub's request framing, not an event field
        yield pytest.param(c, id=c["name"])


def _as_event(a: dict[str, Any]) -> dict[str, Any]:
    typ = a["type"]
    fields = {k: v for k, v in a.items() if k not in ("type", "log")}
    names = {"key", "owner", "base_rev", "from", "qid", "gate_gen", "policy_hash", "source_sha"}
    names |= {"hash"} if typ == "question.asked" else set()  # the hub derives the question's content hash
    derived = {k: v for k, v in VALID[typ].items() if k in names and k not in fields}
    e = {**_COMMON, "type": typ, "actor": {"kind": "person", "id": U1, "via": "x"}, **derived, **fields}
    if typ == "ticket.created":
        e["based_on"] = None
    if typ == "question.asked" and type(e.get("question")) is dict:
        e["question"] = {"id": "Q1", **e["question"]}  # the hub assigns the id from its view
    return e


@pytest.mark.parametrize("case", list(_action_cases()))
def test_the_hub_action_vectors_hold_in_event_form(case):
    a = case["args"]["action"]
    e = _as_event(a)
    if a["type"] == "ticket.updated" and type(a.get("set")) is dict:
        e["base_rev"] = {k: H1 for k in a["set"]} or {"ticket.title": H1}
    if "ok" in case["expect"]:
        assert refused(a["type"], e) is None
    else:
        assert refused(a["type"], e) == "malformed"


def test_the_replay_covers_the_hub_vectors_that_matter():
    names = {p.id for p in _action_cases()}
    assert len(names) >= 45
    assert {
        "action_smuggle_set_tasks_verify_cmd",
        "action_smuggle_visibility_extra_key",
        "action_text_rlo_u202e",
    } <= names

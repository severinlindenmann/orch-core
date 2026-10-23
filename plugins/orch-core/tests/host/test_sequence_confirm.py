"""#1070: the confirmation of a New ticket sequence, op x people role x agent access (pure, no host).

Rules: a plain new ticket and a new ticket with assignees and/or reviewers are a click; an owner step, agent access
and every people edit of an EXISTING ticket keep the typed word (B12D); an unknown kind or op fails closed."""

from __future__ import annotations

import itertools

import pytest

from orch.host import sequence
from tests.host.test_sequence import intent_of

P1, P2 = "p_" + "1" * 32, "p_" + "2" * 32
AGENT = {"hours": 8, "scope": "workable"}


def new_kind(people=None, agent=None, **over):
    extra = {}
    if people:
        extra["people"] = people
    if agent:
        extra["agent"] = agent
    return sequence.confirm_kind(sequence.validate_intent(intent_of(**extra, **over)))


def edit_intent(**over):
    return {
        "v": 1,
        "kind": "edit_ticket",
        "ticket": {"key": "DEMO-0001", "uid": "0" * 26},
        "ticket_type": "feature",
        "head": "sha256:" + "0" * 64,
        "sections": {},
        **over,
    }


def test_a_plain_new_ticket_is_a_click():
    assert new_kind() == "click"
    assert new_kind(acceptance=["It opens"]) == "click"


@pytest.mark.parametrize(
    "people",
    [{"assignees": [P1]}, {"reviewers": [P1]}, {"assignees": [P1], "reviewers": [P1, P2]}],
)
def test_a_new_ticket_with_assignees_or_reviewers_is_a_click(people):
    assert new_kind(people=people) == "click"


@pytest.mark.parametrize(
    "people",
    [{"owner": P1}, {"owner": P1, "assignees": [P2]}, {"owner": P2, "assignees": [P1], "reviewers": [P1]}],
)
def test_a_new_ticket_with_an_owner_step_keeps_the_word(people):
    assert new_kind(people=people) == "word"


@pytest.mark.parametrize("people", [None, {"assignees": [P1]}, {"reviewers": [P1]}, {"owner": P1}])
def test_agent_access_keeps_the_word_whatever_the_people(people):
    assert new_kind(people=people, agent=AGENT) == "word"


@pytest.mark.parametrize("role", ["assignees", "reviewers", "owner"])
def test_a_people_edit_of_an_existing_ticket_keeps_the_word(role):
    intent = sequence.validate_intent(edit_intent(people={role: {"add": [P1], "remove": []}}))
    assert sequence.confirm_kind(intent) == "word"


def test_the_matrix_never_lowers_below_the_strongest_step():
    kinds = {}
    for roles, agent in itertools.product(
        [(), ("assignees",), ("reviewers",), ("owner",), ("assignees", "owner")], (False, True)
    ):
        people = {r: P1 if r == "owner" else [P1] for r in roles}
        kinds[(roles, agent)] = new_kind(people=people, agent=AGENT if agent else None)
    assert {k for k, v in kinds.items() if v == "click"} == {
        ((), False),
        (("assignees",), False),
        (("reviewers",), False),
    }
    assert set(kinds.values()) == {"click", "word"}

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


def new_kind(people=None, agent=None, person=P1, **over):
    extra = {}
    if people:
        extra["people"] = people
    if agent:
        extra["agent"] = agent
    return sequence.confirm_kind(sequence.validate_intent(intent_of(**extra, **over)), person)


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
    [{"assignees": [P1]}, {"reviewers": [P1]}, {"assignees": [P1], "reviewers": [P1]}],
)
def test_a_new_ticket_naming_only_the_signer_is_a_click(people):
    assert new_kind(people=people) == "click"
    assert new_kind(people=people, person=None) == "word"  # no known signer: never lowered


@pytest.mark.parametrize(
    "people",
    [
        {"reviewers": [P2]},
        {"assignees": [P2]},
        {"reviewers": [P1, P2]},
        {"assignees": [P1], "reviewers": [P2]},
        {"assignees": [P2], "reviewers": [P1]},
    ],
)
def test_a_new_ticket_naming_another_member_or_a_mix_keeps_the_word(people):
    assert new_kind(people=people) == "word"


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
    assert sequence.confirm_kind(intent, P1) == "word"  # even naming the signer: an edit of an existing ticket


def test_the_matrix_op_x_people_role_x_who_x_agent():
    who = {"self": [P1], "other": [P2], "mix": [P1, P2]}
    click = set()
    for role, w, agent in itertools.product(("assignees", "reviewers"), who, (False, True)):
        if new_kind(people={role: who[w]}, agent=AGENT if agent else None) == "click":
            click.add((role, w, agent))
    assert click == {("assignees", "self", False), ("reviewers", "self", False)}


def test_the_owners_draft_creator_as_assignee_and_reviewer_no_owner_step_is_a_click():
    """#1070 exact case: the dashboard drops the owner step when the owner is the creator (`sequencePeople`), so the
    host sees the creator as assignee and reviewer only, plus an estimate and requirements text."""
    me = "p_" + "1" * 32
    kind = new_kind(
        people={"assignees": [me], "reviewers": [me]},
        set={"size": "xs"},
        sections={"requirements": "It works."},
    )
    assert kind == "click"

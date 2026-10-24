"""The confirmation of a sequence, op x people role x agent access (pure, no host).

#1198 C1: there is no typed word. Every sequence (people, owner step, agent access, edit of an existing ticket) is a
click; only a terminal-only step (Publish) makes it terminal."""

from __future__ import annotations

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
    [
        {"assignees": [P1]},
        {"reviewers": [P2]},
        {"reviewers": [P1, P2]},
        {"assignees": [P1], "reviewers": [P2]},
        {"owner": P1},
        {"owner": P2, "assignees": [P1], "reviewers": [P1]},
    ],
)
def test_every_people_change_of_a_new_ticket_is_a_click(people):
    assert new_kind(people=people) == "click"


@pytest.mark.parametrize("people", [None, {"assignees": [P1]}, {"reviewers": [P1]}, {"owner": P1}, {"reviewers": [P2]}])
def test_agent_access_is_a_click_whatever_the_people(people):
    assert new_kind(people=people, agent=AGENT) == "click"


@pytest.mark.parametrize("role", ["assignees", "reviewers", "owner"])
def test_a_people_edit_of_an_existing_ticket_is_a_click(role):
    intent = sequence.validate_intent(edit_intent(people={role: {"add": [P1], "remove": []}}))
    assert sequence.confirm_kind(intent) == "click"


def test_a_terminal_only_step_makes_the_sequence_terminal():
    """The class of a step is that of its op with its args; Publish's settings are a click since #1198 C2."""
    intent = sequence.validate_intent(intent_of())
    assert sequence.confirm_kind(intent) == "click"
    from orch.click.protocol import op_class

    assert op_class("settings.set", args={"set": {"publish": None}}) == "click"
    assert op_class("grant") == "terminal"  # an unknown or grant op fails closed

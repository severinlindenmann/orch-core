"""Visibility (T12, §9)."""

import pytest

from orch.model import Code
from tests.model.world import World, refused


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member"})


def test_default_is_workspace_visible_to_every_member(w):
    uid = w.ticket()
    s = w.state()
    assert all(uid in s.visible_to(w.people[n]) for n in ("sev", "mara", "tom"))
    assert uid not in s.visible_to("p_" + "0" * 32)


def test_restricted_is_visible_to_the_list_only(w):
    uid = w.ticket()
    w.tev(uid, "visibility.changed", "sev", visibility={"restricted": sorted([w.people["sev"], w.people["tom"]])})
    s = w.state()
    assert uid in s.visible_to(w.people["tom"]) and uid not in s.visible_to(w.people["mara"])
    assert s.tickets[uid].visibility["restricted"] == tuple(sorted([w.people["sev"], w.people["tom"]]))


def test_who_changes_visibility_and_to_whom(w):
    uid = w.ticket("tom")
    assert refused(w, uid, "visibility.changed", "tom", visibility={"restricted": [w.people["tom"]]}) is None
    other = w.ticket("sev")
    assert refused(w, other, "visibility.changed", "tom", visibility="workspace") == Code.ROLE_DENIED
    assert (
        refused(w, other, "visibility.changed", "mara", visibility={"restricted": ["p_" + "1" * 32]})
        == Code.MEMBER_UNKNOWN
    )


def test_a_member_who_cannot_see_the_ticket_cannot_decide_or_answer_on_it(w):
    uid = w.ticket()
    w.fill(uid)
    w.wev(
        "policy.changed",
        "sev",
        gates={"requirements": __import__("tests.model.world", fromlist=["pol"]).pol(["maintainer", "owner"])},
    )
    w.tev(uid, "visibility.changed", "sev", visibility={"restricted": [w.people["sev"]]})
    assert w.decide(uid, "mara", "requirements", try_=True)[1].code == Code.TICKET_NOT_VISIBLE
    assert w.decide(uid, "sev", "requirements", try_=True)[1].__class__.__name__ == "Ok"


def test_visibility_is_not_a_gate_bound_field(w):
    uid = w.ticket()
    w.fill(uid)
    before = {g: v.hash for g, v in w.view(uid).gates.items()}
    w.tev(uid, "visibility.changed", "sev", visibility={"restricted": [w.people["sev"]]})
    assert {g: v.hash for g, v in w.view(uid).gates.items()} == before

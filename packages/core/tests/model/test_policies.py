"""§5.7 policies: the effective policy is workspace ∩ override, recomputed, never looser."""

import pytest

from orch import canon
from orch.model import Code
from orch.model.policies import intersect
from tests.model.world import World, pol, refused


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member"})


def eff(w, uid, gate):
    return {k: list(v) if isinstance(v, tuple) else v for k, v in w.view(uid).gates[gate].policy.items()}


def test_intersection_rules():
    ws = pol(["owner", "maintainer"], 1, ["watchers"], "all", False)
    ov = pol(["maintainer", "member"], 2, ["assignees"], "all", True)
    assert intersect(ws, ov) == {
        "approvers": ["maintainer"],
        "count": 2,
        "not": ["assignees", "watchers"],
        "applies": "all",
        "independent": True,
    }


@pytest.mark.parametrize(
    ("a", "b", "out"),
    [
        ("all", "off", "all"),
        ("off", "all", "all"),
        ("off", "off", "off"),
        ("off", ["bug"], ["bug"]),
        (["feature"], ["bug"], ["bug", "feature"]),
        (["bug"], "all", "all"),
        (["bug"], ["bug"], ["bug"]),
    ],
)
def test_applies_is_a_union(a, b, out):
    assert intersect(pol(applies=a), pol(applies=b))["applies"] == out


def test_workspace_default_and_override_in_events(w):
    uid = w.ticket()
    assert eff(w, uid, "verify") == pol(["reviewers"], 1, ["assignees"])
    assert eff(w, uid, "code")["applies"] == "off"
    w.tev(uid, "policy.changed", "sev", gates={"verify": pol(["reviewers"], 2, ["watchers"])})
    assert eff(w, uid, "verify") == pol(["reviewers"], 2, ["assignees", "watchers"])


def test_override_cannot_end_up_looser_even_after_a_workspace_change(w):
    uid = w.ticket()
    w.tev(uid, "policy.changed", "sev", gates={"plan": pol(["owner"], 2)})
    w.wev("policy.changed", "sev", gates={"plan": pol(["owner", "maintainer"], 1)})
    assert eff(w, uid, "plan") == pol(["owner"], 2)
    w.wev("policy.changed", "sev", gates={"plan": pol(["owner", "maintainer"], 3)})
    assert eff(w, uid, "plan")["count"] == 3


def test_override_leaving_no_token_is_refused_but_a_later_workspace_change_only_blocks(w):
    uid = w.ticket()
    assert refused(w, uid, "policy.changed", "sev", gates={"plan": pol(["maintainer"])}) == Code.GATE_NO_ELIGIBLE
    w.tev(uid, "policy.changed", "sev", gates={"plan": pol(["owner"])})
    w.wev("policy.changed", "sev", gates={"plan": pol(["maintainer"])})
    g = w.view(uid).gates["plan"]
    assert g.blocked and g.approved is False
    w.wev("policy.changed", "sev", gates={"plan": pol(["owner", "maintainer"])})
    assert not w.view(uid).gates["plan"].blocked


def test_code_gate_off_by_default_and_override_can_turn_it_on_for_one_ticket(w):
    a, b = w.ticket(), w.ticket()
    w.tev(a, "policy.changed", "sev", gates={"code": pol(["owner"], 1, ["assignees"], "all", True)})
    assert w.view(a).gates["code"].applies and not w.view(b).gates["code"].applies


def test_applies_by_type_and_policy_hash_is_the_canon_hash(w):
    w.wev("policy.changed", "sev", gates={"plan": pol(["owner"], 1, [], ["bug", "chore"])})
    feature, bug = w.ticket(type_="feature"), w.ticket(type_="bug")
    assert not w.view(feature).gates["plan"].applies and w.view(bug).gates["plan"].applies
    g = w.view(bug).gates["plan"]
    assert g.policy_hash == canon.policy_hash("plan", eff(w, bug, "plan"))


def test_who_may_change_policies(w):
    uid = w.ticket("tom")
    assert refused(w, uid, "policy.changed", "tom", gates={"plan": pol(["owner"], 2)}) is None  # the ticket owner
    other = w.ticket("sev")
    assert refused(w, other, "policy.changed", "tom", gates={"plan": pol(["owner"], 2)}) == Code.ROLE_DENIED
    assert refused(w, other, "policy.changed", "mara", gates={"plan": pol(["owner"], 2)}) is None
    assert refused(w, "workspace", "policy.changed", "mara", gates={"plan": pol(["owner"], 2)}) == Code.ROLE_DENIED

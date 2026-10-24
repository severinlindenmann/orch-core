"""V3 C6b: a selective workspace puts the ``f1`` block (hub contract 10.2b) with the real roster version and gate /
question hashes, and puts every item again under a higher ``item_version`` when a member change raises ``roster_v``."""

from __future__ import annotations

import pytest

from orch.hubclient import sharefmt
from tests.host.human_helpers import (
    Human,
    lab,  # noqa: F401
)
from tests.host.live_helpers import World
from tests.host.test_hub_ops import keystore  # noqa: F401
from tests.host.test_share_ops import _join, agent, dev_workspace, ok, req  # noqa: F401


@pytest.fixture
def world(lab):  # noqa: F811
    w = World(lab)
    try:
        yield w
    finally:
        w.close()


@pytest.fixture
def hub(world):
    h, sh = _join(world, Human(world), "selective")
    try:
        yield sh
    finally:
        h.close()


def confirm_sync(w):
    """``share.sync`` confirming the selection the owner was just shown (C6 follow-up: a sync without ``expect`` only
    retries what was confirmed)."""
    plan = ok(req(w, "share.preview"))["data"]["plan"]
    return ok(req(w, "share.sync", {"expect": plan}))["data"]


def test_the_put_carries_the_roster_version_and_the_question_hash(world, hub):
    from orch import canon

    _, key = world.ticket("Savings")
    ok(agent(world, "ask", {"ref": key, "text": "which bank?", "text_question": True}))
    assert confirm_sync(world)["put"] == 2  # the needs-you summary (no f1) and the question item
    (item,) = [i for i in hub.opened if i["item_kind"] == "question"]
    roster = world.service.store.state.workspace.roster_v
    q = world.service.store.state.tickets[item["chain"]["log"]].questions[0]
    assert item["f1"] == {"roster_v": roster, "gates": {}, "questions": {"Q1": {"hash": q.hash}}}
    raw = {"id": "Q1", "to": "ticket_owner", "text": "which bank?", "blocking": True, "kind": "text"}
    assert q.hash == canon.question_hash(q.qid, item["chain"]["log"], raw)
    sharefmt.check_f1(item["f1"], item["chain"])


def test_a_member_change_re_puts_every_item_under_a_higher_version(world, hub):
    _, key = world.ticket("Savings")
    ok(agent(world, "ask", {"ref": key, "text": "which bank?", "text_question": True}))
    rule = {"kind": "questions"}
    plan = ok(req(world, "share.preview", {"add": rule}))["data"]["plan"]
    ok(req(world, "share.rule_add", {"rule": rule, "expect": plan}))
    assert hub.count("share_put") == 2  # the question item, the needs-you summary
    before = world.service.store.state.workspace.roster_v
    assert ok(req(world, "share.sync"))["data"]["put"] == 0  # nothing changed: nothing re-sent
    world.person_event("role.changed", person=world.quinn.ref, role="maintainer")  # a member change
    after = world.service.store.state.workspace.roster_v
    assert after == before + 1
    got = ok(req(world, "share.sync"))["data"]
    # only the question item has an f1 to refresh; the needs-you summary has none and stays as it is
    assert got["put"] == 1 and hub.count("share_put") == 3
    assert [i["item_version"] for i in hub.opened[2:]] == [2]
    assert {i["f1"]["roster_v"] for i in hub.opened[2:]} == {after}


def test_the_gate_facts_are_the_models_gate_hashes(world, hub):
    from orch.host import shareops

    uid, _ = world.ticket("Plan me")
    (t,) = [t for t in shareops.tickets_for(world.service) if t.uid == uid]
    view = world.service.store.state.tickets[uid]
    assert {g.gate for g in t.gates} == {"requirements", "plan", "code", "verify"}
    for g in t.gates:
        gv = view.gates[g.gate]
        assert (g.gen, g.hash, g.policy_hash) == (gv.gen, gv.hash, gv.policy_hash)
        assert (g.source_sha is None) == (g.gate not in ("code", "verify"))
    assert t.roster_v == world.service.store.state.workspace.roster_v

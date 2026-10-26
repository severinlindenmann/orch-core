"""Opus review of C3b PR B (core#1038, l2-c3bB): host admission findings, on the real served path."""

from __future__ import annotations

from orch.host import hubdeliver
from tests.host import human_helpers
from tests.host.test_hub_deliver import ALICE, env  # noqa: F401 - fixture
from tests.host.test_hub_ops import dev_workspace, stub  # noqa: F401 - fixtures

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def test_a_demotion_reaches_the_model_at_once_even_when_the_snapshot_also_adds_someone(env):  # noqa: F811
    """ALICE is hub admin (snapshot 3, pinned with the first event). The hub then demotes her to edit and, in the same
    snapshot 4, adds another user. The host holds 4 and its pre-check sees edit; the pin of 4 is rate-limited, so the
    model judges by 3 (admin -> owner) and lets her close a ticket she does not own, which an edit (member) may not."""
    from tests.host.test_hub_deliver import hub_snapshot

    assert env.deliver(env.event()) is None  # pins card + snapshot 3
    env.state.snapshot = hub_snapshot(env.wid, version=4, roles={ALICE: "edit", "b" * 32: "read"})
    out = env.deliver(env.event("ticket.closed", resolution="wont_do"))
    assert out == "forbidden_role"  # as it is when snapshot 4 is pinned


def test_control_a_pure_demotion_pins_at_once_and_refuses(env):  # noqa: F811
    from tests.host.test_hub_deliver import hub_snapshot

    assert env.deliver(env.event()) is None
    env.state.snapshot = hub_snapshot(env.wid, version=4, roles={ALICE: "edit"})
    assert env.deliver(env.event("ticket.closed", resolution="wont_do")) == "forbidden_role"


def test_a_withdrawn_item_is_not_live_for_a_decision(env, monkeypatch):  # noqa: F811
    """share.revoke puts the item in ``withdrawn`` first; ``sent`` loses it only after the hub acknowledges
    ``share_revoke``. A hub that refuses or drops that call keeps the item "live" for selective admission."""
    from orch.host import shareops
    from orch.hubclient.shares import ShareState

    h = "sha256:" + "1" * 64
    st = ShareState(
        sent={"ticket:x": {"item_id": "i", "digest": "d", "version": 1, "item_hash": h,
                           "chain": {"log": env.uid, "seq": 1, "head": "h"}, "f1": {"roster_v": 1}}},
        withdrawn=["ticket:x"],
    )  # fmt: skip
    monkeypatch.setattr(shareops, "_load", lambda service: st)
    live, _ = hubdeliver.StoreLanding(env.service, None, env.state).shared_items(env.uid)
    assert h not in live

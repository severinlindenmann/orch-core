"""V3 C2a: how the model judges ``hub.joined`` and ``hub.left`` (hub contract 9.1): owner only, selective only, a
strictly higher ``binding_seq``, no alias re-pointed, ``hub.left`` names what is joined, and a hub event never adds a
member or touches the member list."""

from __future__ import annotations

import copy

import pytest

from orch.model import workspace
from orch.model.codes import Code
from orch.model.types import Member, WsCore

HUB = "38d8abed52ccfb538da401fd54e64bdc"
ROOT = "BI_UMykYqAqu1C9i1pPLsPGXktAHAqZlKB_EpiicGtikqSoV9hdFn8S3nWlSvAmzbCp1iQ79iiLjD9tWA1p39ik"
ALICE, BOB = "4cfef66f1dc7bee794e9d949f2a216c3", "ac8bdff07f55747177ffd4835cfa5e41"
U1, U2 = "a368bd380588e5dabee62a516031e21f", "9124812ba8533a024bee116af55a7da0"


def ws() -> WsCore:
    core = WsCore(created=True)
    core.members = {
        "p_" + ALICE: Member("p_" + ALICE, "alice", "owner", "k"),
        "p_" + BOB: Member("p_" + BOB, "bob", "member", "k"),
    }
    return core


def joined(seq=1, actor=ALICE, mode="selective", aliases=None, hub=HUB):
    default = [{"person_id": ALICE, "user_id": U1}]
    return {
        "type": "hub.joined", "actor": {"kind": "person", "id": "p_" + actor}, "hub_id": hub, "hk_root_pub": ROOT,
        "mode": mode, "binding_seq": seq, "aliases": default if aliases is None else aliases,
    }  # fmt: skip


def left(seq=1, actor=ALICE, hub=HUB):
    return {"type": "hub.left", "actor": {"kind": "person", "id": "p_" + actor}, "hub_id": hub, "binding_seq": seq}


def test_a_join_records_the_hub_and_nothing_else():
    w = ws()
    members = copy.deepcopy(w.members)
    assert workspace.hub_joined(w, joined()) is None
    assert w.hub["status"] == "joined" and w.hub["mode"] == "selective" and w.hub["binding_seq"] == 1
    assert w.hub["aliases"] == {ALICE: U1}
    assert w.members == members  # no hub member
    assert workspace.hub_left(w, left()) is None
    assert w.hub["status"] == "left" and w.hub["binding_seq"] == 1  # the high-water mark stays


@pytest.mark.parametrize("fn,ev", [(workspace.hub_joined, joined), (workspace.hub_left, left)])
def test_only_an_owner_signs_it(fn, ev):
    w = ws()
    workspace.hub_joined(w, joined())
    r = fn(w, ev(actor=BOB))
    assert r is not None and r.code == Code.ROLE_DENIED


def test_full_mode_is_refused_by_the_model_too():
    w = ws()
    r = workspace.hub_joined(w, joined(mode="full"))
    assert r is not None and r.code == Code.HUB_INVALID and "C2b" in r.detail
    assert w.hub == {}


def test_a_second_join_needs_a_leave_and_a_higher_seq():
    w = ws()
    workspace.hub_joined(w, joined())
    assert workspace.hub_joined(w, joined(seq=2)).code == Code.HUB_INVALID  # already joined
    workspace.hub_left(w, left())
    assert workspace.hub_joined(w, joined(seq=1)).code == Code.HUB_INVALID  # a binding never comes back
    assert workspace.hub_joined(w, joined(seq=2)) is None and w.hub["status"] == "joined"


def test_hub_left_must_name_the_joined_hub_and_binding():
    w = ws()
    assert workspace.hub_left(w, left()).code == Code.HUB_INVALID  # nothing joined
    workspace.hub_joined(w, joined())
    assert workspace.hub_left(w, left(seq=2)).code == Code.HUB_INVALID
    assert workspace.hub_left(w, left(hub="0" * 32)).code == Code.HUB_INVALID
    assert workspace.hub_left(w, left()) is None
    assert workspace.hub_left(w, left()).code == Code.HUB_INVALID  # and not twice


def test_an_alias_is_never_repointed_across_joins():
    w = ws()
    workspace.hub_joined(w, joined())
    workspace.hub_left(w, left())
    assert (
        workspace.hub_joined(w, joined(seq=2, aliases=[{"person_id": ALICE, "user_id": U2}])).code == Code.HUB_INVALID
    )
    assert workspace.hub_joined(w, joined(seq=2, aliases=[{"person_id": BOB, "user_id": U1}])).code == Code.HUB_INVALID
    assert workspace.hub_joined(w, joined(seq=2, aliases=[{"person_id": BOB, "user_id": U2}])) is None
    assert w.hub["aliases"] == {ALICE: U1, BOB: U2}


def test_an_alias_names_a_member():
    w = ws()
    r = workspace.hub_joined(w, joined(aliases=[{"person_id": "1" * 32, "user_id": U2}]))
    assert r is not None and r.code == Code.HUB_INVALID


def test_a_replay_of_a_join_for_another_workspace_state_is_refused():
    """The same signed event replayed after the hub was left (or on a joined workspace) never applies twice."""
    w = ws()
    ev = joined()
    assert workspace.hub_joined(w, ev) is None
    assert workspace.hub_joined(w, ev).code == Code.HUB_INVALID
    workspace.hub_left(w, left())
    assert workspace.hub_joined(w, ev).code == Code.HUB_INVALID

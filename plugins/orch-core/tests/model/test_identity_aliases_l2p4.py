"""L2 phase-4 review (state/l2-review-p4.md, L2P4-C1): aliases are not scoped to the hub they were made for.

``user_id`` is 16 random bytes chosen by a hub (hub contract 2.3), so ``u_x`` names a user only together with a
``hub_id``. ``ws.hub["aliases"]`` survives ``hub.left`` and is carried into a join of another hub, and
``identity.canonical`` maps ``u_x`` to the aliased person whatever hub is joined now. Both tests are xfail(strict)
while the finding is open."""

import pytest

from orch.crypto import suite
from orch.crypto.encoding import b64u
from orch.hubclient import wire
from orch.model import gates, identity
from orch.model.codes import Code
from tests.model.test_identity_aliases import HUB, ROOT, U_MARA, alias, core, decision_event, join
from tests.model.world import World, pol

U_MARA_ON_B = "5d0e3c8a0f1b2c3d4e5f60718293a4b5"


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member"})


def _hub_b():
    pub = suite.public_bytes(suite.generate_private_key())
    return wire.hub_id(pub), b64u(pub)


def _switch_to_hub_b(w, aliases):
    _, ok = join(w, [alias(w, "mara", U_MARA)])
    assert not hasattr(ok, "code")
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    hub_b, root_b = _hub_b()
    assert hub_b != HUB and root_b != ROOT
    return _join_b(w, aliases, hub_b, root_b)


def test_l2p4_c1_hub_b_starts_with_no_aliases_of_its_own_and_a_has_not_been_forgotten(w):
    _, r = _switch_to_hub_b(w, [])
    assert not hasattr(r, "code")
    ws = core(w).ws
    # flipped by L2P4-C1b: the book is a union, so hub A's id keeps meaning mara; hub B minting it is C3's host check
    assert identity.canonical(ws, "u_" + U_MARA) == w.people["mara"]


def test_l2p4_c1_owner_can_alias_their_user_on_a_new_hub(w):
    _, r = _switch_to_hub_b(w, [alias(w, "mara", U_MARA_ON_B)])
    assert not hasattr(r, "code"), r
    assert identity.canonical(core(w).ws, "u_" + U_MARA_ON_B) == w.people["mara"]


def test_l2p4_c1_rejoin_of_the_same_hub_restores_its_aliases_and_never_repoints(w):
    _, ok = join(w, [alias(w, "mara", U_MARA)])
    assert not hasattr(ok, "code")
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    assert identity.canonical(core(w).ws, "u_" + U_MARA) == w.people["mara"]  # left: history still merges (C1b)
    _, r = w.try_(
        "workspace", "hub.joined", "sev", hub_id=HUB, hk_root_pub=ROOT, mode="selective", binding_seq=2, aliases=[]
    )
    assert not hasattr(r, "code"), r
    assert identity.canonical(core(w).ws, "u_" + U_MARA) == w.people["mara"]
    _, bad = w.try_("workspace", "hub.left", "sev", hub_id=HUB, binding_seq=2)
    assert not hasattr(bad, "code")
    _, r = w.try_(
        "workspace",
        "hub.joined",
        "sev",
        hub_id=HUB,
        hk_root_pub=ROOT,
        mode="selective",
        binding_seq=3,
        aliases=[alias(w, "mara", "5d0e3c8a0f1b2c3d4e5f60718293a4b5")],
    )
    assert getattr(r, "code", None) is not None  # re-pointed on the same hub: refused


def _join_b(w, aliases, hub_b, root_b):
    return w.try_(
        "workspace",
        "hub.joined",
        "sev",
        hub_id=hub_b,
        hk_root_pub=root_b,
        mode="selective",
        binding_seq=2,
        aliases=sorted(aliases, key=lambda a: a["person_id"]),
    )


def test_l2p4_c1b_history_of_a_left_hub_still_merges(w):
    _, ok = join(w, [alias(w, "mara", U_MARA)])
    assert not hasattr(ok, "code")
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    assert identity.canonical(core(w).ws, "u_" + U_MARA) == w.people["mara"]  # left: history still merges
    hub_b, root_b = _hub_b()
    _, r = _join_b(w, [], hub_b, root_b)
    assert not hasattr(r, "code")
    assert identity.canonical(core(w).ws, "u_" + U_MARA) == w.people["mara"]  # another hub joined: still merges


def test_l2p4_c1b_alias_conflicts_with_another_hubs_alias(w):
    # hub B may not alias hub A's user id to a different person
    _, r = _switch_to_hub_b(w, [alias(w, "tom", U_MARA)])
    assert r.code == Code.HUB_INVALID and "another hub" in r.detail
    assert identity.canonical(core(w).ws, "u_" + U_MARA) == w.people["mara"]
    # the same person on another hub may reuse the id (still a function)
    hub_b, root_b = _hub_b()
    _, ok = _join_b(w, [alias(w, "mara", U_MARA)], hub_b, root_b)
    assert not hasattr(ok, "code"), ok


def test_l2p4_c1b_d59_worker_recorded_as_user_still_blocks_the_person_after_leave_and_rejoin_elsewhere(w):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1, [], "all", True)})
    uid = w.ticket()
    w.fill(uid)
    join(w, [alias(w, "mara", U_MARA)])
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)

    def refused():
        c = core(w)  # replayed fresh each time; no signing path for u_ actors yet (C3): record the worker directly
        c.tickets[uid].workers.add("u_" + U_MARA)
        r = gates.decision(c.ws, c.tickets[uid], decision_event(w.people["mara"]))
        return r.code == Code.GATE_NOT_ELIGIBLE and "worker" in r.detail

    assert refused()  # after the leave
    hub_b, root_b = _hub_b()
    assert not hasattr(_join_b(w, [], hub_b, root_b)[1], "code")
    assert refused()  # after joining another hub

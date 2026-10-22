"""C3 (L2P4-C1): ``identity.hub_actor`` is the one place the actor of an incoming ``auth: "hub"`` event is resolved.

A user id is random and chosen by a hub. ``canonical`` merges ``u_x`` with its aliased ``p_y`` over EVERY hub ever
joined (so history keeps meaning the same person, D59), which would let hub B mint hub A's user id and act as the
person it was aliased to. ``hub_actor`` refuses that. The D59 tests at the end are the end-to-end test the C4 review
asked for: a worker known only as a hub user id, a leave, another hub, and the person's own approval."""

import pytest

from orch.model import Code, Refusal, gates, identity
from tests.model.test_identity_aliases import HUB, U_MARA, U_TOM, alias, core, decision_event, join
from tests.model.test_identity_aliases_l2p4 import _hub_b, _join_b
from tests.model.world import World, pol


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member"})


def join_other(w, aliases, hub_id, root, seq):
    return w.try_(
        "workspace", "hub.joined", "sev", hub_id=hub_id, hk_root_pub=root, mode="selective", binding_seq=seq,
        aliases=sorted(aliases, key=lambda a: a["person_id"]),
    )  # fmt: skip


def _hub_a_then_b(w, a_aliases, b_aliases=()):
    """Join the vector hub (A) with ``a_aliases``, leave it, join a fresh hub B with ``b_aliases``."""
    assert not hasattr(join(w, a_aliases)[1], "code")
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    hub_b, root_b = _hub_b()
    assert not hasattr(_join_b(w, list(b_aliases), hub_b, root_b)[1], "code")
    return hub_b


def test_an_aliased_user_of_the_joined_hub_resolves_to_its_person(w):
    join(w, [alias(w, "mara", U_MARA)])
    assert identity.hub_actor(core(w).ws, HUB, U_MARA) == w.people["mara"]


def test_an_unaliased_new_user_maps_to_itself(w):
    join(w, [alias(w, "mara", U_MARA)])
    assert identity.hub_actor(core(w).ws, HUB, U_TOM) == "u_" + U_TOM


def test_hub_b_cannot_mint_hub_a_user_id(w):
    hub_b = _hub_a_then_b(w, [alias(w, "mara", U_MARA)])
    ws = core(w).ws
    assert identity.canonical(ws, "u_" + U_MARA) == w.people["mara"]  # history: still mara (the union)
    r = identity.hub_actor(ws, hub_b, U_MARA)  # ...but hub B may not act with that id
    assert isinstance(r, Refusal) and r.code == Code.HUB_FOREIGN_USER
    assert identity.hub_actor(ws, hub_b, U_TOM) == "u_" + U_TOM  # B's own, unaliased users are fine


def test_the_same_person_aliased_on_both_hubs_is_ok(w):
    hub_b = _hub_a_then_b(w, [alias(w, "mara", U_MARA)], [alias(w, "mara", U_MARA)])
    assert identity.hub_actor(core(w).ws, hub_b, U_MARA) == w.people["mara"]


def test_a_hub_that_is_not_the_joined_one_is_refused(w):
    ws = core(w).ws
    assert isinstance(identity.hub_actor(ws, HUB, U_MARA), Refusal)  # no hub at all
    join(w, [alias(w, "mara", U_MARA)])
    other, _ = _hub_b()
    assert isinstance(identity.hub_actor(core(w).ws, other, U_MARA), Refusal)
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    assert isinstance(identity.hub_actor(core(w).ws, HUB, U_MARA), Refusal)  # left: no new events from it


@pytest.mark.parametrize("bad", ["", "u_" + U_MARA, U_MARA.upper(), U_MARA[:-1], U_MARA + "0", None, 5])
def test_malformed_user_ids_are_refused(w, bad):
    join(w, [alias(w, "mara", U_MARA)])
    assert isinstance(identity.hub_actor(core(w).ws, HUB, bad), Refusal)


# --- D59 end to end, with a hub user as the actor --------------------------------------------------------------------


def _record_hub_worker(w, uid, hub_id, user_id):
    """What a custodial event of ``user_id`` does to a ticket once it is admitted: its actor is resolved through
    ``hub_actor`` and the ticket's workers keep that identity as recorded (a ``u_`` id when it was not aliased)."""
    c = core(w)
    who = identity.hub_actor(c.ws, hub_id, user_id)
    assert not isinstance(who, Refusal), who
    c.tickets[uid].workers.add(who)
    return c, who


def _independent_requirements(w):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1, [], "all", True)})
    uid = w.ticket()
    w.fill(uid)
    return uid


def test_d59_worker_known_only_as_a_hub_user_blocks_the_person_after_the_alias_arrives(w):
    """mara works as the hub user ``u_x`` while nothing aliases it; the owner later aliases u_x to mara (a rejoin);
    mara's own approval of the independent gate is refused: u_x and p_mara are one person."""
    uid = _independent_requirements(w)
    assert not hasattr(join(w, [])[1], "code")
    c, who = _record_hub_worker(w, uid, HUB, U_MARA)
    assert who == "u_" + U_MARA  # unaliased then: recorded as the hub user
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    assert not hasattr(join(w, [alias(w, "mara", U_MARA)], seq=2)[1], "code")
    c = core(w)
    c.tickets[uid].workers.add("u_" + U_MARA)  # replay of the recorded worker
    r = gates.decision(c.ws, c.tickets[uid], decision_event(w.people["mara"]))
    assert r.code == Code.GATE_NOT_ELIGIBLE and "worker" in r.detail


def test_d59_holds_after_the_leave_and_a_second_hub_and_the_second_hub_cannot_launder_it(w):
    uid = _independent_requirements(w)
    hub_b = _hub_a_then_b(w, [alias(w, "mara", U_MARA)])
    c = core(w)
    c.tickets[uid].workers.add("u_" + U_MARA)  # recorded while hub A was joined, before the leave
    r = gates.decision(c.ws, c.tickets[uid], decision_event(w.people["mara"]))
    assert r.code == Code.GATE_NOT_ELIGIBLE and "worker" in r.detail
    # hub B minting hub A's id to act as mara is refused at the door, so it cannot even reach this check
    assert isinstance(identity.hub_actor(c.ws, hub_b, U_MARA), Refusal)
    # and the person acting directly as p_mara is exactly the same refusal
    assert identity.canonical(c.ws, "u_" + U_MARA) == w.people["mara"]


def test_d59_a_hub_actor_cannot_approve_what_its_person_worked_on(w):
    """The other direction: the person is the worker, the approval arrives as the aliased hub user."""
    uid = _independent_requirements(w)
    join(w, [alias(w, "mara", U_MARA)])
    c = core(w)
    c.tickets[uid].workers.add(w.people["mara"])
    who = identity.hub_actor(c.ws, HUB, U_MARA)
    r = gates.decision(c.ws, c.tickets[uid], decision_event(who))
    assert r.code == Code.GATE_NOT_ELIGIBLE and "worker" in r.detail

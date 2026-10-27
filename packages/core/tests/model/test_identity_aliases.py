"""C4 (D59): a hub user ``u_x`` and its aliased person ``p_x`` are one person in every separation-of-duties check."""

import pytest

from orch.model import Code, gates, generations, identity, policies
from orch.model.types import Decision
from tests.model.world import World, pol

HUB = "38d8abed52ccfb538da401fd54e64bdc"
ROOT = "BI_UMykYqAqu1C9i1pPLsPGXktAHAqZlKB_EpiicGtikqSoV9hdFn8S3nWlSvAmzbCp1iQ79iiLjD9tWA1p39ik"
U_MARA, U_TOM = "a368bd380588e5dabee62a516031e21f", "9124812ba8533a024bee116af55a7da0"


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer", "tom": "member"})


def join(w, aliases, seq=1, **kw):
    return w.try_(
        "workspace",
        "hub.joined",
        "sev",
        hub_id=HUB,
        hk_root_pub=ROOT,
        mode="selective",
        binding_seq=seq,
        aliases=sorted(aliases, key=lambda a: a["person_id"]),
        **kw,
    )


def alias(w, name, uid):
    return {"person_id": w.people[name][2:], "user_id": uid}


def core(w):
    return w.state()._core


def decision_event(who, gate="requirements", typ="gate.approved"):
    return {"type": typ, "gate": gate, "actor": {"kind": "person", "id": who, "device": "d_x"}}


def test_canonical_maps_an_aliased_user_to_its_person_and_nothing_else(w):
    join(w, [alias(w, "mara", U_MARA)])
    ws = core(w).ws
    assert identity.canonical(ws, "u_" + U_MARA) == w.people["mara"]
    assert identity.canonical(ws, "u_" + U_TOM) == "u_" + U_TOM  # not aliased: itself
    assert identity.canonical(ws, w.people["mara"]) == w.people["mara"]


def test_no_hub_means_identity_function_d79(w):
    ws = core(w).ws
    assert not ws.hub
    assert identity.canonical(ws, "u_" + U_MARA) == "u_" + U_MARA
    assert identity.canonical(ws, w.people["tom"]) == w.people["tom"]


def test_assignee_cannot_approve_via_the_alias(w):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1, [], "all", True)})
    uid = w.ticket()
    w.fill(uid)
    w.tev(uid, "people.changed", "sev", role="assignees", add=[w.people["mara"]], remove=[])
    join(w, [alias(w, "mara", U_MARA)])
    c = core(w)
    r = gates.decision(c.ws, c.tickets[uid], decision_event("u_" + U_MARA))
    assert r.code == Code.GATE_NOT_ELIGIBLE and "worker" in r.detail
    # the same refusal via the person id (control) and a non-aliased user is not caught by independence
    r2 = gates.decision(c.ws, c.tickets[uid], decision_event(w.people["mara"]))
    assert r2.code == Code.GATE_NOT_ELIGIBLE


def test_not_assignees_token_applies_to_the_alias(w):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1, ["assignees"])})
    uid = w.ticket()
    w.tev(uid, "people.changed", "sev", role="assignees", add=[w.people["mara"]], remove=[])
    join(w, [alias(w, "mara", U_MARA)])
    c = core(w)
    t = c.tickets[uid]
    assert "assignees" in policies.person_tokens(c.ws, t, "u_" + U_MARA)
    assert not policies.may_decide(c.ws, t, "requirements", "u_" + U_MARA)
    assert not policies.may_decide(c.ws, t, "requirements", w.people["mara"])


def test_agent_worker_for_person_blocks_the_alias(w):
    # a person who worked via an agent (independent) cannot approve through the other identity
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1, [], "all", True)})
    uid = w.ticket()
    w.fill(uid)
    join(w, [alias(w, "mara", U_MARA)])
    c = core(w)
    c.tickets[uid].workers.add(w.people["mara"])
    r = gates.decision(c.ws, c.tickets[uid], decision_event("u_" + U_MARA))
    assert r.code == Code.GATE_NOT_ELIGIBLE and "worker" in r.detail


def test_requester_cannot_verify_own_work_via_the_alias(w):
    # verify/code: the claim holder (worker) is the person; verdict by their alias is refused
    w.wev("policy.changed", "sev", gates={"code": pol(["maintainer", "owner"], 1, ["assignees"], "all", True)})
    uid = w.ticket()
    w.to_testing(uid, "mara")
    join(w, [alias(w, "mara", U_MARA)])
    c = core(w)
    t = c.tickets[uid]
    assert w.people["mara"] in t.workers
    for gate, typ in (("verify", "verdict.given"), ("code", "gate.approved")):
        e = decision_event("u_" + U_MARA, gate, typ)
        e["outcome"] = "pass"
        r = gates.decision(c.ws, t, e)
        assert r.code == Code.GATE_NOT_ELIGIBLE, gate


def test_one_person_counts_once_even_when_the_alias_joins_after_both_approved(w):
    uid = w.ticket()
    c = core(w)
    t = c.tickets[uid]
    gc = t.gates["requirements"]
    gc.decisions = [
        Decision("a", 1, "requirements", "approve", w.people["mara"], "d_1", gc.gen, "h", "ph", None),
        Decision("b", 2, "requirements", "approve", "u_" + U_MARA, "d_2", gc.gen, "h", "ph", None),
    ]
    assert len(generations.counting(t, "requirements", c.ws)) == 2  # no alias yet: two people
    join(w, [alias(w, "mara", U_MARA)])
    ws = core(w).ws
    assert [d.id for d in generations.counting(t, "requirements", ws)] == ["a"]


def test_repointed_alias_is_refused_and_a_repeat_is_fine(w):
    _, ok = join(w, [alias(w, "mara", U_MARA)])
    assert not hasattr(ok, "code")
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    # person to another user, then another person to the same user
    _, r1 = join(w, [alias(w, "mara", U_TOM)], seq=2)
    assert r1.code == Code.HUB_INVALID and "re-pointed" in r1.detail
    _, r2 = join(w, [alias(w, "tom", U_MARA)], seq=2)
    assert r2.code == Code.HUB_INVALID and "re-pointed" in r2.detail
    _, r3 = join(w, [alias(w, "mara", U_MARA), alias(w, "tom", U_TOM)], seq=2)
    assert not hasattr(r3, "code")


def _direct():
    """A WsCore with one owner, driven through the workspace rules (the snapshot is hub state, not a log line)."""
    from orch.model import workspace
    from orch.model.types import Member, WsCore

    me = "4" * 32
    ws = WsCore(created=True)
    ws.members = {"p_" + me: Member("p_" + me, "mara", "owner", "k")}
    actor = {"kind": "person", "id": "p_" + me, "device": "d_x"}

    def joined(seq, aliases, mode="full"):
        e = {"type": "hub.joined", "actor": actor, "hub_id": HUB, "hk_root_pub": ROOT, "mode": mode,
             "binding_seq": seq, "aliases": aliases, "at": "2026-10-09T09:10:11Z"}  # fmt: skip
        return workspace.hub_joined(ws, e)

    def left(seq):
        return workspace.hub_left(ws, {"type": "hub.left", "actor": actor, "hub_id": HUB, "binding_seq": seq})

    return ws, me, joined, left


def test_leave_and_a_fresh_join_never_carry_a_pinned_snapshot():
    """Run 12 delta: the hub's role projection belongs to one membership."""
    ws, me, joined, left = _direct()
    assert joined(1, [{"person_id": me, "user_id": U_MARA}]) is None
    ws.hub["ledger"][HUB]["snapshot"] = {"o": {"roles": {U_MARA: "admin"}}}
    assert left(1) is None
    assert ws.hub["ledger"][HUB]["snapshot"] is None  # leave clears it
    ws.hub["ledger"][HUB]["snapshot"] = {"o": {"roles": {U_MARA: "admin"}}}  # a stale one that somehow survived
    assert joined(2, []) is None
    assert ws.hub["ledger"][HUB]["snapshot"] is None  # a fresh join does not inherit it


def test_forged_alias_then_leave_then_correct_rejoin_is_refused_and_inert():
    """Run 12 delta: a refused (forged) join leaves a wrong alias. Aliases are never re-pointed (hub contract 9.1,
    wire ``alias_repointed``), so the correct rejoin is refused (a lockout of that hub, recoverable on a new hub), and
    the wrong alias gives the owner at most the LOWER of its role and the forged user's: no snapshot carries over."""
    ws, me, joined, left = _direct()
    victim = "7" * 32
    assert joined(1, [{"person_id": me, "user_id": victim}]) is None
    assert left(1) is None
    assert joined(2, [{"person_id": me, "user_id": U_MARA}]).code == Code.HUB_INVALID  # never re-pointed
    assert joined(2, [{"person_id": me, "user_id": victim}]) is None  # the unchanged alias replays fine
    assert ws.hub["aliases"] == {me: victim}  # one alias, no stacking
    assert ws.hub["ledger"][HUB]["snapshot"] is None
    assert identity.member_role(ws, "p_" + me) == "owner"  # no snapshot yet: the local role stands
    ws.hub["ledger"][HUB]["snapshot"] = {"o": {"roles": {U_MARA: "admin"}}}  # the real user has a role, the victim none
    assert identity.member_role(ws, "p_" + me) is None  # fail closed, never more than the local role

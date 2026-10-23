"""D15b (orch-core#1043, task 7): a gate decision a hub builds from the host's own projection ``P`` is admitted by the
host, and one built from a stale ``P`` is refused (hub contract 10.7 and 8.3 step 9: the host checks the VALUES against
its own log, so ``P`` itself authorizes nothing).

The model-level tests run the production engine (``admit``) on events whose F1 fields come ONLY from ``P`` (made by the
same code as the sender: ``shareops.ticket_fact`` + ``make_f1``). The end-to-end part (the hub actor and its channel
proof through C3b's admission path) waits for orch-core#1038 and is xfail until then."""

from __future__ import annotations

import pytest

from orch.host.shareops import ticket_fact
from orch.hubclient import check_proj
from orch.hubclient.sharefmt import F1_GATES
from orch.hubclient.shares import make_f1
from orch.model.codes import Code
from tests.model.test_gates import w  # noqa: F401 - fixture
from tests.model.test_hub_replay import hub_actor, hub_fields
from tests.model.test_hubledger import card, join, pin, snap
from tests.model.world import World


class _Store:
    """The two reads ``ticket_fact`` makes of a store, over a replayed world (no files)."""

    def __init__(self, world: World) -> None:
        self.w = world

    def log_seq(self, log: str) -> int:
        return len(self.w.ws if log == "workspace" else self.w.tl[log])

    def head_at(self, log: str, seq: int) -> str:
        from tests.hub import logview

        rows = self.w.ws if log == "workspace" else self.w.tl[log]
        return logview.head_of(rows[seq - 1])


def projection(world: World, uid: str) -> dict:
    """``P`` the way the host's sender builds it (``StoreSource.proj``), from the world's replayed state."""
    st = world.state()
    s = _Store(world)
    fact = ticket_fact(s, st, uid, st.tickets[uid])
    f1 = make_f1(fact, F1_GATES)
    assert f1 is not None and fact.chain is not None
    p = {
        "seq": fact.chain[0], "head": fact.chain[1], "ws_seq": s.log_seq("workspace"),
        "ws_head": s.head_at("workspace", s.log_seq("workspace")), "status": fact.status,
        "roster_v": f1["roster_v"], "gates": f1["gates"],
    }  # fmt: skip
    check_proj(p)
    return p


def hub_built(p: dict, gate: str, typ: str = "gate.approved") -> dict:
    """The F1 fields a hub copies from ``P`` for a decision (``build_from_proj``); nothing else is read."""
    g = p["gates"][gate]
    f = {"gate": gate, "gate_gen": g["gen"], "policy_hash": g["policy_hash"], "hash": g["hash"]}
    if gate == "code":
        f["source_sha"] = g["source_sha"]
    return f


def test_a_gate_approval_built_from_p_is_admitted(w):  # noqa: F811
    uid = w.ticket()
    w.fill(uid)
    p = projection(w, uid)
    assert set(p["gates"]) >= {"requirements"} and p["roster_v"] == w.roster_v
    e = hub_built(p, "requirements")
    _, r = w.try_(uid, "gate.approved", "sev", **e)
    assert not hasattr(r, "code"), r  # admitted
    assert w.view(uid).gates["requirements"].approved


def test_p_and_the_hosts_own_decide_agree_on_every_value(w):  # noqa: F811
    uid = w.ticket()
    w.fill(uid)
    p = projection(w, uid)
    v = w.view(uid).gates["requirements"]
    assert p["gates"]["requirements"] == {"gen": v.gen, "hash": v.hash, "policy_hash": v.policy_hash}


def test_a_code_approval_and_a_verdict_built_from_p_carry_the_hosts_source_list(w):  # noqa: F811
    uid = w.ticket()
    w.to_testing(uid)
    p = projection(w, uid)
    assert p["status"] == "testing" and "source_sha" in p["gates"]["code"] and "source_sha" in p["gates"]["verify"]
    own = w.decide(uid, "sev", "code", try_=True)
    mine = w.build_unappended(uid, "gate.approved", "sev", **hub_built(p, "code"))
    assert mine["source_sha"] == [dict(x) for x in w.view(uid).source_list]  # the same list the engine binds
    # the engine's verdict on the P-built event is the verdict on the host's own (accepted, or the same refusal)
    from orch.model import admit

    theirs = admit(w.state(), mine, log=uid)
    assert getattr(theirs, "code", None) == getattr(own[1], "code", None)


@pytest.mark.parametrize("change", ["section", "late_gate"])
def test_a_decision_built_from_a_stale_p_is_refused_gate_stale(w, change):  # noqa: F811
    uid = w.ticket()
    w.fill(uid)
    p = projection(w, uid)
    if change == "section":
        w.edit(uid, "sev", sections={"requirements": "changed text"})  # the gate hash moves, P is now stale
    else:
        w.decide(uid, "sev", "requirements", kind="changes")  # the generation moves
    r = w.try_(uid, "gate.approved", "sev", **hub_built(p, "requirements"))[1]
    assert r.code == Code.GATE_STALE


def test_a_false_p_authorizes_nothing(w):  # noqa: F811
    uid = w.ticket()
    w.fill(uid)
    p = projection(w, uid)
    p["gates"]["requirements"]["hash"] = "sha256:" + "ab" * 32  # a hostile hub makes the value up
    r = w.try_(uid, "gate.approved", "sev", **hub_built(p, "requirements"))[1]
    assert r.code == Code.GATE_STALE


@pytest.mark.xfail(
    reason="orch-core#1038 (C3b admission of a hub actor) is not on develop: the engine stops at consumers_not_ready",
    strict=False,
)
def test_end_to_end_a_hub_built_gate_approved_from_p_is_admitted_through_the_hub_actor_path():
    world = World(validate=False).bootstrap({"mara": "maintainer"})
    from tests.model.test_hub_replay import U2

    join(world, aliases=[{"person_id": world.people["mara"][2:], "user_id": U2}])
    pin(world, card())
    pin(world, snap(1))
    uid = world.ticket()
    world.fill(uid)
    e = hub_built(projection(world, uid), "requirements")
    _, r = world.try_(uid, "gate.approved", hub_actor(), roster_v=world.roster_v, **hub_fields(), **e)
    assert not hasattr(r, "code"), r

# ruff: noqa: F811
"""Selective custodial build (protocol.md §8.6): the engine builds the four decisions from ONE shared item, with the
contract's ``build_selective``, for the aliased owner only. The ``selective_build_*`` and ``custodial_selective_*``
vectors replay against the real engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from custodial_helpers import Env
from shares_helpers import shub  # noqa: F401

from orch_hub.contract import cases as C
from orch_hub.contract import phase4 as P4
from orch_hub.contract import vectorgen as G
from orch_hub.contract import vectorgen_p4 as V4
from orch_hub.contract.hub import Refused

VEC = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "hub" / "vectors_hub.json").read_text())
CASES = {c["name"]: c for c in VEC["cases"]}
ENGINE_FIELDS = {
    "v", "id", "actor", "auth", "channel", "channel_factor", "hub_id", "snapshot_version", "channel_cert", "uk_cert",
}  # fmt: skip
ITEM = V4.ITEM
CODE = {"type": "gate.approved", "log": V4.TICKET, "item": ITEM, "gate": "code", "hash": V4.HASH_A}


def code(fn, *a, **kw):
    with pytest.raises(Refused) as r:
        fn(*a, **kw)
    return r.value.code


class Setup:
    """A selective workspace on a real link + ShareService holding the vector f1 item, and an engine on top of it."""

    def __init__(self, shub, tmp_path, *, owner_is_user=True, put=True):
        self.h = shub()
        self.env = Env(tmp_path, role=None, mode="selective", ws=G.WS_A, shares=self.h.svc)
        owner = self.env.user if owner_is_user else G.fake16("someone_else")
        self.h.invites.owner_user = lambda inv: owner
        self.c = self.h.join()
        self.item = V4.f1_item()
        if put:
            self.c.request("share_put", V4.share_body(self.item))

    def reput(self, **kw):
        self.item = V4.f1_item(item_version=2, **kw)
        self.c.request("share_put", V4.share_body(self.item))


@pytest.fixture
def st(shub, tmp_path):
    s = Setup(shub, tmp_path)
    yield s
    s.env.close()


def test_selective_happy_path_builds_signs_audits_delivers(st):
    env = st.env
    p = env.prepare(dict(CODE))
    ev = p.ctx["event"]
    f1 = st.item["o"]["f1"]
    assert ev["item_hash"] == P4.item_hash(st.item)
    assert ev["based_on"] == st.item["o"]["chain"]["head"] and ev["roster_v"] == f1["roster_v"]
    g = f1["gates"]["code"]
    assert (ev["hash"], ev["gate_gen"], ev["policy_hash"], ev["source_sha"]) == (
        g["hash"],
        g["gen"],
        g["policy_hash"],
        g["source_sha"],
    )
    assert "item" not in ev and ev["gate"] == "code" and p.ctx["log"] == V4.TICKET
    signed = env.engine.sign(p.prepared_id, env.channel, env.proof(p.digest))
    assert env.delivered == [(G.WS_A, V4.TICKET, signed)]
    assert signed["item_hash"] == ev["item_hash"]
    assert len(env.audit.store.after(0, 10)) == 1


@pytest.mark.parametrize(
    "action",
    [
        {"type": "gate.approved", "log": V4.TICKET, "item": ITEM, "gate": "plan", "hash": None},
        {"type": "gate.changes_requested", "log": V4.TICKET, "item": ITEM, "gate": "code", "hash": V4.HASH_A, "text": "no"},
        {"type": "verdict.given", "log": V4.TICKET, "item": ITEM, "outcome": "pass", "hash": None},
        {"type": "question.answered", "log": V4.TICKET, "item": ITEM, "question": "Q1", "hash": V4.HASH_Q, "text": "API"},
    ],
)  # fmt: skip
def test_all_four_decisions_build_and_sign(st, action):
    f1 = st.item["o"]["f1"]
    if action["hash"] is None:
        gate = "verify" if action["type"] == "verdict.given" else action["gate"]
        action = {**action, "hash": f1["gates"][gate]["hash"]}
    p = st.env.prepare(action)
    st.env.engine.sign(p.prepared_id, st.env.channel, st.env.proof(p.digest))
    assert len(st.env.delivered) == 1


def test_only_the_aliased_owner_prepares(shub, tmp_path):
    s = Setup(shub, tmp_path, owner_is_user=False)
    try:
        assert code(s.env.prepare, dict(CODE)) == "not_granted"
    finally:
        s.env.close()


def test_selective_needs_no_hub_role(st):
    assert st.env.access.roles(G.WS_A) == {}  # no grant at all: hub roles grant nothing in a selective workspace
    st.env.prepare(dict(CODE))


def test_answer_with_option_is_malformed(st):
    a = {
        "type": "question.answered",
        "log": V4.TICKET,
        "item": ITEM,
        "question": "Q1",
        "hash": V4.HASH_Q,
        "option": "a",
    }
    assert code(st.env.prepare, a) == "malformed"


@pytest.mark.parametrize(
    ("patch", "want"),
    [
        ({"hash": "sha256:" + "00" * 32}, "event_changed"),
        ({"gate": "requirements"}, "unknown_item"),
        ({"log": "01J9ZP0000000000000000TKT2"}, "unknown_item"),
        ({"item": "ab" * 16}, "unknown_item"),
        ({"item": None}, "malformed"),
        ({"gate_gen": 9}, "malformed"),  # the person supplies no derived value
    ],
)
def test_refusals(st, patch, want):
    a = {**CODE, **patch}
    if a["item"] is None:
        del a["item"]
    assert code(st.env.prepare, a) == want


def test_non_decision_types_are_not_custodial(st):
    assert code(st.env.prepare, {"type": "log.added", "log": V4.TICKET, "item": ITEM, "text": "x"}) == "not_custodial"


def test_item_in_a_full_mode_action_is_malformed(tmp_path):
    env = Env(tmp_path)
    try:
        assert (
            code(env.prepare, {"type": "log.added", "log": "01J9ZP0000000000000000TKT1", "text": "x", "item": ITEM})
            == "malformed"
        )
    finally:
        env.close()


def test_selective_without_an_item_source_builds_nothing(tmp_path):
    env = Env(tmp_path, role=None, mode="selective", ws=G.WS_A)
    try:
        assert code(env.prepare, dict(CODE)) == "wrong_mode"
    finally:
        env.close()


def test_item_without_f1_is_unavailable(shub, tmp_path):
    s = Setup(shub, tmp_path, put=False)
    try:
        s.c.request("share_put", V4.share_body(V4.share_item(item_kind="ticket", content=V4.TICKET_CONTENT)))
        assert code(s.env.prepare, dict(CODE)) == "unavailable"
    finally:
        s.env.close()


def test_a_newer_item_version_after_prepare_is_event_changed(st):
    env = st.env
    p = env.prepare(dict(CODE))
    st.reput()  # the host put the item again: another hash
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "event_changed"
    assert env.delivered == [] and env.audit.store.after(0, 10) == []


def test_a_revoked_item_after_prepare_is_unknown_item(st):
    env = st.env
    p = env.prepare(dict(CODE))
    st.c.request("share_revoke", {"item_id": ITEM})
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "unknown_item"
    assert env.delivered == []


def test_owner_alias_is_re_read_at_sign(st, monkeypatch):
    env = st.env
    p = env.prepare(dict(CODE))
    monkeypatch.setattr(st.h.registry, "owner_alias", lambda ws: G.fake16("new_owner"))
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "not_granted"
    assert env.delivered == []


# --- the vectors against the real engine ---------------------------------------------------------------------------


class VectorItems:
    """The item source of a ``selective_build_*`` vector: the vector's signed item, whatever the id."""

    def __init__(self, item):
        self.item = item

    def shared_item(self, user_id, workspace_id, item_id):
        return {"signed": self.item, "item": self.item["o"], "hash": P4.item_hash(self.item)}


BUILD = sorted(n for n in CASES if n.startswith("selective_build_"))


@pytest.mark.parametrize("name", BUILD)
def test_selective_build_vectors_replay_against_the_engine(name, tmp_path):
    case = CASES[name]
    a = case["args"]
    env = Env(tmp_path, role=None, mode="selective", ws=G.WS_A, shares=VectorItems(a["item"]))
    try:
        exp = case["expect"]
        if "error" in exp:
            assert code(env.prepare, dict(a["action"])) == exp["error"]
            return
        p = env.prepare(dict(a["action"]))
        got = {k: v for k, v in p.ctx["event"].items() if k not in ENGINE_FIELDS}
        assert p.ctx["log"] == exp["ok"]["log"] and got == exp["ok"]["event"]
    finally:
        env.close()


ADMIT = sorted(n for n in CASES if n.startswith("custodial_selective_"))


@pytest.mark.parametrize("name", ADMIT)
def test_custodial_selective_vectors_replay_through_the_engines_verifier(name, tmp_path):
    case = CASES[name]
    a = case["args"]
    env = Env(tmp_path, role=None, mode="selective", ws=G.WS_A)
    try:
        held = C._held(a)
        extra = {
            "mode": a.get("mode", "full"),
            "granted_users": frozenset(a.get("granted_users", [])),
            "member_roles": a.get("member_roles", {}),
            "live_items": a.get("live_items", {}),
            "superseded_items": frozenset(a.get("superseded_items", [])),
            "log_heads": a.get("log_heads", {}),
            "min_factor": a.get("min_factor"),
        }
        exp = case["expect"]
        run = lambda: env.engine._verify_signed(
            a["event"], a["workspace_id"], a["log"], held, a["snapshot"], a["now_ms"], **extra
        )
        if "error" in exp:
            assert code(run) == exp["error"]
        else:
            assert run() == exp["ok"]
    finally:
        env.close()


# --- the engine hands the signed event to the delivery queue -------------------------------------------------------


def test_sign_queues_the_signed_event_durably(st):
    from orch_hub.contract.canon import cj
    from orch_hub.custodial.deliver import DeliveryQueue

    q = DeliveryQueue()
    st.env.engine._deliver = q
    p = st.env.prepare(dict(CODE))
    signed = st.env.engine.sign(p.prepared_id, st.env.channel, st.env.proof(p.digest))
    row = q._db.execute("SELECT event, ws, log FROM deliver").fetchone()
    assert bytes(row[0]) == cj(signed) and row[1:] == (G.WS_A, V4.TICKET)
    q.close()


def test_a_queue_that_cannot_store_refuses_and_sends_nothing(st):
    def broken(ws, log, ev):
        raise OSError("disk full")

    st.env.engine._deliver = broken
    p = st.env.prepare(dict(CODE))
    assert code(st.env.engine.sign, p.prepared_id, st.env.channel, st.env.proof(p.digest)) == "unavailable"

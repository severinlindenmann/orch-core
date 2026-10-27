"""D6's CustodialEngine over the REAL view (store + sealed pages, ``StoreWorkspaceView``): prepare -> proof -> sign
builds, signs and delivers events from what ``log_sync`` delivered; the view moving between prepare and sign is
``event_changed``."""

from __future__ import annotations

import pytest
from custodial_helpers import LOG, NEW_LOG, WK, WS, Env
from view_helpers import OWNER, WSK_PUB, Host, services

from orch_hub.contract.hub import Refused
from orch_hub.custodial import CustodialEngine
from orch_hub.view import project as PR
from orch_hub.view.build import new_ticket_uid


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, role="admin")
    e.vs = services(tmp_path, e.seals)
    e.host = Host()
    e.host.genesis()
    e.host.append("workspace", "member.added", roster_v=1)
    e.host.ticket()
    e.host.push_all(e.vs)
    e.engine = CustodialEngine(
        keys=e.keys,
        snapshots=e.snaps,
        seals=e.seals,
        view=e.vs.workspace_view,
        channels=e.channels,
        sessions=e.sessions,
        audit=e.audit,
        deliver=lambda ws, log, ev: e.delivered.append((ws, log, ev)),
        mode_of=lambda ws: e.mode,
        now_ms=lambda: e.clock[0],
        dev_mode=True,
    )
    yield e
    e.vs.close()
    e.close()


def code(fn, *a, **kw):
    with pytest.raises(Refused) as r:
        fn(*a, **kw)
    return r.value.code


def sign(env, action):
    p = env.prepare(action)
    return p, env.engine.sign(p.prepared_id, env.channel, env.proof(p.digest))


def test_log_added_is_built_from_the_real_view_and_signed(env):
    p, ev = sign(env, {"type": "log.added", "log": LOG, "text": "hello"})
    e = p.ctx["event"]
    assert e["based_on"] == env.vs.service.head(WS, LOG)
    assert e["roster_v"] == 2 and e["hash_v"] == 1 and e["text"] == "hello"
    assert env.delivered and env.delivered[-1][1] == LOG and ev["sig"]


def test_workspace_log_event_uses_the_workspace_head(env):
    p = env.prepare({"type": "addon.disabled", "log": "workspace", "name": "wiki"})
    e = p.ctx["event"]
    assert p.ctx["log"] == "workspace" and e["name"] == "wiki"
    assert e["based_on"] == env.vs.service.head(WS, "workspace") and e["roster_v"] == 2


def test_ticket_created_gets_next_key_and_a_stable_uid(env):
    a = {"type": "ticket.created", "ticket_type": "bug", "title": "Crash on start"}
    p = env.prepare(a)
    e = p.ctx["event"]
    assert p.ctx["log"] == new_ticket_uid(e["id"]) and e["key"] == "DEMO-0002"
    assert e["based_on"] is None and e["owner"] == "u_" + env.user and e["ticket_type"] == "bug"
    ev = env.engine.sign(p.prepared_id, env.channel, env.proof(p.digest))  # sign rebuilds the same uid and key
    assert ev["key"] == "DEMO-0002"


def test_ticket_updated_base_rev_is_the_value_hash_of_the_current_value(env):
    p = env.prepare({"type": "ticket.updated", "log": LOG, "set": {"ticket.priority": "high", "ticket.size": "m"}})
    e = p.ctx["event"]
    assert e["base_rev"] == {"ticket.priority": PR.value_hash("medium"), "ticket.size": PR.value_hash(None)}
    assert e["set"] == {"ticket.priority": "high", "ticket.size": "m"}
    # after the host logged a priority change, base_rev follows the view
    env.host.append(LOG, "ticket.updated", base_rev={}, set={"ticket.priority": "urgent"}, roster_v=2)
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 2))
    p2 = env.prepare({"type": "ticket.updated", "log": LOG, "set": {"ticket.priority": "low"}})
    assert p2.ctx["event"]["base_rev"] == {"ticket.priority": PR.value_hash("urgent")}


def test_question_asked_then_answered_with_the_hub_derived_hash(env):
    p = env.prepare(
        {
            "type": "question.asked",
            "log": LOG,
            "question": {"to": OWNER, "text": "CSV?", "blocking": True, "kind": "text"},
        }
    )
    q = p.ctx["event"]
    assert q["question"]["id"] == "Q1" and q["qid"] == PR.question_id(WS, LOG, "Q1")
    assert q["hash"] == PR.question_hash(q["qid"], LOG, q["question"])
    h = env.host.ask(text="Which?", options=[{"key": "csv", "label": "CSV"}, {"key": "api", "label": "API"}])
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 2))
    p2, ev = sign(env, {"type": "question.answered", "log": LOG, "question": "Q1", "hash": h, "option": "csv"})
    assert p2.ctx["event"]["hash"] == h and ev["option"] == "csv"
    # a second asked question is Q2
    p3 = env.prepare(
        {
            "type": "question.asked",
            "log": LOG,
            "question": {"to": OWNER, "text": "More?", "blocking": False, "kind": "text"},
        }
    )
    assert p3.ctx["event"]["question"]["id"] == "Q2"


def test_answer_to_a_changed_question_is_event_changed(env):
    env.host.ask(text="Old?")
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 2))
    old = PR.question_hash(PR.question_id(WS, LOG, "Q1"), LOG, {"text": "Older text", "kind": "text"})
    a = {"type": "question.answered", "log": LOG, "question": "Q1", "hash": old, "text": "x"}
    assert code(env.prepare, a) == "event_changed"
    a2 = {**a, "question": "Q9"}
    assert code(env.prepare, a2) == "unknown_item"


def test_answer_with_an_option_the_question_lacks_is_malformed(env):
    h = env.host.ask(options=[{"key": "csv", "label": "CSV"}, {"key": "pdf", "label": "PDF"}])
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 2))
    a = {"type": "question.answered", "log": LOG, "question": "Q1", "hash": h, "option": "api"}
    assert code(env.prepare, a) == "malformed"


def test_view_moving_between_prepare_and_sign_is_event_changed(env):
    p = env.prepare({"type": "log.added", "log": LOG, "text": "hi"})
    env.host.append(LOG, "log.added", text="agent note", roster_v=2)
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 2))
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "event_changed"


def test_roster_change_between_prepare_and_sign_is_event_changed(env):
    p = env.prepare({"type": "log.added", "log": LOG, "text": "hi"})
    env.host.append("workspace", "member.added", roster_v=2)
    env.vs.service.accept(WS, WSK_PUB, env.host.page("workspace", 3))
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "event_changed"


SRC = [{"repo": "github.com/acme/app", "ref": "refs/heads/feat-x", "sha": "c0" * 20}]
H_REQ, H_CODE, H_VER = ("sha256:" + x * 32 for x in ("a1", "a3", "a4"))


def push_proj(env, status="in_progress", from_seq=None):
    """The host's overlap page for the ticket's last line, carrying P (D15b, sender rule 7)."""
    n = len(env.host.logs[LOG])
    gates = {
        "requirements": env.host.gate(1, H_REQ),
        "code": env.host.gate(2, H_CODE, SRC),
        "verify": env.host.gate(4, H_VER, SRC),
    }
    env.vs.service.accept(
        WS, WSK_PUB, env.host.page(LOG, from_seq or n, 1, proj=env.host.proj(status=status, gates=gates))
    )


@pytest.mark.parametrize(
    "action, extra",
    [
        ({"type": "gate.approved", "gate": "requirements", "hash": H_REQ}, {"gate_gen": 1}),
        ({"type": "gate.approved", "gate": "code", "hash": H_CODE}, {"gate_gen": 2, "source_sha": SRC}),
        ({"type": "gate.changes_requested", "gate": "code", "hash": H_CODE, "text": "no"}, {"gate_gen": 2}),
        ({"type": "verdict.given", "outcome": "pass", "hash": H_VER}, {"gate_gen": 4, "source_sha": SRC}),
        ({"type": "status.changed", "to": "open"}, {"from": "testing"}),
        ({"type": "ticket.moved", "to": "backlog", "reason": "later"}, {"from": "testing"}),
    ],
)
def test_five_types_are_built_from_P_and_signed(env, action, extra):
    push_proj(env, status="testing")
    p, ev = sign(env, {**action, "log": LOG})
    e = p.ctx["event"]
    assert {k: e[k] for k in extra} == extra and e["type"] == action["type"]
    if "hash" in action:
        assert e["hash"] == action["hash"] and e["policy_hash"] == "sha256:" + "cd" * 32
    assert env.delivered[-1][2] is ev or env.delivered[-1][2] == ev


def test_no_projection_yet_is_unavailable(env):
    a = {"type": "gate.approved", "log": LOG, "gate": "requirements", "hash": H_REQ}
    assert code(env.prepare, a) == "unavailable"
    assert code(env.prepare, {"type": "status.changed", "log": LOG, "to": "open"}) == "unavailable"


def test_stale_P_is_unavailable(env):
    push_proj(env)
    env.host.append(LOG, "log.added", text="moved on", roster_v=2)
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 2))  # the P held describes seq 1, the view is at seq 2
    assert code(env.prepare, {"type": "status.changed", "log": LOG, "to": "open"}) == "unavailable"
    push_proj(env)  # the host's refresh
    assert env.prepare({"type": "status.changed", "log": LOG, "to": "open"}).ctx["event"]["from"] == "in_progress"


def test_roster_moved_makes_P_unavailable(env):
    push_proj(env)
    env.host.append("workspace", "member.added", roster_v=2)
    env.vs.service.accept(WS, WSK_PUB, env.host.page("workspace", 3))
    assert code(env.prepare, {"type": "status.changed", "log": LOG, "to": "open"}) == "unavailable"


def test_gate_left_out_is_unknown_item_and_other_hash_is_event_changed(env):
    push_proj(env)
    assert code(env.prepare, {"type": "gate.approved", "log": LOG, "gate": "plan", "hash": H_REQ}) == "unknown_item"
    a = {"type": "gate.approved", "log": LOG, "gate": "requirements", "hash": "sha256:" + "ee" * 32}
    assert code(env.prepare, a) == "event_changed"


def test_P_changing_between_prepare_and_sign_is_event_changed(env):
    push_proj(env, status="testing")
    p = env.prepare({"type": "status.changed", "log": LOG, "to": "open"})
    push_proj(env, status="done")
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "event_changed"


def test_P_is_only_used_for_a_visible_ticket(env):
    push_proj(env)
    env.host.append(LOG, "visibility.changed", visibility={"restricted": ["p_" + "22" * 16]}, roster_v=2)
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 2))
    push_proj(env)
    a = {"type": "gate.approved", "log": LOG, "gate": "requirements", "hash": H_REQ}
    assert code(env.prepare, a) == code(env.prepare, {**a, "log": NEW_LOG}) == "unknown_item"


def test_unknown_ticket_and_unsynced_view(env, tmp_path):
    assert code(env.prepare, {"type": "log.added", "log": NEW_LOG, "text": "x"}) == "unknown_item"
    env.vs.store.drop(WS)
    assert code(env.prepare, {"type": "log.added", "log": LOG, "text": "x"}) == "unavailable"


def test_selective_workspace_builds_nothing(env):
    env.mode = "selective"
    assert code(env.prepare, {"type": "log.added", "log": LOG, "text": "x"}) == "wrong_mode"


def test_a_stored_page_that_no_longer_matches_its_heads_is_unavailable(env):
    # the db is tampered with: a head is replaced, the sealed pages still open but no longer match
    with env.vs.store.lock, env.vs.store._db:
        env.vs.store._db.execute(
            "UPDATE view_heads SET head=? WHERE ws=? AND log=? AND seq=1", ("sha256:" + "00" * 32, WS, LOG)
        )
    assert code(env.prepare, {"type": "log.added", "log": LOG, "text": "x"}) == "unavailable"


def test_keyring_zeroises_the_keys_it_opened(env):
    from orch_hub.view.service import Keyring

    k = Keyring(env.seals, WS)
    key = k.get(1)
    assert bytes(key) == WK
    k.close()
    assert bytes(key) == bytes(32)


def test_restricted_ticket_cannot_be_built_on_by_a_user_not_on_the_list(env):
    env.host.append(LOG, "visibility.changed", visibility={"restricted": ["p_" + "22" * 16]}, roster_v=2)
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 2))
    # the ticket owner is OWNER (a person the helper user is not aliased to): same answer as a missing ticket
    a = {"type": "log.added", "log": LOG, "text": "probe"}
    assert code(env.prepare, a) == code(env.prepare, {**a, "log": NEW_LOG}) == "unknown_item"
    env.host.append(LOG, "visibility.changed", visibility={"restricted": ["u_" + env.user]}, roster_v=2)
    env.vs.service.accept(WS, WSK_PUB, env.host.page(LOG, 3))
    assert env.prepare(a).ctx["event"]["text"] == "probe"


def test_addon_requested_is_built_and_signed_by_the_real_engine_and_core_accepts_its_shape(env):
    """FB15 (8.8b): the hub engine builds exactly the six action fields on the workspace log (core's host-side shape check
    accepts such an event: the shared vector custodial_event_addon_requested)."""
    a = {
        "type": "addon.requested", "log": "workspace", "name": "wiki", "version": "1.2.0",
        "package_sha256": "sha256:" + "a" * 64, "capabilities": ["network"], "handoff": "hr_01J9ZP0000000000000000HND1",
        "runtime": {"kind": "node-package-lock", "lock": "package-lock.json", "packages": 4},
    }  # fmt: skip
    p, ev = sign(env, a)
    e = p.ctx["event"]
    assert p.ctx["log"] == "workspace" and e["based_on"] == env.vs.service.head(WS, "workspace")
    for k in ("name", "version", "package_sha256", "capabilities", "handoff", "runtime"):
        assert e[k] == a[k]
    assert env.delivered[-1][1] == "workspace" and ev["sig"]
    # a reserved name never reaches the view (the hub refuses it as malformed)
    assert code(env.prepare, {**a, "name": "ticket"}) == "malformed"
    assert code(env.prepare, {**a, "fields": {}}) == "malformed"


def test_settings_changed_is_built_and_signed_by_the_real_engine(env):
    """27 Oct (8.8): a settings change from the hub page is built on the workspace log with exactly the `set` it was sent."""
    a = {"type": "settings.changed", "log": "workspace", "set": {"grant_hours": 24, "claim_ttl_min": 60}}
    p, ev = sign(env, a)
    e = p.ctx["event"]
    assert (
        p.ctx["log"] == "workspace" and e["set"] == a["set"] and e["based_on"] == env.vs.service.head(WS, "workspace")
    )
    assert env.delivered[-1][1] == "workspace" and ev["sig"]
    assert code(env.prepare, {**a, "set": {"sign_methods": ["click"]}}) == "malformed"
    assert code(env.prepare, {**a, "set": {"grant_hours": 999}}) == "malformed"

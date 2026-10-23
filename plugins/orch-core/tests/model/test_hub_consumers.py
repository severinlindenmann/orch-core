"""C3b task 4 (ticket-format §15.4): every consumer of a person actor goes through ``identity.signer`` / ``role_of``.

Real admitted hub lines (``World.try_`` -> ``admit``) with the fake verifier for the crypto (the real signatures are in
tests/hub/test_c3b_vectors). Covers: gates and counting, the effective role (min of local and hub), the roster,
questions, lifecycle, channel revocations voiding open decisions, ``sign_methods`` ``hub``, the D66 factor floor, and
the D59 end to end (a worker's hub id, a leave, a second hub, the aliased person's approval refused)."""

from __future__ import annotations

import pytest

from orch.model import Code, authz, generations, policies
from orch.model.codes import Refusal
from orch.model.state import admit
from tests.model.test_hub_replay import CHAN, hub_actor, hub_fields
from tests.model.test_hubledger import HUB, HUB_B, ROOT, ROOT_B, U1, U2, card, snap
from tests.model.world import World, pol

U3 = "5" * 32
CHAN2 = "b" * 32


def snapx(version: int, roles: dict[str, str] | None = None, channels: tuple[str, ...] = (), hub: str = HUB) -> dict:
    s = snap(version, channels, hub)
    s["o"]["roles"] = {U1: "admin", U2: "admin"} if roles is None else roles
    return s


def hub_joined(w: World, aliases=(), mode: str = "full", hub: str = HUB, root: str = ROOT, seq: int = 1):
    return w.wev("hub.joined", "sev", hub_id=hub, hk_root_pub=root, mode=mode, binding_seq=seq,
                 aliases=sorted(aliases, key=lambda a: a["person_id"]))  # fmt: skip


def pin(w: World, obj: dict, hub: str = HUB):
    return w.try_("workspace", "hub.pinned", {"kind": "host"}, hub_id=hub, object=obj)


def alias(w: World, name: str, uid: str) -> dict:
    return {"person_id": w.people[name][2:], "user_id": uid}


def hub(w: World, uid: str, typ: str, user: str = U2, chan: str = CHAN, **payload):
    """Try a hub-signed event on ticket ``uid``; returns the refusal or ``None``."""
    _, r = w.try_(uid, typ, hub_actor(user, chan), roster_v=w.roster_v, **hub_fields(channel=chan), **payload)
    return r if hasattr(r, "code") else None


def decide(w: World, uid: str, gate: str = "requirements", user: str = U2, chan: str = CHAN, **over):
    _, r = w.decide(uid, hub_actor(user, chan), gate, try_=True, **hub_fields(channel=chan), **over)
    return r if hasattr(r, "code") else None


def decisions(w: World, uid: str, gate: str = "requirements"):
    return w.state()._core.tickets[uid].gates[gate].decisions


@pytest.fixture
def w() -> World:
    """sev owner, mara maintainer (aliased to U2), tom member; full mode; U1 an unaliased hub admin; U2 hub admin."""
    w = World(validate=False).bootstrap({"mara": "maintainer", "tom": "member"})
    hub_joined(w, [alias(w, "mara", U2)])
    pin(w, card())
    pin(w, snapx(1))
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer", "member"], 1)})
    return w


@pytest.fixture
def uid(w: World) -> str:
    u = w.ticket()
    w.fill(u)
    return u


# --- gates and counting ------------------------------------------------------------------------------------------


def test_a_hub_decision_is_recorded_under_the_canonical_person_and_the_channel_key(w, uid):
    assert decide(w, uid) is None
    (d,) = decisions(w, uid)
    assert d.person == w.people["mara"]  # u_ aliased to p_mara: one person (D59)
    assert d.signer == f"hub:{HUB}/{CHAN}"  # never a d_ id
    assert w.state().tickets[uid].gates["requirements"].decisions[0].counting


def test_one_person_counts_once_across_the_device_and_the_hub(w, uid):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 2)})
    w.decide(uid, "mara", "requirements")
    assert decide(w, uid) is None  # the same person through the hub: recorded, but one counting approval
    g = w.state().tickets[uid].gates["requirements"]
    assert len(g.decisions) == 2 and sum(d.counting for d in g.decisions) == 1
    assert not g.approved


def test_an_unaliased_hub_admin_decides_with_the_owner_token_as_its_own_person(w, uid):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner"], 1)})
    assert decide(w, uid, user=U1) is None
    (d,) = decisions(w, uid)
    assert d.person == "u_" + U1 and d.signer.startswith("hub:")


def test_the_effective_role_is_the_lower_of_the_local_and_the_hub_role(w, uid):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1)})
    pin(w, snapx(2, {U1: "admin", U2: "edit"}))  # hub demotes mara's user to member
    r = decide(w, uid)
    assert r.code == Code.GATE_NOT_ELIGIBLE  # maintainer locally, member through the hub: no maintainer token
    pin(w, snapx(3, {U1: "admin", U2: "read"}))  # viewer through the hub
    assert decide(w, uid).code == Code.ROLE_DENIED


def test_the_hub_cannot_lift_a_local_role(w, uid):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner"], 1)})
    r = decide(w, uid)  # U2 is a hub admin (owner) but mara is a maintainer locally
    assert r.code == Code.GATE_NOT_ELIGIBLE


def test_an_aliased_user_removed_from_the_roster_has_no_role_at_all(w, uid):
    w.wev("member.removed", "sev", person=w.people["mara"])
    r = decide(w, uid)
    assert (r.code, r.detail) == (Code.HUB_DENIED, "forbidden_role")  # never the unaliased hub role


def test_a_user_the_snapshot_gives_no_role_is_denied(w, uid):
    r = decide(w, uid, user=U3)
    assert r.code in (Code.HUB_DENIED, Code.HUB_FOREIGN_USER)


def test_a_hub_event_of_a_type_outside_the_custodial_list_is_refused_by_the_model(w):
    e = w.build_unappended(
        "workspace", "settings.changed", hub_actor(U2), roster_v=w.roster_v, set={"claim_ttl_min": 30}, **hub_fields()
    )
    r = admit(w.state(), e, log="workspace")
    assert (r.code, r.detail) == (Code.HUB_DENIED, "not_custodial")


# --- D59 end to end ----------------------------------------------------------------------------------------------


def test_d59_a_worker_who_left_and_rejoined_through_a_second_hub_cannot_approve_the_work(w):
    """The ticket's worker is hub user ``u_x`` (aliased to ``p_mara``). The hub is left, a second hub is joined, and
    ``p_mara`` approves with her device: still one person, so the independence rule refuses her."""
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer", "member"], 1, [], "all", True)})
    u = w.ticket()
    w.fill(u)
    # a real admitted hub line makes u_x a worker: the hub admin (mara through U2) assigns u_x to the ticket
    assert hub(w, u, "people.changed", role="assignees", add=["u_" + U2], remove=[]) is None
    t = w.state()._core.tickets[u]
    assert "u_" + U2 in t.workers
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    hub_joined(w, [], hub=HUB_B, root=ROOT_B, seq=2)  # a second hub: u_x is not aliased there, but the book is kept
    _, r = w.decide(u, "mara", "requirements", try_=True)
    assert r.code == Code.GATE_NOT_ELIGIBLE and "worker" in r.detail
    # control: another eligible person is not caught
    _, ok = w.decide(u, "tom", "requirements", try_=True)
    assert not hasattr(ok, "code"), ok


def test_d59_the_aliased_worker_cannot_approve_through_the_hub_either(w):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer"], 1, [], "all", True)})
    u = w.ticket()
    w.fill(u)
    w.tev(u, "people.changed", "sev", role="assignees", add=[w.people["mara"]], remove=[])
    r = decide(w, u)
    assert r.code == Code.GATE_NOT_ELIGIBLE and "worker" in r.detail


# --- channel revocation voids open decisions (C3b-2) -------------------------------------------------------------


def test_a_channel_revocation_voids_the_open_decisions_of_that_channel_only(w, uid):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer", "member"], 3)})
    assert decide(w, uid, chan=CHAN) is None
    assert decide(w, uid, user=U1, chan=CHAN2) is None
    w.decide(uid, "tom", "requirements")
    pin(w, snapx(2, channels=(CHAN,)))
    by = {d.signer: d for d in decisions(w, uid)}
    assert by[f"hub:{HUB}/{CHAN}"].voided
    assert not by[f"hub:{HUB}/{CHAN2}"].voided and not by[w.dev["tom"]].voided
    assert len(w.state().tickets[uid].gates["requirements"].decisions) == 3
    assert not w.state().tickets[uid].gates["requirements"].approved


def test_a_decision_in_a_consumed_generation_is_not_voided(w, uid):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer", "member"], 2)})
    assert decide(w, uid) is None
    w.edit(uid, "sev", sections={"context": "changed"})  # raises the generation: the decision is no longer open
    pin(w, snapx(2, channels=(CHAN,)))
    (d,) = decisions(w, uid)
    assert not d.voided


def test_op_revocations_rotations_and_role_changes_void_nothing(w, uid):
    w.wev("policy.changed", "sev", gates={"requirements": pol(["owner", "maintainer", "member"], 2)})
    assert decide(w, uid) is None
    pin(w, {"o": {"kind": "hub.op_revoked", "hub_id": HUB, "cert_id": "7" * 32}, "sig": card()["sig"]})
    pin(w, snapx(2, {U1: "admin", U2: "read"}))  # lowers the role
    pin(w, snapx(3, {U1: "admin"}))  # drops the user
    (d,) = decisions(w, uid)
    assert not d.voided


def test_a_settled_ticket_keeps_its_state_and_lists_the_revoked_decision(w):
    u = w.ticket()
    w.fill(u)
    assert decide(w, u) is None
    core = w.state()._core
    t = core.tickets[u]
    t.status = "done"
    key = f"hub:{HUB}/{CHAN}"
    generations.void_signer(core.ws, t, key)
    (d,) = t.gates["requirements"].decisions
    assert not d.voided and d.id in t.gates["requirements"].revoked_flag and key in t.exempt_devices
    generations.on_unsettle(core.ws, t, "done")  # a push after done: the void applies now
    t.status = "in_progress"
    generations.on_unsettle(core.ws, t, "done")
    assert d.voided


def test_void_signer_is_void_device_for_a_device_key(w, uid):
    core = w.state()._core
    assert generations.void_device is generations.void_signer
    t = core.tickets[uid]
    t.gates["requirements"].decisions.clear()
    generations.void_signer(core.ws, t, w.dev["tom"])  # nothing to void, no error


# --- questions, lifecycle, visibility ----------------------------------------------------------------------------


def test_a_question_to_a_person_is_answered_through_the_alias(w):
    u = w.ticket()
    q = {
        "id": "Q1",
        "text": "which?",
        "to": w.people["mara"],
        "blocking": False,
        "options": [{"key": "a", "label": "A"}],
    }
    from orch.canon import question_hash, question_id

    qid = question_id(w.workspace_id, w.state()._core.tickets[u].uid, "Q1")
    h = question_hash(qid, w.state()._core.tickets[u].uid, "which?", q["options"])
    w.tev(u, "question.asked", "sev", question=q, qid=qid, hash=h)
    assert hub(w, u, "question.answered", question="Q1", hash=h, option="a") is None
    assert w.state()._core.tickets[u].questions["Q1"].answer["by"] == w.people["mara"]


def test_an_unaliased_member_level_hub_user_cannot_answer_a_question_to_someone_else(w):
    pin(w, snapx(2, {U1: "edit", U2: "admin"}))
    u = w.ticket()
    from orch.canon import question_hash, question_id

    q = {
        "id": "Q1",
        "text": "which?",
        "to": w.people["tom"],
        "blocking": False,
        "options": [{"key": "a", "label": "A"}],
    }
    uidt = w.state()._core.tickets[u].uid
    qid = question_id(w.workspace_id, uidt, "Q1")
    h = question_hash(qid, uidt, "which?", q["options"])
    w.tev(u, "question.asked", "sev", question=q, qid=qid, hash=h)
    r = hub(w, u, "question.answered", user=U1, question="Q1", hash=h, option="a")
    assert r.code == Code.ANSWER_NOT_ALLOWED


def test_lifecycle_and_visibility_use_the_signer_role(w):
    u = w.ticket()
    assert hub(w, u, "ticket.closed") is None  # a maintainer (through U2) closes
    assert hub(w, u, "ticket.reopened") is None
    pin(w, snapx(2, {U1: "admin", U2: "edit"}))  # mara is a member through the hub now: not the ticket owner
    assert hub(w, u, "ticket.closed").code == Code.ROLE_DENIED
    assert hub(w, u, "visibility.changed", visibility={"restricted": ["u_" + U1]}) is not None


def test_u_references_need_a_joined_hub_and_a_known_user(w):
    u = w.ticket()
    r = hub(w, u, "people.changed", role="watchers", add=["u_" + U3], remove=[])
    assert r.code == Code.MEMBER_UNKNOWN  # U3 has no role in the snapshot
    assert hub(w, u, "people.changed", role="watchers", add=["u_" + U1], remove=[]) is None
    # a standalone workspace never holds a u_ reference (D79)
    s = World(validate=False).bootstrap({"mara": "maintainer"})
    su = s.ticket()
    _, r = s.try_(su, "people.changed", "sev", role="watchers", add=["u_" + U1], remove=[])
    assert (r.code, r.detail) == (Code.HUB_INVALID, "not_joined")


def test_a_restricted_ticket_is_visible_to_the_listed_person_through_the_alias(w):
    u = w.ticket()
    w.tev(u, "visibility.changed", "sev", visibility={"restricted": [w.people["sev"]]})
    r = hub(w, u, "handoff.written", text="hi")
    assert r is not None and r.code == Code.TICKET_NOT_VISIBLE
    w.tev(u, "visibility.changed", "sev", visibility={"restricted": [w.people["sev"], w.people["mara"]]})
    assert hub(w, u, "handoff.written", text="hi") is None  # u_ aliased to the listed p_mara


# --- claims ------------------------------------------------------------------------------------------------------


def test_a_hub_actor_releases_a_claim_by_the_ticket_owner_or_manager_rule(w):
    u = w.ticket()
    w.fill(u)
    a = w.claim(u, "sev")
    assert hub(w, u, "claim.released", session=a["session"], reason="released") is None


# --- sign_methods hub and the D66 factor floor -------------------------------------------------------------------


def test_sign_methods_without_hub_refuses_every_hub_event(w, uid):
    w.settings(sign_methods=["terminal", "click"])
    assert decide(w, uid).code == Code.SIGN_METHOD
    w.settings(sign_methods=["terminal", "hub"])
    assert decide(w, uid) is None


def test_the_factor_floor_is_one_function_and_replay_applies_it(w, uid):
    core = w.state()._core
    t = core.tickets[uid]
    assert policies.factor_floor(core.ws, t, "gate.approved", "requirements") == "password+totp"
    assert policies.factor_floor(core.ws, t, "ticket.updated") == "password+totp"
    core.ws.policies["requirements"]["factor"] = "secure-enclave-unlocked"
    assert policies.factor_floor(core.ws, t, "gate.approved", "requirements") == "se-unlocked"
    t.overrides["requirements"] = {"factor": "secure-enclave"}
    assert policies.factor_floor(core.ws, t, "gate.approved", "requirements") == "passkey"
    assert policies.factor_floor(core.ws, t, "verdict.given") == "password+totp"  # the verify gate has no key
    core.ws.policies["verify"]["factor"] = "nonsense"
    assert policies.factor_floor(core.ws, t, "verdict.given") == "passkey"  # unknown value: strongest, fail closed
    e = w.build_unappended(
        uid, "gate.approved", hub_actor(U2), gate="requirements", gate_gen=0, hash="h", policy_hash="p",
        roster_v=w.roster_v, **hub_fields(channel_factor="se-unlocked"),
    )  # fmt: skip
    r = authz.authorize_hub(core, uid, e, w.verifier)
    assert isinstance(r, Refusal) and (r.code, r.detail) == (Code.HUB_DENIED, "factor_too_weak")
    e["channel_factor"] = "passkey"
    assert authz.authorize_hub(core, uid, e, w.verifier) is None


# --- user ids are recorded at admission (Opus P1 on PR A) --------------------------------------------------------


def test_an_admitted_hub_event_records_every_u_id_it_uses_under_its_hub(w):
    u = w.ticket()
    assert hub(w, u, "people.changed", role="watchers", add=["u_" + U1], remove=[]) is None
    book = w.state()._core.ws.hub["user_hub"]
    assert book[U2] == HUB and book[U1] == HUB  # the actor and the payload reference


def test_hub_b_cannot_present_a_user_id_hub_a_used_no_state_injected(w):
    """Hub A uses u_X (as actor and in a payload); after a leave and a join of hub B, B presents u_X: foreign_user."""
    u = w.ticket()
    assert hub(w, u, "people.changed", role="watchers", add=["u_" + U1], remove=[]) is None
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    hub_joined(w, [], hub=HUB_B, root=ROOT_B, seq=2)
    pin(w, card(1, HUB_B), HUB_B)
    pin(w, snapx(1, {U1: "admin", U3: "admin"}, hub=HUB_B), HUB_B)
    ev = {"hub_id": HUB_B, "snapshot_version": 1}
    # as the actor
    _, r = w.try_(u, "ticket.closed", hub_actor(U1), roster_v=w.roster_v, **hub_fields(**ev))
    assert r.code == Code.HUB_FOREIGN_USER
    # as a payload reference of an otherwise fine event of hub B
    _, r = w.try_(u, "people.changed", hub_actor(U3), roster_v=w.roster_v, role="watchers", add=["u_" + U1],
                  remove=[], **hub_fields(**ev))  # fmt: skip
    assert r.code == Code.HUB_FOREIGN_USER
    # B's own new id is fine and is recorded under B
    _, r = w.try_(u, "ticket.closed", hub_actor(U3), roster_v=w.roster_v, **hub_fields(**ev))
    assert not hasattr(r, "code") or r.code != Code.HUB_FOREIGN_USER
    assert w.state()._core.ws.hub["user_hub"].get(U3) in (None, HUB_B)


def test_hub_is_not_a_custody_auth_value():
    from orch.custody.base import AUTH_VALUES

    assert "hub" not in AUTH_VALUES  # hub is custody metadata of a hub-signed line, never a backend a host signs with


# --- u_ references are found from the schema, not from a list (security review of the first push) -------------------


def _put(node: dict, path: tuple[str, ...], value: str) -> None:
    """Set ``value`` at the key path (``"*"`` is a list item) in a fresh nested document."""
    for i, k in enumerate(path[:-1]):
        nxt = [] if path[i + 1] == "*" else {}
        node = node.setdefault(k, nxt)
    if path[-1] == "*":
        node.append(value)  # type: ignore[attr-defined]
    else:
        node[path[-1]] = value


def test_every_person_ref_field_of_every_schema_is_covered_by_u_refs():
    """The personRef places come from the schemas themselves. This table is the guard: a new personRef field changes
    it, and ``u_refs`` (and so ``record_users``) already covers it, because it walks the same paths."""
    from orch.model import identity
    from orch.schema import event_types, person_ref_paths

    found = {t: person_ref_paths("event." + t) for t in event_types() if person_ref_paths("event." + t)}
    assert found == {
        "people.changed": (("add", "*"), ("remove", "*")),
        "question.asked": (("question", "to"),),
        "ticket.created": (("owner",),),
        "visibility.changed": (("visibility", "restricted", "*"),),
    }
    # every custodial type that has a personRef field is one the model lets a hub actor sign
    assert set(found) & authz.HUB_TYPES == set(found)
    # and u_refs reads exactly those places, one distinct id each
    for typ, paths in found.items():
        ids = [f"{i:032x}" for i in range(len(paths))]
        e: dict = {"type": typ, "actor": {"kind": "person", "id": "u_" + "e" * 32, "via": CHAN}}
        for path, uid in zip(paths, ids, strict=True):
            _put(e, path, "u_" + uid)
        assert set(identity.u_refs(e)) == {"e" * 32, *ids}, typ


def test_a_foreign_owner_on_ticket_created_is_foreign_user(w):
    """Hub A used u_X; hub B then names u_X as the owner of a new ticket: refused, though X is not the actor."""
    u = w.ticket()
    assert hub(w, u, "people.changed", role="watchers", add=["u_" + U1], remove=[]) is None
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1)
    hub_joined(w, [], hub=HUB_B, root=ROOT_B, seq=2)
    pin(w, card(1, HUB_B), HUB_B)
    pin(w, snapx(1, {U3: "admin", U1: "admin"}, hub=HUB_B), HUB_B)
    ev = {"hub_id": HUB_B, "snapshot_version": 1}
    base = {"key": "DEMO-0099", "ticket_type": "feature", "title": "t"}
    uid2 = "01J9ZK4Q7M3R8T2V6X0B000099"
    _, r = w.try_(
        uid2, "ticket.created", hub_actor(U3), roster_v=w.roster_v, owner="u_" + U1, **base, **hub_fields(**ev)
    )
    assert r.code == Code.HUB_FOREIGN_USER
    _, r = w.try_(
        uid2, "ticket.created", hub_actor(U3), roster_v=w.roster_v, owner="u_" + U3, **base, **hub_fields(**ev)
    )
    assert getattr(r, "code", None) != Code.HUB_FOREIGN_USER


def _ask(w, u, to):
    from orch.canon import question_hash, question_id

    q = {"id": "Q1", "text": "which?", "to": to, "blocking": False, "options": [{"key": "a", "label": "A"}]}
    uidt = w.state()._core.tickets[u].uid
    qid = question_id(w.workspace_id, uidt, "Q1")
    h = question_hash(qid, uidt, "which?", q["options"])
    w.tev(u, "question.asked", "sev", question=q, qid=qid, hash=h)
    return h


@pytest.mark.parametrize("hub_role", ["edit", "admin"])
def test_a_member_addressee_with_hub_role_edit_can_answer_through_the_alias(hub_role):
    w = World(validate=False).bootstrap({"mara": "maintainer", "tom": "member"})
    hub_joined(w, [alias(w, "tom", U2)])
    pin(w, card())
    pin(w, snapx(1, {U1: "admin", U2: hub_role}))
    u = w.ticket()
    h = _ask(w, u, w.people["tom"])
    r = hub(w, u, "question.answered", question="Q1", hash=h, option="a")
    assert r is None, r
    assert w.state()._core.tickets[u].questions["Q1"].answer["by"] == w.people["tom"]


def test_a_member_via_hub_edit_cannot_answer_a_question_to_someone_else():
    w = World(validate=False).bootstrap({"mara": "maintainer", "tom": "member"})
    hub_joined(w, [alias(w, "tom", U2)])
    pin(w, card())
    pin(w, snapx(1, {U1: "admin", U2: "edit"}))
    u = w.ticket()
    h = _ask(w, u, w.people["mara"])
    r = hub(w, u, "question.answered", question="Q1", hash=h, option="a")
    assert r is not None and r.code == Code.ANSWER_NOT_ALLOWED

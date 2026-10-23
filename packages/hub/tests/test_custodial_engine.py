"""The custodial signing engine (protocol.md §8): prepare -> channel proof over the digest -> recompute -> UK."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from custodial_helpers import ACTION, LOG, WK, WS, Env

from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import suite as S
from orch_hub.contract.canon import unb64u
from orch_hub.contract.hub import Refused
from orch_hub.contract.labels import custodial_label
from orch_hub.custodial import BuiltEvent, CustodialEngine

VEC = json.loads((Path(__file__).parent.parent / "vectors" / "vectors_hub.json").read_text())


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def code(fn, *a, **kw):
    with pytest.raises(Refused) as r:
        fn(*a, **kw)
    return r.value.code


def verify(env, ev):
    held = env.keys.verify_card(env.keys.card())
    snap = env.snaps.snapshot(WS, "full")
    return H.verify_custodial(
        ev, workspace_id=WS, log=LOG, held=held, snapshot=snap["o"], now_ms=env.clock[0], dev_mode=True
    )


def test_vector_digest_and_proof_bytes_match_the_contract():
    c = VEC["custodial"]
    e = {k: v for k, v in c["event"].items() if k not in ("sig", "channel_proof")}
    assert H.event_digest(VEC["ids"]["workspace_a"], c["log"], e).hex() == c["digest_hex"]
    assert H.channel_proof_bytes(bytes.fromhex(c["digest_hex"])).hex() == c["proof_signed_hex"]
    assert c["uk_label"].encode() == custodial_label(c["event"]["type"])


def test_happy_path_signs_audits_delivers(env):
    p = env.prepare()
    # the channel gets ctx(E), recomputes the digest itself (WYSIWYS, L2-5) and signs it
    ev = p.ctx["event"]
    assert ev["actor"] == {"kind": "person", "id": "u_" + env.user, "via": env.channel}
    assert ev["auth"] == "hub" and ev["channel_factor"] == "se-unlocked" and ev["text"] == "hello"
    assert H.event_digest(WS, LOG, ev) == p.digest == bytes.fromhex(p.public()["digest"])
    signed = env.engine.sign(p.prepared_id, env.channel, env.proof(p.digest))
    assert verify(env, signed)["user_id"] == env.user
    assert env.delivered == [(WS, LOG, signed)]
    recs = env.audit.store.after(0, 10)
    assert len(recs) == 1
    _, _, a = recs[0]
    assert (a["workspace_id"], a["log"], a["verb"], a["user_id"], a["channel_id"]) == (
        WS,
        LOG,
        "log.added",
        env.user,
        env.channel,
    )
    assert unb64u(a["digest"], 32) == p.digest


def test_wk_is_opened_for_the_build_only_and_wiped(env):
    env.prepare()
    assert env.view.seen_wk_copy == WK
    assert bytes(env.view.seen_wk) == bytes(32)  # zeroised after the build
    assert not any(WK in v.encode() for v in map(str, env.prepare().ctx.values()))  # never in the context


def test_proof_over_another_digest_is_refused_and_consumes_the_request(env):
    p = env.prepare()
    other = env.prepare(dict(ACTION))
    assert other.digest != p.digest or other.prepared_id != p.prepared_id
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(b"\x01" * 32)) == "bad_channel_proof"
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "unknown_prepared"  # single use
    assert env.delivered == [] and env.audit.store.head() is None


def test_proof_by_another_key_is_refused(env):
    p = env.prepare()
    other = ec.generate_private_key(ec.SECP256R1())
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest, other)) == "bad_channel_proof"
    assert env.delivered == []


@pytest.mark.parametrize("bad", ["", "AAAA", "!!", "A" * 86 + "=", 5])
def test_malformed_proof_is_refused(env, bad):
    p = env.prepare()
    assert code(env.engine.sign, p.prepared_id, env.channel, bad) == "bad_channel_proof"


def test_prepared_is_single_use(env):
    p = env.prepare()
    env.engine.sign(p.prepared_id, env.channel, env.proof(p.digest))
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "unknown_prepared"
    assert len(env.delivered) == 1


def test_prepared_expires(env):
    p = env.prepare()
    env.clock[0] += 120_001
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "prepared_expired"
    assert env.delivered == []


def test_unknown_prepared_and_wrong_channel(env):
    assert code(env.engine.sign, "00" * 16, env.channel, "x") == "unknown_prepared"
    p = env.prepare()
    other = env.new_channel(ec.generate_private_key(ec.SECP256R1()))
    assert code(env.engine.sign, p.prepared_id, other, env.proof(p.digest)) == "other_user"


def test_no_role_refused(tmp_path):
    e = Env(tmp_path, role=None)
    try:
        assert code(e.prepare) == "forbidden_role"
        assert e.delivered == [] and e.audit.store.head() is None
    finally:
        e.close()


def test_read_role_cannot_sign_and_edit_cannot_sign_admin_types(tmp_path, env):
    r = Env(tmp_path / "r", role="read")
    try:
        assert code(r.prepare) == "forbidden_role"
    finally:
        r.close()
    assert (
        code(
            env.prepare,
            {"type": "people.changed", "log": LOG, "role": "watchers", "add": ["u_" + "a" * 32], "remove": []},
        )
        == "forbidden_role"
    )  # admin-only (§8.5)
    assert code(env.prepare, {"type": "addon.purged", "log": "workspace", "name": "wiki"}) == "forbidden_role"


def test_role_lost_between_prepare_and_sign(env):
    p = env.prepare()
    env.access.revoke(WS, "team")
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "forbidden_role"
    assert env.delivered == []


def test_channel_revoked_in_the_snapshot_is_refused(env):
    env.access.revoke_channel(env.channel)
    assert code(env.prepare) == "channel_revoked"


def test_channel_revoked_between_prepare_and_sign(env):
    p = env.prepare()
    env.access.revoke_channel(env.channel)
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "channel_revoked"
    assert env.delivered == [] and env.audit.store.head() is None


def test_channel_deregistered_between_prepare_and_sign(env):
    p = env.prepare()
    env.channels.certs.clear()
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "channel_revoked"


def test_channel_of_another_user_is_refused(env):
    other_user = env.keys.issue_uk()["o"]["user_id"]
    cid = env.new_channel(ec.generate_private_key(ec.SECP256R1()), user=other_user)
    assert (
        code(env.engine.prepare, "sess", WS, env.user, cid, dict(ACTION)) == "channel_revoked"
    )  # not this user's: not active


def test_selective_workspace_is_refused_without_opening_wk(env):
    env.mode = "selective"
    assert code(env.prepare) == "wrong_mode"
    assert env.view.calls == 0


def test_prepare_reply_digest_is_what_a_channel_recomputes(env):
    from orch_hub.contract import phase4 as P4

    p = env.prepare()
    assert P4.check_prepared(p.public()["ctx"], p.public()["digest"]) == p.digest
    assert (
        code(P4.check_prepared, {**p.ctx, "event": {**p.ctx["event"], "text": "x"}}, p.digest.hex()) == "event_changed"
    )


def test_event_changed_between_prepare_and_sign(env):
    p = env.prepare()
    env.view.text = "tampered"
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "event_changed"
    assert env.delivered == [] and env.audit.store.head() is None


def test_snapshot_version_change_is_event_changed(env):
    p = env.prepare()
    env.access.add_group("other")
    other = env.keys.issue_uk()["o"]["user_id"]
    env.access.add_user(other, "bob")
    env.access.add_member("other", other)
    env.access.grant(WS, "other", "read")  # roles changed -> a new snapshot version
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "event_changed"


def test_uk_signature_is_under_the_custodial_label_of_its_type_only(env):
    p = env.prepare()
    ev = env.engine.sign(p.prepared_id, env.channel, env.proof(p.digest))
    pub = unb64u(env.keys.uk_cert(env.user)["o"]["uk_pub"], 65)
    sig = unb64u(ev["sig"], 64)
    signed_ctx = {k: v for k, v in ev.items() if k != "sig"}
    good = H.custodial_bytes(WS, LOG, signed_ctx)
    assert S.verify(pub, sig, good)
    bad_type = H.custodial_bytes(WS, LOG, {**signed_ctx, "type": "gate.approved"})
    assert not S.verify(pub, sig, bad_type)
    assert not S.verify(pub, sig, H.L["sig_hub_snapshot"] + good[len(custodial_label("log.added")) :])


def test_uk_signer_refuses_everything_but_custodial_events(env):
    signer = env.keys.uk_signer(env.user)
    for kind in ("hub.snapshot", "hub.uk_cert", "hub.audit"):  # no object kind is signable by a UK
        with pytest.raises(KS.KeyUseError):
            signer.sign_object({"kind": kind})
    with pytest.raises(KS.KeyUseError):
        signer.sign_event(WS, LOG, {"auth": "device", "type": "log.added"})
    with pytest.raises(KS.KeyUseError):
        signer.sign_event(WS, LOG, {"auth": "hub", "type": "not a type|x", "sig": "x"})
    with pytest.raises(KS.KeyUseError):
        signer.sign_event(WS, LOG, {"auth": "hub", "type": "bad type"})


def test_no_entry_point_takes_event_bytes():
    public = [n for n, f in inspect.getmembers(CustodialEngine, inspect.isfunction) if not n.startswith("_")]
    assert sorted(public) == ["prepare", "sign"]
    assert list(inspect.signature(CustodialEngine.sign).parameters)[1:] == ["prepared_id", "channel_id", "proof"]
    assert list(inspect.signature(CustodialEngine.prepare).parameters)[1:] == [
        "session",
        "workspace_id",
        "user_id",
        "channel_id",
        "action",
    ]


@pytest.mark.parametrize(
    "field", ["actor", "auth", "channel", "sig", "channel_proof", "hub_id", "uk_cert", "id", "v", "seq"]
)
def test_view_cannot_set_engine_owned_fields(env, field):
    env.view.extra = {field: "x"}
    assert code(env.prepare) == "malformed"


def test_view_must_supply_the_event_core_and_a_valid_type(env):
    env.view.etype = "Not A Type"
    assert code(env.prepare) == "malformed"


def test_audit_failure_blocks_delivery(env):
    p = env.prepare()

    def boom(**kw):
        raise RuntimeError("disk full")

    env.audit.custodial = boom
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "unavailable"
    assert env.delivered == []


@pytest.mark.parametrize(
    "typ", ["role.changed", "grant.issued", "claim.taken", "task.done", "artifact.added", "member.added", "x.y"]
)
def test_types_outside_the_closed_table_are_not_custodial(env, typ):
    assert code(env.prepare, {"type": typ, "log": LOG, "text": "x"}) == "not_custodial"
    assert env.view.calls == 0


def test_log_kind_matters_for_the_table(env):
    assert code(env.prepare, {"type": "log.added", "log": "workspace", "text": "x"}) == "not_custodial"


@pytest.mark.parametrize(
    "extra",
    [
        {"set": {"ticket.visibility": "x"}},
        {"based_on": "x"},
        {"actor": {}},
        {"sig": "x"},
        {"channel_proof": "x"},
        {"gate_gen": 1},
        {"unknown": 1},
    ],
)
def test_action_fields_are_exact_no_smuggling(env, extra):
    assert code(env.prepare, {"type": "log.added", "log": LOG, "text": "x", **extra}) == "malformed"
    upd = {"type": "ticket.updated", "log": LOG, "set": {"ticket.tasks": []}}
    assert code(env.prepare, upd) == "malformed"
    assert env.view.calls == 0


def test_text_rule_rejects_display_spoofing(env):
    assert code(env.prepare, {"type": "log.added", "log": LOG, "text": "a\u202eb"}) == "malformed"


def test_ticket_created_has_no_log_and_gets_a_new_uid(env):
    act = {"type": "ticket.created", "ticket_type": "bug", "title": "t"}
    p = env.prepare(act)
    assert p.log == "01J9ZP0000000000000000NEW1"
    assert code(env.prepare, {**act, "log": LOG}) == "malformed"
    env.view.log = "workspace"
    assert code(env.prepare, act) == "malformed"


def test_view_cannot_change_the_type_or_log_of_the_action(env):
    env.view.etype = "ticket.closed"
    assert code(env.prepare) == "event_changed"
    env.view.etype = None
    env.view.log = "01J9ZP0000000000000000OTHR"
    assert code(env.prepare) == "event_changed"


def test_decision_hash_moved_is_event_changed(env):
    h = "sha256:" + "11" * 32
    act = {"type": "gate.approved", "log": LOG, "gate": "plan", "hash": h}
    env.view.extra = {"hash": h, "gate": "plan"}
    p = env.prepare(act)
    env.view.extra = {"hash": "sha256:" + "22" * 32, "gate": "plan"}  # content moved since the person looked
    assert code(env.prepare, act) == "event_changed"
    assert code(env.engine.sign, p.prepared_id, env.channel, env.proof(p.digest)) == "event_changed"
    assert env.delivered == []


def test_decision_event_without_hash_is_malformed(env):
    h = "sha256:" + "11" * 32
    act = {"type": "gate.approved", "log": LOG, "gate": "plan", "hash": h}
    env.view.extra = {"gate": "plan"}  # the view built no hash at all: check_built never skips it
    assert code(env.prepare, act) == "malformed"


def test_malformed_ids_and_action_are_refused(env):
    assert code(env.engine.prepare, "sess", "zz", env.user, env.channel, {}) == "malformed"
    assert code(env.engine.prepare, "sess", WS, env.user, env.channel, "not a dict") == "malformed"


def test_per_user_cap_evicts_only_that_users_oldest(env):
    from orch_hub.custodial import engine as E

    first = env.prepare()
    for _ in range(E.MAX_PER_USER + 2):
        env.prepare()
    assert code(env.engine.sign, first.prepared_id, env.channel, env.proof(first.digest)) == "unknown_prepared"
    assert len(env.engine._pending) == E.MAX_PER_USER


def test_one_user_filling_the_cap_does_not_affect_another(tmp_path, env):
    from orch_hub.custodial import engine as E

    b_priv = ec.generate_private_key(ec.SECP256R1())
    bob = env.keys.issue_uk()["o"]["user_id"]
    env.access.add_user(bob, "bob")
    env.access.add_member("team", bob)
    bch = env.new_channel(b_priv, user=bob)
    pb = env.engine.prepare("sess", WS, bob, bch, dict(ACTION))
    for _ in range(E.MAX_PER_USER * 3):
        env.prepare()
    env.engine.sign(pb.prepared_id, bch, env.proof(pb.digest, b_priv))
    assert len(env.delivered) == 1


def test_prepare_needs_the_users_own_session(env):
    assert code(env.engine.prepare, "stolen", WS, env.user, env.channel, dict(ACTION)) == "unauthorized"
    env.sessions.ok = False
    assert code(env.prepare) == "unauthorized"
    assert env.view.calls == 0


def test_built_event_type_is_checked(env):
    class Bad:
        def build(self, *a):
            return {"log": "x"}

    env.engine._view = Bad()
    assert code(env.prepare) == "malformed"
    assert BuiltEvent("workspace", {}).log == "workspace"

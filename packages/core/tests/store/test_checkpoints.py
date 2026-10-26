"""Checkpoints (§5.10): written, signed, verified on open; divergence refuses events; restore carries revocations."""

from __future__ import annotations

import json
import shutil

import pytest

from orch import canon, crypto, schema
from orch.ops.caps import Files
from orch.store import StoreError, checkpoints
from tests.store.helpers import WS


def cp_files(env):
    return sorted(p.name for p in (env.store_dir / ".state" / "checkpoints").glob("*.json"))


def load(env, name):
    return json.loads((env.store_dir / ".state" / "checkpoints" / name).read_bytes())


def test_checkpoints_have_the_protocol_shape_and_a_valid_wsk_signature(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid)
    t = load(env, f"ticket-{uid}.json")
    schema.validate("checkpoint", t)
    o = t["o"]
    assert o["kind"] == "ticket_checkpoint" and o["seq"] == 2 and o["uid"] == uid and o["workspace_id"] == WS
    assert o["head"] == canon.event_head(env.read_events(uid)[-1]) and o["v"] == 2 and o["suite"] == 2
    assert checkpoints.verify_object(env.wsk_pub, t)
    w = load(env, "workspace.json")
    schema.validate("checkpoint", w)
    assert w["o"]["workspace_log"] == {
        "seq": 2,
        "head": canon.event_head(env.read_events("workspace")[-1]),
        "acc": s._logs["workspace"].acc,
    }
    assert w["o"]["genesis"] == s.genesis and w["o"]["n"] >= 1
    assert checkpoints.verify_object(env.wsk_pub, w)
    forged = {**t, "o": {**t["o"], "seq": 9}}
    assert not checkpoints.verify_object(env.wsk_pub, forged)
    assert not checkpoints.verify_object(crypto.public_bytes(crypto.generate_private_key()), t)


def test_workspace_checkpoint_numbers_count_up_and_follow_workspace_appends(env):
    env.bootstrap()
    n1 = load(env, "workspace.json")["o"]["n"]
    env.add_device()
    n2 = load(env, "workspace.json")["o"]["n"]
    assert n2 == n1 + 1
    uid = env.new_ticket()
    assert load(env, "workspace.json")["o"]["n"] == n2  # ticket events write only the ticket checkpoint
    assert f"ticket-{uid}.json" in cp_files(env)


def test_workspace_checkpoint_after_every_fifty_ticket_events(env):
    env.bootstrap()
    uid = env.new_ticket()
    n = load(env, "workspace.json")["o"]["n"]
    for _ in range(checkpoints.WORKSPACE_EVERY):
        env.log(uid)
    assert load(env, "workspace.json")["o"]["n"] == n + 1
    assert load(env, "workspace.json")["o"]["tickets"][uid]["seq"] >= 50


def test_truncating_a_log_below_its_checkpoint_is_divergence_and_refuses_events(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    for _ in range(3):
        env.log(uid)
    s.close()
    p = env.path(uid, "events.jsonl")
    ls = p.read_bytes().splitlines(keepends=True)
    p.write_bytes(b"".join(ls[:2]))  # the last two events are gone
    s = env.open()
    d = s.diverged[uid]
    assert "before the checkpoint" in d.detail and d.code == "chain.diverged"
    with pytest.raises(StoreError) as e:
        env.log(uid)
    assert e.value.code == "chain.diverged"
    other = next(u for u in s.state.tickets if u != uid) if len(s.state.tickets) > 1 else None
    assert other is None  # (only one ticket here)
    with pytest.raises(StoreError):  # a diverged log might hide a key: no new ticket until it is restored
        s.create_ticket(actor=env.agent, ticket_type="chore", title="n", owner=env.owner.ref)


def test_a_missing_ticket_log_is_divergence(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    s.close()
    shutil.rmtree(env.store_dir / "tickets" / uid)
    assert "missing" in env.open().diverged[uid].detail


def test_equal_height_different_head_is_chain_diverged(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "real")
    s.close()
    p = env.path(uid, "events.jsonl")
    ls = p.read_bytes().splitlines(keepends=True)
    e = canon.parse_event_line(ls[-1])
    e["text"] = "rewritten"
    e["host_sig"] = crypto.b64u(env.signer.sign(canon.host_signing_bytes(WS, uid, e)))  # valid, even
    ls[-1] = canon.event_line(e)
    p.write_bytes(b"".join(ls))
    s = env.open()
    assert s.chain_errors() == []  # the chain itself verifies ...
    d = s.diverged[uid]  # ... but it is not the chain the host checkpointed
    assert d.code == "chain.diverged" and "differs from the checkpoint" in d.detail
    with pytest.raises(StoreError) as ex:
        env.log(uid)
    assert ex.value.code == "chain.diverged"


def test_the_checkpoint_writer_never_moves_a_checkpoint_to_another_head_or_backwards(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid)
    cps = checkpoints.Checkpoints(env.store_dir / ".state" / "checkpoints", WS)
    assert (
        cps.write_ticket(env.signer.sign, uid, 2, "sha256:" + "0" * 64, "2026-10-10T10:00:00Z", "sha256:" + "0" * 64)
        == "diverged"
    )
    assert (
        cps.write_ticket(env.signer.sign, uid, 1, "sha256:" + "1" * 64, "2026-10-10T10:00:00Z", "sha256:" + "0" * 64)
        == "diverged"
    )
    assert s.diverged == {}


def test_a_checkpoint_not_signed_by_the_host_key_is_a_problem_of_its_log(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    s.close()
    path = env.store_dir / ".state" / "checkpoints" / f"ticket-{uid}.json"
    cp = json.loads(path.read_bytes())
    cp["o"]["at"] = "2020-01-01T00:00:00Z"
    path.write_bytes(canon.cj_checked(cp) + b"\n")
    assert "not signed" in env.open().diverged[uid].detail


def test_a_checkpoint_for_another_genesis_is_refused_outright(env):
    s = env.bootstrap()
    s.close()
    path = env.store_dir / ".state" / "checkpoints" / "workspace.json"
    cp = json.loads(path.read_bytes())
    cp["o"]["genesis"] = "sha256:" + "5" * 64
    cp["sig"] = crypto.b64u(env.signer.sign(checkpoints.LABEL + canon.cj_checked(cp["o"])))
    path.write_bytes(canon.cj_checked(cp) + b"\n")
    with pytest.raises(StoreError) as e:
        env.open()
    assert e.value.code == "trust.genesis_mismatch"


def rollback(env, backup):
    """Put the backed-up logs and projections back, keep ``.state`` (git ignores it; a force-push restores the rest)."""
    for name in ("events", "tickets", "config.json", "keys.jsonl"):
        target = env.store_dir / name
        shutil.rmtree(target) if target.is_dir() else target.unlink(missing_ok=True)
        src = backup / ".orch" / name
        shutil.copytree(src, target) if src.is_dir() else shutil.copy(src, target)


def test_restore_after_a_rollback_abandons_the_checkpoint_and_new_chain_continues(env, tmp_path):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "kept")
    s.close()
    backup = tmp_path / "backup"
    shutil.copytree(env.root, backup)
    s = env.open()
    for i in range(3):
        env.log(uid, f"lost-{i}")
    cp_head = canon.event_head(env.read_events(uid)[-1])
    s.close()
    rollback(env, backup)
    s = env.open()
    assert "before the checkpoint" in s.diverged[uid].detail
    facts = s.restore_facts(uid)
    assert facts["from_seq"] == 2 and facts["abandoned"] == {"seq": 5, "head": cp_head}
    with pytest.raises(StoreError) as e:  # anything but a restore is refused
        env.log(uid)
    assert e.value.code == "chain.diverged"
    bad = env.person_event(env.owner, uid, "restore", **{**facts, "abandoned": None, "reason": "rollback"})
    with pytest.raises(StoreError) as e:
        s.append(bad, log=uid)
    assert e.value.code == "validation.restore"
    ok = env.person_event(env.owner, uid, "restore", **facts, reason="rollback")
    r = s.append(ok, log=uid)
    assert r.event["seq"] == 3 and r.event["prev"] == facts["head"]
    assert s.diverged == {}
    assert (env.store_dir / ".state" / "checkpoints" / "abandoned").is_dir()
    env.log(uid, "after")
    s.close()
    again = env.open()  # and a fresh open agrees: no divergence, the new chain is the checkpointed one
    assert again.diverged == {} and again.chain_errors() == []
    assert load(env, f"ticket-{uid}.json")["o"]["seq"] == 4
    gates = again.state.tickets[uid].gates
    assert all(
        gates[g].gen >= 1 for g in ("requirements", "plan", "verify")
    )  # a restore raises every gate that applies


def test_restore_of_a_forked_tail_uses_abandon_tail_and_keeps_the_cut_lines(env):
    s = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "one")
    env.log(uid, "two")
    s.close()
    p = env.path(uid, "events.jsonl")
    ls = p.read_bytes().splitlines(keepends=True)
    e = canon.parse_event_line(ls[-1])
    e["text"] = "fork"
    e["host_sig"] = crypto.b64u(env.signer.sign(canon.host_signing_bytes(WS, uid, e)))
    ls[-1] = canon.event_line(e)
    p.write_bytes(b"".join(ls))
    s = env.open()
    assert uid in s.diverged
    assert s.abandon_tail(uid, 2) == 1
    assert any((env.store_dir / ".state" / "abandoned").glob(f"{uid}-3.jsonl"))
    facts = s.restore_facts(uid)
    assert facts["from_seq"] == 2 and facts["abandoned"] == {
        "seq": 3,
        "head": load(env, f"ticket-{uid}.json")["o"]["head"],
    }
    s.append(env.person_event(env.owner, uid, "restore", **facts, reason="diverged"), log=uid)
    assert s.diverged == {}
    assert env.log(uid, "continues").event["seq"] == 4


def test_workspace_restore_puts_back_every_person_signed_revocation(env, tmp_path):
    s = env.bootstrap()
    d2, _ = env.add_device()
    s.close()
    backup = tmp_path / "backup"
    shutil.copytree(env.root, backup)
    s = env.open()
    env.revoke_device(d2)
    assert s.state.workspace.devices[d2].revoked == "compromised"
    s.close()
    rollback(env, backup)  # the revocation is gone from the chain, but the host remembers it
    s = env.open()
    assert WS and "workspace" in s.diverged
    assert s.state.workspace.devices[d2].revoked is None
    facts = s.restore_facts("workspace")
    r = s.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="rollback"), log="workspace")
    evs = env.read_events("workspace")
    assert evs[-1]["type"] == "restore" and evs[-1]["seq"] == r.event["seq"]
    assert evs[-1]["revocations"][0]["revocation"]["o"]["device_id"] == d2[2:]
    assert s.state.workspace.devices[d2].revoked == "compromised"
    s.close()
    again = env.open()
    assert again.diverged == {} and again.chain_errors() == [] and not again.state.workspace.invalid


def test_a_revocation_for_a_device_the_restored_chain_never_had_is_a_tombstone(env, tmp_path):
    s = env.bootstrap()
    s.close()
    backup = tmp_path / "backup"
    shutil.copytree(env.root, backup)
    s = env.open()
    d2, _ = env.add_device()
    env.revoke_device(d2)
    s.close()
    rollback(env, backup)
    s = env.open()
    facts = s.restore_facts("workspace")
    s.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    assert env.read_events("workspace")[-1]["type"] == "restore"
    assert d2 in {device for device, _person in s.state.revocation_evidence()}


@pytest.mark.parametrize("host", [True, False])
def test_missing_checkpoint_repair_requires_the_host_and_a_verified_log(env, host):
    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "original")
    store.close()
    path = env.store_dir / ".state" / "checkpoints" / f"ticket-{uid}.json"
    path.unlink()
    if host:
        log = env.path(uid, "events.jsonl")
        log.write_bytes(log.read_bytes().replace(b"original", b"tampered"))
    store = env.open(host=env.signer if host else None)
    store.repair_checkpoints()
    assert not path.exists()


@pytest.mark.parametrize("load", ["all", "lazy"])
@pytest.mark.parametrize("rollback", [False, True])
def test_checkpoint_only_finishes_the_recorded_append(env, load, rollback):
    from orch.ops.health import _checkpoints

    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "last append")
    store.close()
    path = env.store_dir / ".state" / "checkpoints" / f"ticket-{uid}.json"
    path.unlink()
    if rollback:
        log = env.path(uid, "events.jsonl")
        log.write_bytes(b"".join(log.read_bytes().splitlines(keepends=True)[:-1]))
    store = env.open(load=load)
    if load == "lazy":
        store.ticket(uid)
        store.checkpoint()
    assert path.exists() is not rollback
    findings = _checkpoints(store, host_here=True, files=Files.direct())
    assert any(f.code == "checkpoint.missing" for f in findings) is rollback


@pytest.mark.parametrize("load", ["all", "lazy"])
@pytest.mark.parametrize("applied_elsewhere", [False, True])
def test_rollback_with_deleted_checkpoint_cannot_be_laundered_by_append(env, load, applied_elsewhere):
    from orch.ops.health import _checkpoints

    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "last append")
    if applied_elsewhere:
        env.new_ticket()
    store.close()
    checkpoint = env.store_dir / ".state" / "checkpoints" / f"ticket-{uid}.json"
    checkpoint.unlink()
    log = env.path(uid, "events.jsonl")
    rolled_back = b"".join(log.read_bytes().splitlines(keepends=True)[:-1])
    log.write_bytes(rolled_back)
    store = env.open(load=load)
    store.ticket(uid)
    assert not store.repairable_checkpoint(uid)
    store.checkpoint()
    assert uid not in json.loads((env.store_dir / ".state/checkpoints/workspace.json").read_bytes())["o"]["tickets"]
    with pytest.raises(StoreError) as exc:
        env.log(uid, "must not hide the rollback")
    assert exc.value.code == "checkpoint.missing"
    assert log.read_bytes() == rolled_back
    assert not checkpoint.exists()
    assert any(f.code == "checkpoint.missing" for f in _checkpoints(store, host_here=True, files=Files.direct()))
    store.close()
    store = env.open(load=load)
    store.ticket(uid)
    assert not store.repairable_checkpoint(uid)
    assert any(f.code == "checkpoint.missing" for f in _checkpoints(store, host_here=True, files=Files.direct()))


def test_checkpoint_cache_is_bound_to_content_and_key(env):
    from orch.store.checkpoints import verify_object

    store = env.bootstrap()
    checkpoint = store._checkpoints.workspace()
    assert verify_object(env.wsk_pub, checkpoint)
    checkpoint["o"]["n"] += 1
    assert not verify_object(env.wsk_pub, checkpoint)
    original = store._checkpoints.workspace()
    assert original["o"]["n"] != checkpoint["o"]["n"]
    assert verify_object(env.wsk_pub, original)
    assert not verify_object(bytes(len(env.wsk_pub)), original)
    store._checkpoints._write(store._checkpoints._workspace_path, checkpoint)
    assert not verify_object(env.wsk_pub, store._checkpoints.workspace())


def test_repeated_checkpoint_reads_and_verification_validate_once(env, monkeypatch):
    store = env.bootstrap()
    checkpoints._validated.cache_clear()
    checkpoints._verified.cache_clear()
    calls = []
    validate = schema.validate

    def counted(name, obj, **kw):
        calls.append(name)
        return validate(name, obj, **kw)

    monkeypatch.setattr(schema, "validate", counted)
    for _ in range(5):
        assert checkpoints.verify_object(env.wsk_pub, store._checkpoints.workspace())
    assert calls == ["checkpoint"]

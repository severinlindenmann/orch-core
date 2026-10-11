"""F1 restore contract vectors at schema, model and durable host boundaries."""

import copy
import json

import pytest

from orch import canon, schema
from orch.identity import CryptoVerifier, certs
from orch.model import admit, replay
from orch.store import Store, StoreError
from tests.model.f1_runner import load

V = load("restore.json")
C = load("checkpoint.json")


@pytest.mark.parametrize("case", V["schema_cases"], ids=lambda c: c["name"])
def test_restore_structure_precedes_signature_and_is_atomic(case, env):
    event = case["event"]
    kind = "workspace" if case["log"] == "workspace" else "ticket"
    with pytest.raises(schema.SchemaError):
        schema.validate("event", event, log=kind)
    sc = V["scenarios"][0]
    genesis = sc["steps"][0]["event"]
    state = replay([genesis], {}, verifier=CryptoVerifier(), now=sc["now"], expected_workspace_id=sc["workspace_id"])
    unsigned = {k: v for k, v in event.items() if k != "host_sig"}
    assert admit(state, unsigned, log=case["log"]).code.value == case["expect"]
    logs = {} if kind == "workspace" else {case["log"]: [event]}
    result = replay(
        [genesis, event] if kind == "workspace" else [genesis],
        logs,
        verifier=CryptoVerifier(),
        now=sc["now"],
        expected_workspace_id=sc["workspace_id"],
    )
    assert result.chain_errors[0].code == case["replay"]

    store = env.bootstrap()
    before = env.read_events("workspace")
    fields = {
        k: copy.deepcopy(event[k]) for k in ("reason", "note", "revocations", "abandoned_decisions") if k in event
    }
    if kind == "ticket":
        uid = env.new_ticket()
    else:
        uid = "workspace"
    fields = {**store.restore_facts(uid), **fields}
    candidate = env.person_event(env.owner, uid, "restore", **fields)
    held = store._pins.held()
    with pytest.raises(StoreError) as exc:
        store.append(candidate, log=uid)
    assert exc.value.code == "validation.event"
    assert env.read_events("workspace") == before
    assert store._pins.held() == held
    assert not list((store._pins.dir / "transitions").glob("*.json"))


@pytest.mark.parametrize("case", V["host_cases"], ids=lambda c: c["name"])
def test_durable_restore_contract(case, env, monkeypatch):
    store = env.bootstrap()
    device, _ = env.add_device()
    before_rev = len(env.read_events("workspace"))
    if not case.get("learned"):
        env.revoke_device(device, reason="lost")
        revocation = env.read_events("workspace")[-1]["revocation"]
    else:
        revocation = certs.make_revocation(
            env.owner.pk_pub, env.owner.pk_sign, device_id_hex=device[2:], revoked_ms=env.clock[0] * 1000, reason="lost"
        )
    if case.get("missing") == "abandoned" or "fault" in case:
        store.abandon_tail("workspace", before_rev)
    facts = store.restore_facts("workspace")
    if case.get("learned"):
        facts["revocations"] = [{"revocation": revocation, **store.state.device_evidence(device)}]
    if "missing" in case:
        facts["revocations"] = []
    candidate = env.person_event(env.owner, "workspace", "restore", **facts, reason="rollback")
    held = store._pins.held()
    if case["name"] == "stale_source":
        store.checkpoint()
        with pytest.raises(StoreError) as exc:
            store.append(candidate, log="workspace")
        assert exc.value.code == case["expect"]
        return
    if "missing" in case:
        with pytest.raises(StoreError) as exc:
            store.append(candidate, log="workspace")
        assert exc.value.code == case["expect"]
        assert store._pins.held() == held
        return
    if case["name"] == "exact_restore_retry":
        result = store.append(candidate, log="workspace")
        target = store._pins.held()
        again = store.append(candidate, log="workspace")
        assert again.event == result.event and again.duplicate
        assert store._pins.held() == target
        return
    fault = "_after_commit" if case["fault"] == "after_commit" else "_write_pending"

    def crash(*args, **kwargs):
        raise RuntimeError("simulated power loss")

    with monkeypatch.context() as patch:
        patch.setattr(Store, fault, crash)
        with pytest.raises(RuntimeError, match="power loss"):
            store.append(candidate, log="workspace")
    if case["fault"] == "after_commit":
        retried = store.append(candidate, log="workspace")
        assert retried.duplicate and retried.event["id"] == candidate["id"]
    store.close()
    store = env.open()
    if case["fault"] == "before_commit":
        assert env.read_events("workspace")[-1]["type"] != "restore"
        assert store._pins.held() == held
        assert "workspace" in store.diverged
        return
    assert env.read_events("workspace")[-1]["type"] == "restore"
    assert store.state.revocation_evidence()[device]["revocation"] == revocation
    assert store._pins.revocations()[0]["revocation"] == revocation
    transition = json.loads(next((store._pins.dir / "transitions").glob("*.json")).read_text())
    assert transition["committed"] and transition["C"] == held
    assert transition["T"]["o"]["n"] <= store._pins.held()["o"]["n"]
    assert transition["T"]["o"]["workspace_log"] == store._pins.held()["o"]["workspace_log"]
    assert transition["event"] == env.read_events("workspace")[-1]
    assert transition["device_cert"] and transition["delegation"] and transition["publication_complete"]
    assert store.diverged == {}


def test_accumulator_folds_decoded_hashes_and_is_incremental():
    acc = "sha256:" + "0" * 64
    for head in C["workspace_log"]["heads"]:
        acc = canon.log_acc(acc, head)
    assert acc == C["workspace_checkpoint"]["signed"]["o"]["workspace_log"]["acc"]
    for bad in ("sha256:" + "00" * 31, "sha256:" + "00" * 33, "00" * 32):
        with pytest.raises(canon.HashError):
            canon.log_acc(bad, C["workspace_log"]["heads"][0])


def test_lazy_open_finishes_ticket_transition_before_other_appends(env, monkeypatch):
    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid)
    store.checkpoint()
    store.abandon_tail(uid, 1)
    candidate = env.person_event(env.owner, uid, "restore", **store.restore_facts(uid), reason="rollback")
    with monkeypatch.context() as patch:

        def crash(*args, **kwargs):
            raise RuntimeError("crash after ticket restore")

        patch.setattr(Store, "_after_commit", crash)
        with pytest.raises(RuntimeError):
            store.append(candidate, log=uid)
    store.close()
    store = env.open(load="lazy")
    transition = json.loads(next((store._pins.dir / "transitions").glob("*.json")).read_text())
    assert transition["committed"] and store._pins.held() == transition["T"]
    env.add_device()
    assert store.diverged == {}
    assert store._pins.held()["o"]["tickets"][uid] == transition["T"]["o"]["tickets"][uid]


def test_checkpoint_and_restore_time_never_go_behind_source(env):
    store = env.bootstrap()
    env.clock[0] += 1000
    store.checkpoint()
    source = store._pins.held()
    env.clock[0] -= 2000
    store.checkpoint()
    assert store._pins.held()["o"]["at"] == source["o"]["at"]
    event = env.person_event(env.owner, "workspace", "restore", **store.restore_facts("workspace"), reason="other")
    result = store.append(event, log="workspace")
    assert result.event["at"] == source["o"]["at"]


def test_unloaded_accumulator_is_copied_forward_and_checked_on_load(env):
    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid)
    store.checkpoint()
    entry = store._pins.held()["o"]["tickets"][uid]
    store.close()
    store = env.open(load="lazy")
    env.add_device()
    assert uid not in store._loaded
    assert store._pins.held()["o"]["tickets"][uid] == entry
    store.ticket(uid)
    assert store._logs[uid].acc == entry["acc"] and store.diverged == {}

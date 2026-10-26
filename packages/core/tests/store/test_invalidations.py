"""Recovery follows replay, including atomic restores and accumulated voids."""

from datetime import datetime

import pytest

from orch import canon
from orch.store import Store, StoreError
from orch.store.invalidations import pending_causes
from orch.store.render import thaw
from tests.model.f1_runner import load


def test_recovery_uses_post_restore_revocations(tmp_path, monkeypatch):
    from orch.model import state

    sc = next(s for s in load("restore.json")["scenarios"] if s["name"] == "restore_carries_revocations_at_its_ws_seq")
    uid = next(s["log"] for s in sc["steps"] if s["log"] != "workspace")
    for step in sc["steps"][:-1]:
        path = (
            tmp_path
            / ".orch"
            / ("events/workspace.jsonl" if step["log"] == "workspace" else f"tickets/{uid}/events.jsonl")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as f:
            f.write(canon.event_line(step["event"]))
    store = Store.open(
        tmp_path,
        expected_workspace_id=sc["workspace_id"],
        expected_genesis=sc["genesis"],
        clock=lambda: datetime.fromisoformat(sc["now"]).timestamp(),
        load="all",
    )
    refused = []
    real = state.apply_event

    def apply(*args, **kwargs):
        result = real(*args, **kwargs)
        if result is not None:
            refused.append(result.code.value)
        return result

    monkeypatch.setattr(state, "apply_event", apply)
    try:
        assert pending_causes(store, uid) == {}
        assert "device.invalid" in refused
    finally:
        store.close()


@pytest.mark.parametrize("cause", ["policy_changed", "restored"])
def test_recovery_records_workspace_and_restore_causes(env, cause):
    store = env.bootstrap()
    uid = env.new_ticket()
    env.fill(uid)
    approval = env.approve(uid).event["id"]
    if cause == "policy_changed":
        policy = thaw(store.state.tickets[uid].gates["requirements"].policy)
        policy["count"] = 2
        event = env.person_event(env.owner, "workspace", "policy.changed", gates={"requirements": policy})
        store.append(event, log="workspace")
    else:
        event = env.person_event(
            env.owner,
            uid,
            "restore",
            **store.restore_facts(uid),
            reason="rollback",
        )
        store.append(event, log=uid)
    store.close()
    store = env.open()
    out = store.flush_invalidations(uid)
    assert [(e["cause"], e["voided"]) for e in out] == [(cause, [approval])]
    assert store.flush_invalidations(uid) == []


def test_accumulated_invalidations_keep_the_latest_cause(env):
    store = env.bootstrap()
    uid = env.new_ticket()
    env.fill(uid)
    first = env.approve(uid).event["id"]
    env.update(uid, body={"requirements": "changed"})
    second = env.approve(uid).event["id"]
    policy = thaw(store.state.tickets[uid].gates["requirements"].policy)
    policy["count"] = 2
    store.append(
        env.person_event(env.owner, "workspace", "policy.changed", gates={"requirements": policy}), log="workspace"
    )
    store.close()
    store = env.open()
    out = store.flush_invalidations(uid)
    assert [(e["cause"], e["voided"]) for e in out] == [("policy_changed", sorted([first, second]))]


def test_missing_recovered_cause_is_a_store_error(env, monkeypatch):
    from orch.store import invalidations

    store = env.bootstrap()
    uid = env.new_ticket()
    env.fill(uid)
    env.approve(uid)
    env.update(uid, body={"requirements": "changed"})
    monkeypatch.setattr(invalidations, "pending_causes", lambda store, uid: {})
    with pytest.raises(StoreError, match="cannot recover the invalidation cause"):
        store.flush_invalidations(uid)

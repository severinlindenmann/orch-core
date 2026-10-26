"""F1 restore contract vectors at schema, model and durable host boundaries."""

import copy
import json

import pytest

from orch import canon, schema
from orch.identity import CryptoVerifier, certs
from orch.model import admit, replay
from orch.store import Store, StoreError
from orch.store.store import _epoch
from tests.model.f1_runner import load
from tests.store.helpers import WS

V = load("restore.json")
C = load("checkpoint.json")


CALLER_FIELDS = ("reason", "note", "revocations", "abandoned_decisions")


def _genesis_scenario():
    sc = V["scenarios"][0]
    return sc, sc["steps"][0]["event"]


@pytest.mark.parametrize("case", V["schema_cases"], ids=lambda c: c["name"])
def test_restore_structure_is_refused_before_authorization(case):
    """Step 0 at the schema and at admit, before any signature or authorization check."""
    event = case["event"]
    kind = "workspace" if case["log"] == "workspace" else "ticket"
    with pytest.raises(schema.SchemaError):
        schema.validate("event", event, log=kind)
    sc, genesis = _genesis_scenario()
    state = replay([genesis], {}, verifier=CryptoVerifier(), now=sc["now"], expected_workspace_id=sc["workspace_id"])
    unsigned = {k: v for k, v in event.items() if k != "host_sig"}
    assert admit(state, unsigned, log=case["log"]).code.value == case["expect"]


@pytest.mark.parametrize("case", V["schema_cases"], ids=lambda c: c["name"])
def test_restore_structure_breaks_the_chain_on_replay(case):
    event = case["event"]
    kind = "workspace" if case["log"] == "workspace" else "ticket"
    if kind == "ticket" and "ws_seq" not in event:
        pytest.skip("replay takes schema-valid lines (its merge key needs ws_seq); the store's read refuses this one")
    sc, genesis = _genesis_scenario()
    result = replay(
        [genesis, event] if kind == "workspace" else [genesis],
        {} if kind == "workspace" else {case["log"]: [event]},
        verifier=CryptoVerifier(),
        now=sc["now"],
        expected_workspace_id=sc["workspace_id"],
    )
    assert result.chain_errors[0].code == case["replay"]


@pytest.mark.parametrize("case", V["schema_cases"], ids=lambda c: c["name"])
def test_restore_structure_is_refused_atomically_by_the_store(case, env):
    if not set(case["mutated"]) <= set(CALLER_FIELDS) or case["name"].startswith("signature_"):
        pytest.skip("host-stamped or signer-made fields: a caller cannot set them; schema, admit and replay cover it")
    event = case["event"]
    store = env.bootstrap()
    before = env.read_events("workspace")
    fields = {k: copy.deepcopy(event[k]) for k in CALLER_FIELDS if k in event}
    uid = "workspace" if case["log"] == "workspace" else env.new_ticket()
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
    if case.get("signer_revoked"):
        # Self-contained host case: its own setup, the refusal and its assertions, then return; nothing below applies.
        # the owner's own device is revoked, then the revocation is given up: only durable evidence still holds it
        kept = len(env.read_events("workspace"))
        env.revoke_device(env.owner.device, reason=case["signer_revoked"])
        store.abandon_tail("workspace", kept)
        facts = store.restore_facts("workspace")
        candidate = env.person_event(env.owner, "workspace", "restore", **facts, reason="rollback")
        before, held = env.read_events("workspace"), store._pins.held()
        with pytest.raises(StoreError) as exc:
            store.append(candidate, log="workspace")
        assert exc.value.code == case["expect"]
        assert env.read_events("workspace") == before and store._pins.held() == held
        assert not list((store._pins.dir / "transitions").glob("*.json"))
        return
    if case.get("missing") == "abandoned" or "fault" in case:
        store.abandon_tail("workspace", before_rev)
    facts = store.restore_facts("workspace")
    if case.get("learned"):
        facts["revocations"] = [{"revocation": revocation, **store.state.device_evidence(device)}]
    if "missing" in case:
        facts["revocations"] = []
    if case.get("self_revocation"):
        rev = certs.make_revocation(
            env.owner.pk_pub,
            env.owner.pk_sign,
            device_id_hex=env.owner.device[2:],
            revoked_ms=env.clock[0] * 1000,
            reason="retired",
        )
        own = {"revocation": rev, **store.state.device_evidence(env.owner.device)}
        facts["revocations"] = sorted([*facts["revocations"], own], key=lambda r: r["revocation"]["o"]["device_id"])
    candidate = env.person_event(env.owner, "workspace", "restore", **facts, reason="rollback")
    held = store._pins.held()
    if case.get("self_revocation"):
        before = env.read_events("workspace")
        with pytest.raises(StoreError) as exc:
            store.append(candidate, log="workspace")
        assert exc.value.code == case["expect"]
        assert env.read_events("workspace") == before and store._pins.held() == held
        return
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
    pair = (device, "p_" + revocation["o"]["person_id"])
    assert store.state.revocation_evidence()[pair]["revocation"] == revocation
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
    env.clock[0] -= 300
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


def test_missing_checkpoint_pin_uses_stored_checkpoint(env):
    store = env.bootstrap()
    env.add_device()
    held = store._pins.held()
    (store._pins.dir / "checkpoint.json").unlink()
    facts = store.restore_facts("workspace")
    assert facts["source"] == {"n": held["o"]["n"], "hash": canon.checkpoint_hash(held["o"])}
    candidate = env.person_event(env.owner, "workspace", "restore", **{**facts, "source": None}, reason="other")
    with pytest.raises(StoreError, match="source"):
        store.append(candidate, log="workspace")
    candidate = env.person_event(env.owner, "workspace", "restore", **facts, reason="other")
    store.append(candidate, log="workspace")


def test_restore_refuses_checkpoint_more_than_300_seconds_ahead(env):
    store = env.bootstrap()
    env.clock[0] += 301
    store.checkpoint()
    env.clock[0] -= 301
    before = env.read_events("workspace")
    held = store._pins.held()
    candidate = env.person_event(env.owner, "workspace", "restore", **store.restore_facts("workspace"), reason="other")
    with pytest.raises(StoreError, match="300 seconds ahead of the clock") as exc:
        store.append(candidate, log="workspace")
    assert exc.value.code == "validation.restore"
    assert env.read_events("workspace") == before and store._pins.held() == held


def test_restore_refuses_a_computed_at_more_than_300_seconds_ahead(env):
    """C2 at the host: the stamped restore `at` is max(clock, C.o.at, last at); a ticket event ahead of the clock moves
    it past the ceiling although C itself is within it."""
    store = env.bootstrap()
    uid = env.new_ticket()
    env.clock[0] += 400
    env.log(uid)
    env.clock[0] -= 400
    held = store._pins.held()
    assert _epoch(held["o"]["at"]) <= env.clock[0] + 300
    before = env.read_events("workspace")
    candidate = env.person_event(env.owner, "workspace", "restore", **store.restore_facts("workspace"), reason="other")
    with pytest.raises(StoreError, match="computed at") as exc:
        store.append(candidate, log="workspace")
    assert exc.value.code == "validation.restore"
    assert env.read_events("workspace") == before and store._pins.held() == held


@pytest.mark.parametrize("reason", ["lost", "retired", "compromised"])
def test_host_refuses_a_restore_that_revokes_its_own_signer(env, reason):
    """Receivers refuse such a bundle, so the host refuses it before the append instead of committing an
    unpublishable restore (§5.10); replay of an existing line keeps pre-event authorization (restore.json)."""
    store = env.bootstrap()
    facts = store.restore_facts("workspace")
    rev = certs.make_revocation(
        env.owner.pk_pub,
        env.owner.pk_sign,
        device_id_hex=env.owner.device[2:],
        revoked_ms=env.clock[0] * 1000,
        reason=reason,
    )
    facts["revocations"] = [{"revocation": rev, **store.state.device_evidence(env.owner.device)}]
    candidate = env.person_event(env.owner, "workspace", "restore", **facts, reason="other")
    before, held = env.read_events("workspace"), store._pins.held()
    with pytest.raises(StoreError, match="revokes its own signer") as exc:
        store.append(candidate, log="workspace")
    assert exc.value.code == "validation.restore"
    assert env.read_events("workspace") == before and store._pins.held() == held
    assert not list((store._pins.dir / "transitions").glob("*.json"))


def test_completed_restores_do_not_add_work_to_append(env, monkeypatch):
    import statistics
    import time

    import orch.store.store as sm

    store = env.bootstrap()
    uid = env.new_ticket()
    reads = []
    read = sm.read_or_none

    def tracked(path, *args):
        if path.parent.name == "transitions":
            reads.append(path)
        return read(path, *args)

    monkeypatch.setattr(sm, "read_or_none", tracked)

    def append_cost():
        samples = []
        for _ in range(7):
            start = time.process_time()
            env.log(uid)
            samples.append((time.process_time() - start) * 1000)
        return statistics.median(samples)

    before = append_cost()
    for _ in range(40):
        candidate = env.person_event(
            env.owner, "workspace", "restore", **store.restore_facts("workspace"), reason="other"
        )
        store.append(candidate, log="workspace")
    reads.clear()
    after = append_cost()
    assert not reads  # completed publication records never enter the append recovery scan
    assert after < before * 2 + 5, (before, after)
    assert len(list((store._pins.dir / "transitions").glob("*.json"))) == 40
    assert not list((store._pins.dir / "pending-transitions").glob("*.json"))


def test_transition_keeps_only_the_abandoned_suffix(env):
    store = env.bootstrap()
    uid = env.new_ticket()
    for _ in range(25):
        env.log(uid)
    store.checkpoint()
    old = store._logs[uid]
    expected = old.heads[-2:]
    acc_from = old.accs[-3]
    store.abandon_tail(uid, old.seq - 2)
    candidate = env.person_event(env.owner, uid, "restore", **store.restore_facts(uid), reason="rollback")
    store.append(candidate, log=uid)
    transition = json.loads(next((store._pins.dir / "transitions").glob("*.json")).read_text())
    assert "retained_heads" not in transition
    assert transition["acc_from"] == acc_from
    assert transition["abandoned_heads"] == expected
    assert transition["forward_heads"] == []
    from orch.store.checkpoints import ReceiverView, Restore, judge_offer

    receiver = ReceiverView(
        workspace_id=store.workspace_id,
        pinned_genesis=store.state.workspace.genesis,
        wsk_pub=env.wsk_pub,
        pinned_delegation=store._genesis_event["delegation"],  # the pin, never the bundle's own delegation
        now=transition["T"]["o"]["at"],  # the test clock stamped T
        held=transition["C"]["o"],
        known_devices={},
        known_revocations=(),
        local_revocations=(),
        learned_later=(),
    )
    bundle = Restore(transition=transition, proofs=transition["proofs"], applied=())
    assert judge_offer(receiver, transition["T"]["o"], restore=bundle).code == "ok"


@pytest.mark.parametrize("evidence", ["ticket", "transition", "workspace_log"])
def test_null_source_refused_when_workspace_checkpoint_files_are_lost(env, evidence):
    store = env.bootstrap()
    if evidence == "workspace_log":
        pass  # no ticket, no transition: the workspace log alone is the evidence
    elif evidence == "ticket":
        env.new_ticket()
    else:
        store.append(
            env.person_event(env.owner, "workspace", "restore", **store.restore_facts("workspace"), reason="other"),
            log="workspace",
        )
    (store._pins.dir / "checkpoint.json").unlink()
    store._checkpoints._workspace_path.unlink()
    with pytest.raises(StoreError) as exc:
        store.restore_facts("workspace")
    assert exc.value.code == "chain.diverged"
    candidate = env.person_event(env.owner, "workspace", "restore", **_facts_with_null_source(store), reason="other")
    before = env.read_events("workspace")
    with pytest.raises(StoreError) as exc:
        store.append(candidate, log="workspace")
    assert exc.value.code == "checkpoint.missing" and env.read_events("workspace") == before


def test_an_ordinary_append_does_not_rebaseline_after_lost_checkpoints(env):
    """Opus #377 finding 2: with both checkpoint files gone, an ordinary append must not write a fresh n=1 checkpoint
    that a restore could then name as its source."""
    store = env.bootstrap()
    high = store._pins.held()["o"]["n"]
    (store._pins.dir / "checkpoint.json").unlink()
    store._checkpoints._workspace_path.unlink()
    before = env.read_events("workspace")
    with pytest.raises(StoreError) as exc:
        store.append(env.grant_event(), log="workspace")
    assert exc.value.code == "checkpoint.missing" and env.read_events("workspace") == before
    assert store._pins.held() is None and store._checkpoints.workspace() is None
    with pytest.raises(StoreError):
        store.restore_facts("workspace")
    assert store._pins.high_n() == high


def test_a_host_without_checkpoint_evidence_starts_a_baseline(env, tmp_path):
    """A fresh clone or a lost host state dir: no checkpoint evidence, so the next append starts a baseline (F1 "What
    P1 can't detect"), and checkpoint() works."""
    import shutil

    store = env.bootstrap()
    store.close()
    shutil.rmtree(env.store_dir / ".state" / "checkpoints")
    env.host_state = tmp_path / "new-host-state"  # the host's state dir is gone
    store = env.open()
    store.append(env.grant_event(), log="workspace")
    cp = store._checkpoints.workspace()
    assert cp == store._pins.held() and cp["o"]["workspace_log"]["seq"] == store._logs["workspace"].seq
    assert cp["o"]["n"] <= 2  # a new baseline: n restarts on a host that never had one (opening wrote n=1)
    store.checkpoint()


@pytest.mark.parametrize("evidence", ["high_water", "ancestry_only"])
def test_a_host_with_checkpoint_evidence_refuses_without_a_checkpoint(env, evidence):
    store = env.bootstrap()
    store.close()
    pins = env.host_state / "hosts" / store.workspace_id
    (pins / "checkpoint.json").unlink()
    if evidence == "ancestry_only":
        (pins / "checkpoint-n").unlink()  # heads.sqlite rows from the earlier run remain
    (env.store_dir / ".state" / "checkpoints" / "workspace.json").unlink()
    store = env.open()
    before = env.read_events("workspace")
    with pytest.raises(StoreError) as exc:
        store.append(env.grant_event(), log="workspace")
    assert exc.value.code == "checkpoint.missing" and env.read_events("workspace") == before
    with pytest.raises(StoreError) as exc:
        store.checkpoint()
    assert exc.value.code == "checkpoint.missing"
    assert "no in-place repair" in exc.value.detail and "from a backup" in exc.value.detail
    assert "Do not delete the host state dir" in exc.value.detail and "must restore" not in exc.value.detail
    assert store._checkpoints.workspace() is None


def test_checkpoint_n_never_restarts_below_the_durable_high_water(env):
    store = env.bootstrap()
    high = store._pins.held()["o"]["n"]
    store._checkpoints._workspace_path.unlink()  # .state lost, the host pin survives
    store.append(env.grant_event(), log="workspace")
    assert store._checkpoints.workspace()["o"]["n"] == high + 1 == store._pins.held()["o"]["n"]


def _facts_with_null_source(store):
    info = store._logs["workspace"]
    return {
        "from_seq": info.seq,
        "head": info.head,
        "abandoned": None,
        "abandoned_decisions": [],
        "source": None,
        "revocations": [],
    }


def test_host_refuses_a_restore_signed_by_a_device_it_holds_revoked(env, tmp_path):
    """A WSK holder with a revoked device key rolls the log back past the revocation and signs a restore with that
    device: the kept chain authorizes it, the host's durable evidence does not (§5.10, C3)."""
    import shutil

    from tests.store.test_checkpoints import rollback

    store = env.bootstrap()
    store.close()
    backup = tmp_path / "backup"
    shutil.copytree(env.root, backup)
    store = env.open()
    env.revoke_device(env.owner.device, reason="compromised")
    store.close()
    rollback(env, backup)
    store = env.open()
    facts = store.restore_facts("workspace")
    assert [r["revocation"]["o"]["device_id"] for r in facts["revocations"]] == [env.owner.device[2:]]
    candidate = env.person_event(env.owner, "workspace", "restore", **facts, reason="rollback")
    before = env.read_events("workspace")
    with pytest.raises(StoreError) as exc:
        store.append(candidate, log="workspace")
    assert exc.value.code == "device.invalid"
    assert env.read_events("workspace") == before
    assert not list((store._pins.dir / "transitions").glob("*.json"))


def _committed_transition(env):
    store = env.bootstrap()
    store.append(
        env.person_event(env.owner, "workspace", "restore", **store.restore_facts("workspace"), reason="other"),
        log="workspace",
    )
    (done,) = (store._pins.dir / "transitions").glob("*.json")
    return store, json.loads(done.read_text())


def test_a_pending_transition_whose_slot_holds_another_head_is_dropped(env):
    store, transition = _committed_transition(env)
    transition["committed"] = False
    transition["event"]["reason"] = "rollback"  # same seq, another head: this restore can never commit
    pending = store._pins.dir / "pending-transitions" / (transition["event"]["id"] + ".json")
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_bytes(canon.cj_checked(transition) + b"\n")
    held = store._pins.held()
    store._recover_transitions()
    assert not pending.exists() and store._pins.held() == held
    env.log(env.new_ticket())  # appends go on


def test_a_pending_transition_the_held_checkpoint_passed_is_dropped(env):
    store, transition = _committed_transition(env)
    store.append(env.grant_event(), log="workspace")  # the held checkpoint moves past T
    assert store._pins.held()["o"]["n"] > transition["T"]["o"]["n"]
    transition["committed"] = False
    pending = store._pins.dir / "pending-transitions" / (transition["event"]["id"] + ".json")
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_bytes(canon.cj_checked(transition) + b"\n")
    held = store._pins.held()
    store._recover_transitions()
    assert not pending.exists() and store._pins.held() == held


def test_reload_skips_already_recorded_ancestry(env, monkeypatch):
    store = env.bootstrap()
    uid = env.new_ticket()
    for _ in range(50):
        env.log(uid)

    def unexpected(*args, **kw):
        pytest.fail("reload visited an individual event's ancestry")

    monkeypatch.setattr(store._pins, "note_head", unexpected)
    statements = []
    store._pins._db.set_trace_callback(statements.append)
    store._needs_reload = True
    store.refresh()
    assert sum(s.startswith("SELECT 1 FROM heads") for s in statements) == 2
    assert not any(s.startswith("INSERT") for s in statements)


def test_lazy_open_finishes_transition_interrupted_before_archive(env, monkeypatch):
    import orch.store.store as sm

    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid)
    store.checkpoint()
    store.abandon_tail(uid, 1)
    candidate = env.person_event(env.owner, uid, "restore", **store.restore_facts(uid), reason="rollback")
    replace = sm.os.replace

    def crash(source, target, **kw):
        if not kw and target.parent.name == "transitions":  # the host pin's archive (by path, outside the root)
            raise RuntimeError("before archive")
        return replace(source, target, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(sm.os, "replace", crash)
        with pytest.raises(RuntimeError, match="before archive"):
            store.append(candidate, log=uid)
    store.close()
    store = env.open(load="lazy")
    assert not list((store._pins.dir / "pending-transitions").glob("*.json"))
    assert store.append(candidate, log=uid).duplicate


def test_retained_fork_leaves_publication_incomplete(env):
    from orch import crypto
    from tests.store.helpers import WS

    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid, "original")
    store.checkpoint()
    store.close()
    path = env.path(uid, "events.jsonl")
    lines = path.read_bytes().splitlines(keepends=True)
    event = canon.parse_event_line(lines[-1])
    event["text"] = "fork"
    event["host_sig"] = crypto.b64u(env.signer.sign(canon.host_signing_bytes(WS, uid, event)))
    path.write_bytes(b"".join([*lines[:-1], canon.event_line(event)]))
    store = env.open()
    store.append(env.person_event(env.owner, uid, "restore", **store.restore_facts(uid), reason="diverged"), log=uid)
    transition = json.loads(next((store._pins.dir / "transitions").glob("*.json")).read_text())
    assert not transition["publication_complete"]


def test_malformed_checkpoint_pin_is_not_treated_as_missing(env):
    store = env.bootstrap()
    (store._pins.dir / "checkpoint.json").write_bytes(b"{}")
    with pytest.raises(StoreError, match="invalid held workspace checkpoint"):
        store.restore_facts("workspace")


# --- #441: a cut tail holding convenience events sets the durable mark first ------------------------------------------


def _tail_with(env, uid, line: bytes):
    path = env.store_dir / "tickets" / uid / "events.jsonl"
    with open(path, "ab") as fh:
        fh.write(line)
    return path


def test_abandon_tail_of_convenience_events_sets_the_mark_before_the_cut(env):
    from orch.ops.readers import has_convenience_events, seen_path

    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid)
    _tail_with(env, uid, b'{"auth":"convenience","type":"log.added","v":2}\n')
    assert not seen_path(env.host_state, WS).exists()
    store.abandon_tail(uid, 1)
    assert seen_path(env.host_state, WS).exists()
    assert has_convenience_events(store)  # the cut tail in .state/abandoned still counts


def test_abandon_tail_without_convenience_or_with_a_torn_line_sets_no_mark(env):
    from orch.ops.readers import seen_path

    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid)
    _tail_with(env, uid, b'{"type":"log.added","v":2,"au')  # a torn line that does not name the value
    store.abandon_tail(uid, 1)
    assert not seen_path(env.host_state, WS).exists()


def test_abandon_tail_refuses_when_the_mark_cannot_be_written(env, monkeypatch):
    from orch.ops import readers

    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid)
    path = _tail_with(env, uid, b'{"auth":"convenience","type":"log.added","v":2}\n')
    before = path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("no space left")

    monkeypatch.setattr(readers, "ensure_convenience_mark", boom)
    with pytest.raises(StoreError) as info:
        store.abandon_tail(uid, 1)
    assert "convenience_seen" in str(info.value)
    assert path.read_bytes() == before  # nothing was cut


@pytest.mark.parametrize(
    "line",
    [b'{"\\u0061uth":"convenience"}\n', b'{"auth": "convenience"}\n', b'{"v":2,"auth":"convenience"}\n',
     b'{"auth":"convenience"'],
)  # fmt: skip
def test_only_a_canonical_event_line_in_a_cut_tail_counts(env, line):
    """Opus #454 L4: escaped, spaced, unsorted or torn bytes are no event any reader accepted: no mark."""
    from orch.ops.readers import has_convenience_events, seen_path

    store = env.bootstrap()
    uid = env.new_ticket()
    env.log(uid)
    _tail_with(env, uid, line)
    store.abandon_tail(uid, 1)
    assert not seen_path(env.host_state, WS).exists()
    assert not has_convenience_events(store)


def test_a_legacy_cut_tail_counts_as_convenience_history(env):
    from orch.ops.readers import has_convenience_events

    store = env.bootstrap()
    ab = env.store_dir / ".state" / "abandoned"
    ab.mkdir(parents=True, exist_ok=True)
    (ab / "workspace-9.jsonl").write_bytes(b'{"auth":"passphrase"}\n')
    assert not has_convenience_events(store)
    (ab / "workspace-10.jsonl").write_bytes(b'{"auth": "convenience"}\n{"auth":"convenience"')  # not canonical, torn
    assert not has_convenience_events(store)
    (ab / "workspace-12.jsonl").write_bytes(b'{"auth":"convenience","type":"log.added"}\n')
    assert has_convenience_events(store)


def test_a_bare_cr_is_no_line_boundary_in_a_cut_tail(monkeypatch):
    """Codex #454 delta 2: ``junk\\r{...}\\n`` is ONE line (LF only), which no reader accepts: no mark."""
    import json

    from orch import canon
    from orch.ops.readers import canonical_lines_have_convenience

    seen: list[bytes] = []

    def parse(line: bytes) -> dict:
        seen.append(line)
        if not line.endswith(b"\n") or not line.startswith(b"{"):
            raise ValueError("not an event line")
        return json.loads(line)

    monkeypatch.setattr(canon, "parse_event_line", parse)
    assert not canonical_lines_have_convenience(b'junk\r{"auth":"convenience"}\n')
    assert seen == [b'junk\r{"auth":"convenience"}\n']
    assert canonical_lines_have_convenience(b'{"auth":"x"}\n{"auth":"convenience"}\n')
    assert not canonical_lines_have_convenience(b'{"auth":"convenience"}')  # a torn last line keeps no LF

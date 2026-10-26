"""H2b: classification, the outcome store, reconciliation, tombstones, the M1 digest rules, job completion repeats."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import threading

import pytest

from orch.host.errors import HostError
from orch.host.intents import KEEP_SECONDS, TOMBSTONE_SECONDS
from orch.host.jobs import CompletionStore, completion_digest
from orch.host.outcomes import (
    OutcomeStore,
    Planned,
    classify,
    dry_run_supported,
    principal_of,
    request_idem,
    result_frame,
)
from orch.host.workspace import ok_result

UID = os.getuid()
WS = "a" * 32
P = "uid:1"
IDEM = "a_01J9ZP0000000000000000000A"
IDEM2 = "a_01J9ZP0000000000000000000B"
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


class FakeLog:
    """The verified log as a reconciler sees it: ``(log, id) -> event``."""

    def __init__(self) -> None:
        self.events: dict[tuple[str, str], dict] = {}
        self.broken = False

    @contextlib.contextmanager
    def locked(self):
        yield

    def land(self, log, event_id, seq=1, etype="x"):
        self.events[(log, event_id)] = {"id": event_id, "seq": seq, "type": etype}

    def find(self, log, event_id):
        if self.broken:
            raise HostError("workspace.unavailable", "no store")
        return self.events.get((log, event_id))

    def rebuild(self, record, events):
        return ok_result({"rebuilt": [e["id"] for e in events]}, seq=events[-1]["seq"])


@pytest.fixture
def env(tmp_path):
    now = [1_800_000_000.0]
    (tmp_path / "state").mkdir(mode=0o700)
    log = FakeLog()
    store = OutcomeStore(tmp_path / "state", WS, UID, clock=lambda: now[0], reconciler=log)
    store.open()
    return store, log, now


def pl(log, event_id):
    return Planned(log, event_id, "x")


def begin(store, idem=IDEM, op="new", args=None, **kw):
    kw.setdefault("planned", [pl("L", "default")])
    return store.begin(P, idem, op, args if args is not None else {"title": "x"}, **kw)


# --- classification and dry_run -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "category"),
    [
        ("new", "log"),  # a registry op whose who is not read
        ("claim", "log"),
        ("apply", "log"),
        ("human.submit", "log"),
        ("show", "read"),  # a registry read
        ("doctor", "read"),
        ("human.prepare", "read"),
        ("human.get", "read"),
        ("human.cancel", "read"),
        ("blob.put", "read"),
        ("sub.open", "read"),
        ("outcome.get", "read"),
        ("host.outcome", "read"),
        ("job.next", "read"),
        ("host.list", "read"),
        ("host.register", "control"),
        ("host.unregister", "control"),
        ("host.bootstrap", "control"),
        ("job.done", "job"),
        ("host.wsk_create", "create"),
    ],
)
def test_classification(op, category):
    assert classify(op) == category


def test_every_registry_op_is_classified_by_its_who():
    from orch import ops

    for op in ops.all():
        want = "read" if op.declaration["who"] == "read" else "log"
        assert classify(op.name) == want, op.name


def test_an_op_nobody_serves_is_unknown_command():
    with pytest.raises(HostError) as exc:
        classify("no.such.op")
    assert exc.value.code == "unknown_command"


def test_dry_run_is_accepted_only_by_log_writes_and_human_prepare():
    for op in ("new", "claim", "human.prepare"):
        assert dry_run_supported(op)
    for op in ("show", "human.submit", "host.register", "job.done", "blob.put", "sub.open", "outcome.get"):
        assert not dry_run_supported(op)
        with pytest.raises(HostError) as exc:
            request_idem(op, {"dry_run": True})
        assert exc.value.code == "invalid.input" and "not supported" in exc.value.message


def test_a_dry_run_ignores_the_idem_and_a_write_needs_one():
    assert request_idem("new", {"dry_run": True}) is None  # not required
    assert request_idem("new", {"dry_run": True, "idem": IDEM}) is None  # not looked up, not stored
    assert request_idem("new", {"idem": IDEM}) == IDEM
    assert request_idem("show", {"idem": IDEM}) is None  # ignored on a read
    assert request_idem("host.list", None) is None
    for op in ("new", "human.submit", "host.register"):
        with pytest.raises(HostError) as exc:
            request_idem(op, {})
        assert exc.value.code == "idem.required"


def test_principal_is_the_verified_grant_or_the_uid():
    grant = "gr_01J9ZP0000000000000000000Z." + "A" * 43
    assert principal_of(501, "G", grant) == "g:gr_01J9ZP0000000000000000000Z@501"
    assert principal_of(501, "N", grant) == "uid:501"  # a grant that did not verify never makes a principal
    assert principal_of(501, "P") == "uid:501"


# --- the store: states, binding, replay -----------------------------------------------------------------------------


def test_begin_commit_and_replay_with_duplicate_true(env):
    store, log, now = env
    intent = begin(store, planned=[pl("L", "e1")])
    path = store.dir / intent.key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = json.loads(path.read_text())
    assert record["step"] == "pending" and record["planned"] == [
        {"log": "L", "id": "e1", "type": "x"}
    ]  # before any append
    assert store.lookup(P, IDEM) == {"state": "pending"}
    done = store.commit(intent, ok_result({"n": 1}, seq=4))
    assert done["step"] == "committed"
    again = begin(store, planned=[pl("L", "e2")])
    assert again["duplicate"] is True and again["data"] == {"n": 1} and again["seq"] == 4
    assert store.lookup(P, IDEM) == {"state": "committed", "result": ok_result({"n": 1}, seq=4), "exit": 0}


def test_a_record_stores_a_result_never_a_frame_and_a_replay_takes_the_retrys_request_id(env):
    store, _, _ = env
    store.commit(begin(store), ok_result({"n": 1}))
    raw = (store.dir / store.key(WS, P, IDEM)).read_text()
    assert '"t":"res"' not in raw and '"ok"' not in raw
    replay = begin(store)
    frame = result_frame("retry-7", replay)
    assert frame["id"] == "retry-7" and frame["ok"] is True and frame["result"]["duplicate"] is True
    assert result_frame("first-1", ok_result({"n": 1}))["id"] == "first-1"


def test_a_nonretryable_refusal_is_stored_and_a_retryable_one_runs_again(env):
    store, _, _ = env
    store.refuse(begin(store), HostError("invalid.input", "nope"))
    with pytest.raises(HostError) as exc:
        begin(store)
    assert exc.value.code == "invalid.input" and exc.value.message == "nope"
    assert store.lookup(P, IDEM)["state"] == "failed"
    assert store.refuse(begin(store, IDEM2), HostError("lock.busy", "busy")) is None  # retryable: nothing stored
    assert not (store.dir / store.key(WS, P, IDEM2)).exists()
    intent = begin(store, IDEM2)  # runs again
    store.commit(intent, ok_result({}))


def test_idem_conflict_on_op_args_files_or_session(env):
    store, _, _ = env
    store.commit(begin(store, files={"f": SHA_A}, session="s1"), ok_result({}))
    for change in (
        {"op": "other"},
        {"args": {"title": "y"}},
        {"files": {"f": SHA_B}},
        {"files": {}},
        {"session": "s2"},
    ):
        kw = {"files": {"f": SHA_A}, "session": "s1", **change}
        op, args = kw.pop("op", "new"), kw.pop("args", None)
        with pytest.raises(HostError) as exc:
            begin(store, op=op, args=args, **kw)
        assert exc.value.code == "idem.conflict", change
    assert begin(store, files={"f": SHA_A}, session="s1")["duplicate"] is True


def test_concurrent_duplicates_wait_for_the_first_call_and_get_one_result(env):
    store, _, _ = env
    applied, out = [], []
    started = threading.Event()
    release = threading.Event()

    def call():
        def apply(pending):
            applied.append(1)
            started.set()
            release.wait(10)
            return ok_result({"n": len(applied)})

        out.append(store.execute(P, IDEM, "new", {"title": "x"}, apply, planned=[pl("L", "e")]))

    first = threading.Thread(target=call)
    first.start()
    assert started.wait(5)
    others = [threading.Thread(target=call) for _ in range(3)]
    for t in others:
        t.start()
    assert store.lookup(P, IDEM) == {"state": "pending"}
    release.set()
    for t in [first, *others]:
        t.join(10)
    assert len(applied) == 1  # one append
    assert sorted(r["duplicate"] for r in out) == [False, True, True, True]
    assert {r["data"]["n"] for r in out} == {1}


def test_a_failed_waiter_target_lets_the_next_duplicate_run(env):
    store, _, _ = env

    def boom(pending):
        raise HostError("lock.busy", "busy")  # retryable: stored nothing

    with pytest.raises(HostError):
        store.execute(P, IDEM, "new", {"title": "x"}, boom, planned=[pl("L", "e")])
    assert store.execute(P, IDEM, "new", {"title": "x"}, lambda p: ok_result({"ok": 1}), planned=[pl("L", "e2")])[
        "data"
    ] == {"ok": 1}


# --- outcome.get scoping --------------------------------------------------------------------------------------------


def test_lookup_is_scoped_to_the_principal(env):
    store, _, _ = env
    store.commit(begin(store), ok_result({}))
    with pytest.raises(HostError) as exc:
        store.lookup("uid:2", IDEM)
    assert exc.value.code == "outcome.unknown_idem"
    with pytest.raises(HostError) as exc:
        store.lookup(P, IDEM2)
    assert exc.value.code == "outcome.unknown_idem"
    # the same idem under another principal is its own record, not a conflict
    other = store.begin("uid:2", IDEM, "new", {"title": "other"}, planned=[pl("L", "o")])
    assert not isinstance(other, dict)


# --- reconciliation -------------------------------------------------------------------------------------------------


def crash_after_pending(store, **kw):
    intent = begin(store, **kw)
    store.release(intent)  # the process died: the record stays pending, nothing runs it
    return intent


def test_reconcile_all_landed_rebuilds_the_result_from_the_log(env):
    store, log, _ = env
    crash_after_pending(store, planned=[pl("L", "e1")])
    log.land("L", "e1", seq=7)
    assert store.reconcile_all() == [("new", "committed")]
    assert store.lookup(P, IDEM)["result"]["data"] == {"rebuilt": ["e1"]}
    replay = begin(store)
    assert replay["duplicate"] is True and replay["seq"] == 7


def test_a_duplicate_reconciles_a_stale_pending_record_at_its_next_lookup(env):
    store, log, _ = env
    crash_after_pending(store, planned=[pl("L", "e1")])
    log.land("L", "e1", seq=3)
    replay = begin(store)  # no reconcile_all ran: the retry itself reconciles
    assert replay["duplicate"] is True and replay["data"] == {"rebuilt": ["e1"]}


def test_lookup_reconciles_a_stale_pending_record(env):
    store, log, _ = env
    crash_after_pending(store, planned=[pl("L", "e1")])
    log.land("L", "e1")
    assert store.lookup(P, IDEM)["state"] == "committed"


def test_some_landed_is_partial_and_a_retry_gets_the_interrupted_error(env):
    store, log, _ = env
    crash_after_pending(store, op="apply", planned=[pl("L", "e1"), pl("L", "e2")])
    log.land("L", "e1", seq=5)
    assert store.reconcile_all() == [("apply", "partial")]
    out = store.lookup(P, IDEM)
    assert out["state"] == "partial" and out["error"]["code"] == "invalid.input"
    assert "interrupted after 1 event(s)" in out["error"]["message"]
    assert json.loads((store.dir / store.key(WS, P, IDEM)).read_text())["seqs"] == [5]
    with pytest.raises(HostError, match="interrupted after 1 event"):
        begin(store, op="apply")


def test_human_submit_is_never_partial_it_stays_pending_for_the_roll_forward(env):
    store, log, _ = env
    crash_after_pending(store, op="human.submit", planned=[pl("L", "e1"), pl("L", "e2")])
    log.land("L", "e1")
    assert store.reconcile_all() == [("human.submit", "pending")]
    assert store.lookup(P, IDEM) == {"state": "pending"}
    with pytest.raises(HostError) as exc:
        begin(store, op="human.submit")
    assert exc.value.code == "outcome.unknown" and exc.value.retryable


def test_none_landed_removes_the_record_and_the_retry_runs_again(env):
    store, _, _ = env
    intent = crash_after_pending(store, planned=[pl("L", "e1")])
    assert store.reconcile_all() == [("new", "removed")]
    assert not (store.dir / intent.key).exists()
    with pytest.raises(HostError, match="outcome.unknown_idem"):
        store.lookup(P, IDEM)
    again = begin(store, planned=[pl("L", "e9")])
    assert not isinstance(again, dict)  # it runs again, with fresh planned ids


def test_an_unreadable_log_leaves_the_record_pending(env):
    store, log, _ = env
    crash_after_pending(store, planned=[pl("L", "e1")])
    log.broken = True
    assert store.reconcile_all() == [("new", "pending")]
    with pytest.raises(HostError) as exc:
        begin(store)
    assert exc.value.code == "outcome.unknown"
    log.broken = False
    assert store.reconcile_all() == [("new", "removed")]


def test_a_running_call_is_not_reconciled_away(env):
    store, log, _ = env
    intent = begin(store, planned=[pl("L", "e1")])  # running in this process
    assert store.reconcile_all() == []
    assert store.lookup(P, IDEM) == {"state": "pending"}
    store.commit(intent, ok_result({}))


# --- tombstones and retention ---------------------------------------------------------------------------------------


def test_records_become_tombstones_after_24h_then_go_after_7_days(env):
    store, _, now = env
    store.commit(begin(store), ok_result({"n": 1}))
    store.refuse(begin(store, IDEM2), HostError("invalid.input", "no"))
    now[0] += KEEP_SECONDS + 1
    store.sweep()
    for idem in (IDEM, IDEM2):
        with pytest.raises(HostError) as exc:
            store.lookup(P, idem)  # outcome.get on a tombstone
        assert exc.value.code == "idem.expired" and exc.value.exit == 2
    with pytest.raises(HostError) as exc:
        begin(store)  # never a second append
    assert exc.value.code == "idem.expired"
    with pytest.raises(HostError) as exc:
        begin(store, args={"title": "other"})
    assert exc.value.code == "idem.conflict"  # the tombstone keeps its binding
    tomb = json.loads((store.dir / store.key(WS, P, IDEM)).read_text())
    assert set(tomb) >= {"key", "binding", "state"} and "result" not in tomb and tomb["state"] == "committed"
    now[0] += TOMBSTONE_SECONDS + 1
    store.sweep()
    assert os.listdir(store.dir) == []
    with pytest.raises(HostError, match="outcome.unknown_idem"):
        store.lookup(P, IDEM)


def test_a_pending_record_is_never_swept(env):
    store, _, now = env
    crash_after_pending(store, planned=[pl("L", "e1")])
    now[0] += 10 * KEEP_SECONDS
    store.sweep()
    assert json.loads((store.dir / store.key(WS, P, IDEM)).read_text())["step"] == "pending"


def test_the_record_limit_tombstones_the_oldest_completed_records_early(tmp_path):
    now = [1_800_000_000.0]
    (tmp_path / "state").mkdir(mode=0o700)
    store = OutcomeStore(tmp_path / "state", WS, UID, clock=lambda: now[0], max_records=5)
    store.open()
    idems = [f"a_01J9ZP00000000000000000{i:02d}" for i in range(7)]
    for idem in idems[:5]:
        now[0] += 1
        store.commit(begin(store, idem), ok_result({}))
    assert store.count(P) == 5
    store.commit(begin(store, idems[5]), ok_result({}))  # over the limit: the oldest become tombstones
    with pytest.raises(HostError, match="idem.expired"):
        store.lookup(P, idems[0])
    assert store.lookup(P, idems[5])["state"] == "committed"
    assert store.count(P) <= 5
    other = store.begin("uid:9", idems[0], "new", {"title": "x"}, planned=[pl("L", "o")])  # the limit is per principal
    assert not isinstance(other, dict)


def test_at_the_limit_with_nothing_completed_is_quota_host(tmp_path):
    (tmp_path / "state").mkdir(mode=0o700)
    store = OutcomeStore(tmp_path / "state", WS, UID, max_records=2)
    store.open()
    begin(store, IDEM)
    begin(store, IDEM2)  # both running
    with pytest.raises(HostError) as exc:
        begin(store, "a_01J9ZP0000000000000000000C")
    assert exc.value.code == "quota.host"


def test_counts_survive_a_restart(env, tmp_path):
    store, _, now = env
    store.commit(begin(store), ok_result({}))
    fresh = OutcomeStore(tmp_path / "state", WS, UID, clock=lambda: now[0])
    fresh.open()
    assert fresh.count(P) == 1


# --- files, M1 ------------------------------------------------------------------------------------------------------


def test_bind_files_hashes_inline_bytes_and_asks_the_blob_store_for_blobs(env):
    import base64
    import hashlib

    store, _, _ = env
    inline = base64.b64encode(b"hello").decode()
    digests, by_digest = store.bind_files(
        P, IDEM, {"a": {"b64": inline}, "b": {"blob": "b_X"}}, blob_digest=lambda blob: SHA_B if blob == "b_X" else None
    )
    assert digests == {"a": "sha256:" + hashlib.sha256(b"hello").hexdigest(), "b": SHA_B} and by_digest == frozenset()
    for bad in ({"b64": "***"}, {"blob": "b_unknown"}, {"b64": "aa", "blob": "b_X"}, {}):
        with pytest.raises(HostError) as exc:
            store.bind_files(P, IDEM, {"a": bad}, blob_digest=lambda blob: SHA_B if blob == "b_X" else None)
        assert exc.value.code == "blob.invalid"


def test_digest_without_a_record_is_blob_invalid(env):
    store, _, _ = env
    for idem in (IDEM, None):
        with pytest.raises(HostError) as exc:
            store.bind_files(P, idem, {"f": {"digest": SHA_A}})
        assert exc.value.code == "blob.invalid"
    with pytest.raises(HostError) as exc:
        begin(store, files={"f": SHA_A}, digest_args=frozenset({"f"}))  # begin refuses it too
    assert exc.value.code == "blob.invalid"
    assert os.listdir(store.dir) == []  # and wrote nothing


def test_a_retry_after_the_blob_was_consumed_binds_through_the_digest(env):
    store, _, _ = env
    lookups = []
    first_digests, _ = store.bind_files(
        P, IDEM, {"f": {"blob": "b_X"}}, blob_digest=lambda b: (lookups.append(b), SHA_A)[1]
    )
    store.commit(begin(store, files=first_digests), ok_result({"n": 1}))  # the commit consumed the blob
    digests, by_digest = store.bind_files(P, IDEM, {"f": {"digest": SHA_A}}, blob_digest=lambda b: None)
    assert by_digest == {"f"}
    replay = begin(store, files=digests, digest_args=by_digest)
    assert replay["duplicate"] is True and lookups == ["b_X"]  # the digest was never resolved to bytes
    wrong, _ = store.bind_files(P, IDEM, {"f": {"digest": SHA_B}})
    with pytest.raises(HostError) as exc:
        begin(store, files=wrong, digest_args=frozenset({"f"}))
    assert exc.value.code == "idem.conflict"  # compared with the stored binding only


def test_a_digest_retry_that_has_to_run_again_is_blob_invalid_even_if_a_blob_has_that_digest(env):
    store, _, _ = env
    known = {"b_Y": SHA_A}  # a blob (or artifact) with exactly this digest exists
    calls = []

    def blob_digest(blob):
        calls.append(blob)
        return known.get(blob)

    files, _ = store.bind_files(P, IDEM, {"f": {"blob": "b_Y"}}, blob_digest=blob_digest)
    crash_after_pending(store, files=files, planned=[pl("L", "e1")])  # died before the append
    digests, by_digest = store.bind_files(P, IDEM, {"f": {"digest": SHA_A}}, blob_digest=blob_digest)
    with pytest.raises(HostError) as exc:
        begin(store, files=digests, digest_args=by_digest)  # reconciliation removes the record: it must run again
    assert exc.value.code == "blob.invalid"
    assert calls == ["b_Y"]  # {digest} never made a lookup
    with pytest.raises(HostError) as exc:  # and the record is gone, so the digest has nothing to bind to
        store.bind_files(P, IDEM, {"f": {"digest": SHA_A}}, blob_digest=blob_digest)
    assert exc.value.code == "blob.invalid"
    # uploading again (a blob spec) runs it
    again, _ = store.bind_files(P, IDEM, {"f": {"blob": "b_Y"}}, blob_digest=blob_digest)
    assert not isinstance(begin(store, files=again, planned=[pl("L", "e2")]), dict)


def test_a_malformed_digest_is_blob_invalid(env):
    store, _, _ = env
    store.commit(begin(store), ok_result({}))
    for digest in ("sha256:xyz", "md5:" + "a" * 32, SHA_A.upper(), 5):
        with pytest.raises(HostError) as exc:
            store.bind_files(P, IDEM, {"f": {"digest": digest}})
        assert exc.value.code == "blob.invalid"


# --- job.done -------------------------------------------------------------------------------------------------------


JOB = "01J9ZP0000000000000000000J"


@pytest.fixture
def jobs(tmp_path):
    (tmp_path / "state").mkdir(mode=0o700)
    now = [1_800_000_000.0]
    store = CompletionStore(tmp_path / "state", UID, clock=lambda: now[0])
    store.open()
    return store, now


def test_job_done_repeated_for_a_completed_job_answers_empty(jobs, tmp_path):
    store, now = jobs
    conn, other = object(), object()
    digest = completion_digest("ok", {"changed": []})
    store.lease(JOB, WS, "tree.check", "L" * 26, conn)
    assert store.done(JOB, "L" * 26, "ok", digest, conn) == {}
    assert store.done(JOB, "L" * 26, "ok", digest, conn) == {}  # a repeat
    assert store.done(JOB, "L" * 26, "ok", digest, other) == {}  # from any worker connection
    fresh = CompletionStore(tmp_path / "state", UID, clock=lambda: now[0])  # and across a host restart
    fresh.open()
    assert fresh.done(JOB, "L" * 26, "ok", digest, object()) == {}
    for lease, dig in (("M" * 26, digest), ("L" * 26, completion_digest("ok", {"changed": [1]}))):
        with pytest.raises(HostError) as exc:
            fresh.done(JOB, lease, "ok", dig, conn)
        assert exc.value.code == "worker.lease"
    now[0] += KEEP_SECONDS + 1
    fresh.sweep()
    with pytest.raises(HostError, match="worker.lease"):
        fresh.done(JOB, "L" * 26, "ok", digest, conn)


def test_job_done_precedence_unknown_leased_void(jobs):
    store, _ = jobs
    conn, other = object(), object()
    digest = completion_digest("error", error={"code": "cancelled", "message": "m"})
    with pytest.raises(HostError, match="worker.lease"):
        store.done(JOB, "L" * 26, "error", digest, conn)  # 1. unknown job
    with pytest.raises(HostError, match="worker.lease"):
        store.done("not-an-id", "L" * 26, "error", digest, conn)
    store.lease(JOB, WS, "addon.call", "L" * 26, conn)
    with pytest.raises(HostError, match="worker.lease"):
        store.done(JOB, "L" * 26, "error", digest, other)  # 3. another connection
    with pytest.raises(HostError, match="worker.lease"):
        store.done(JOB, "X" * 26, "error", digest, conn)  # 3. another lease
    assert store.void(conn) == [JOB]
    with pytest.raises(HostError, match="worker.lease"):
        store.done(JOB, "L" * 26, "error", digest, conn)  # 4. void
    # the message is part of the digest
    assert digest != completion_digest("error", error={"code": "cancelled", "message": "other"})


# --- fix round: a refusal after an append, store codes, leaked keys, the index, reconciliation chec -----------------


def landing(log, *ids, then=None, etype="x"):
    """An ``apply`` that appends these events (they appear in the fake log) and then raises ``then``."""

    def apply(pending):
        for i, event_id in enumerate(ids, 1):
            log.land("L", event_id, seq=i, etype=etype)
        if then is not None:
            raise then
        return ok_result({"done": True})

    return apply


def run(store, apply, *ids, op="new", idem=IDEM):
    return store.execute(P, idem, op, {"title": "x"}, apply, planned=[pl("L", i) for i in ids])


def test_a_retryable_error_after_the_append_does_not_delete_the_record_and_never_appends_twice(env):
    store, log, _ = env
    applied = []

    def apply(pending):
        applied.append(1)
        return landing(log, "e1", then=HostError("lock.busy", "busy"))(pending)

    result = store.execute(P, IDEM, "new", {"title": "x"}, apply, planned=[pl("L", "e1")])
    assert result["data"] == {"rebuilt": ["e1"]}  # the log says it landed: committed, the rebuilt result
    assert store.lookup(P, IDEM)["state"] == "committed"
    again = store.execute(P, IDEM, "new", {"title": "x"}, apply, planned=[pl("L", "e2")])
    assert again["duplicate"] is True and applied == [1]  # the retry did not append again


def test_a_nonretryable_error_after_the_first_of_two_events_is_partial_not_failed(env):
    store, log, _ = env
    with pytest.raises(HostError, match="interrupted after 1 event"):
        run(store, landing(log, "e1", then=HostError("invalid.input", "no")), "e1", "e2", op="apply")
    out = store.lookup(P, IDEM)
    assert out["state"] == "partial" and json.loads((store.dir / store.key(WS, P, IDEM)).read_text())["seqs"] == [1]
    with pytest.raises(HostError, match="interrupted after 1 event"):
        run(store, landing(log), "e3", "e4", op="apply")


def test_a_refusal_after_the_first_event_of_a_human_submit_stays_pending(env):
    store, log, _ = env
    with pytest.raises(HostError, match="invalid.input"):
        run(store, landing(log, "e1", then=HostError("invalid.input", "no")), "e1", "e2", op="human.submit")
    assert store.lookup(P, IDEM) == {"state": "pending"}


def test_a_refusal_with_nothing_landed_is_stored_or_forgotten_as_before(env):
    store, log, _ = env
    with pytest.raises(HostError):
        run(store, landing(log, then=HostError("invalid.input", "no")), "e1")
    assert store.lookup(P, IDEM)["state"] == "failed"
    with pytest.raises(HostError):
        run(store, landing(log, then=HostError("lock.busy", "busy")), "e2", idem=IDEM2)
    with pytest.raises(HostError, match="outcome.unknown_idem"):
        store.lookup(P, IDEM2)


def test_an_unreadable_log_after_a_refusal_keeps_the_record_pending(env):
    store, log, _ = env

    def apply(pending):
        log.land("L", "e1")
        log.broken = True
        raise HostError("lock.busy", "busy")

    with pytest.raises(HostError, match="lock.busy"):
        run(store, apply, "e1")
    log.broken = False
    assert store.lookup(P, IDEM)["state"] == "committed"  # the next lookup reconciles it


def test_store_busy_is_retryable_and_every_store_code_keeps_its_cli_disposition(env):
    from orch.cli.store_errors import TABLE
    from orch.host.errors import disposition
    from orch.ops.errors import ERRORS
    from orch.store import StoreError

    assert disposition("store.busy") == (9, True)
    for code, mapped in TABLE.items():
        spec = ERRORS.get(mapped)
        if spec is not None:
            assert disposition(code)[1] == spec.retryable, code
    store, log, _ = env

    def apply(pending):
        raise StoreError("store.busy", "adopting")

    with pytest.raises(HostError) as exc:
        run(store, apply, "e1")
    assert exc.value.code == "store.busy" and exc.value.retryable
    with pytest.raises(HostError, match="outcome.unknown_idem"):
        store.lookup(P, IDEM)  # not stored as a permanent failure


def test_a_failed_record_write_does_not_leak_the_running_key(env, monkeypatch):
    store, log, _ = env
    intent = begin(store, planned=[pl("L", "e1")])
    real = store._write

    def broken(key, record):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_write", broken)
    with pytest.raises(OSError):
        store.commit(intent, ok_result({}))
    monkeypatch.setattr(store, "_write", real)
    log.land("L", "e1")
    assert begin(store, planned=[pl("L", "e9")])["duplicate"] is True  # reconciled from the log, no hang
    intent2 = begin(store, IDEM2, planned=[pl("L", "f1")])
    monkeypatch.setattr(store.__class__, "_path", lambda self, key: (_ for _ in ()).throw(PermissionError("x")))
    with pytest.raises(PermissionError):
        store.abandon(intent2)
    monkeypatch.undo()
    assert not store._running  # released although the unlink failed
    with pytest.raises(HostError, match="outcome.unknown_idem"):
        store.lookup(P, IDEM2)  # nothing landed: reconciliation removes the stale record


def test_a_duplicate_of_a_call_that_never_finishes_waits_a_bounded_time(tmp_path):
    (tmp_path / "state").mkdir(mode=0o700)
    store = OutcomeStore(tmp_path / "state", WS, UID, wait_seconds=0.2)
    store.open()
    begin(store)  # running, and nobody will ever finish it
    with pytest.raises(HostError) as exc:
        begin(store)
    assert exc.value.code == "outcome.unknown" and exc.value.retryable


def test_lookup_of_a_foreign_record_leaks_no_running_key(env):
    store, log, _ = env
    key = store.key(WS, P, IDEM)
    foreign = {"idem": IDEM, "namespace": WS, "principal": "uid:2", "binding": "b", "op": "new", "step": "pending"}
    (store.dir / key).write_text(json.dumps(foreign))
    os.chmod(store.dir / key, 0o600)
    with pytest.raises(HostError, match="outcome.unknown_idem"):
        store.lookup(P, IDEM)
    assert not store._running
    with pytest.raises(HostError, match="internal"):
        begin(store)
    assert not store._running


def test_reconciliation_requires_the_planned_type_and_actor(env):
    store, log, _ = env
    crash_after_pending(store, planned=[Planned("L", "e1", "ticket.created", {"kind": "agent", "id": "a"})])
    log.land("L", "e1", etype="note.added")  # another event that carries the planned id
    assert store.reconcile_all() == [("new", "removed")]
    crash_after_pending(store, planned=[Planned("L", "e2", "x", {"kind": "agent", "id": "a"})])
    log.events[("L", "e2")] = {"id": "e2", "seq": 1, "type": "x", "actor": {"kind": "agent", "id": "other"}}
    assert store.reconcile_all() == [("new", "removed")]
    crash_after_pending(store, planned=[Planned("L", "e3", "x", {"kind": "agent", "id": "a"})])
    log.events[("L", "e3")] = {"id": "e3", "seq": 1, "type": "x", "actor": {"kind": "agent", "id": "a"}}
    assert store.reconcile_all() == [("new", "committed")]


def test_a_result_builder_bug_marks_the_record_failed_instead_of_leaving_it_pending(env, caplog):
    store, log, _ = env
    crash_after_pending(store, planned=[pl("L", "e1")])
    log.land("L", "e1")

    def boom(record, events):
        raise KeyError("key")

    log.rebuild = boom
    assert store.reconcile_all() == [("new", "failed")]
    out = store.lookup(P, IDEM)
    assert out["state"] == "failed" and out["error"]["code"] == "internal" and "rebuilt" in out["error"]["message"]
    assert "cannot rebuild" in caplog.text
    with pytest.raises(HostError, match="internal"):
        begin(store)  # the write landed: a retry must not run it again


def test_a_write_plans_at_least_one_event(env):
    store, _, _ = env
    with pytest.raises(ValueError):
        store.begin(P, IDEM, "new", {}, planned=[])
    with pytest.raises(ValueError):
        store.execute(P, IDEM, "new", {}, lambda p: ok_result({}), planned=[])


def test_open_reads_every_record_once_and_the_limit_check_reads_only_what_it_changes(tmp_path, monkeypatch):
    now = [1_800_000_000.0]
    (tmp_path / "state").mkdir(mode=0o700)
    store = OutcomeStore(tmp_path / "state", WS, UID, clock=lambda: now[0], max_records=20, reconciler=FakeLog())
    store.open()
    idems = [f"a_01J9ZP00000000000000000{i:02d}" for i in range(20)]
    for idem in idems:
        now[0] += 1
        store.commit(begin(store, idem), ok_result({}))
    reads = []
    real = type(store).read

    def counting(self, key):
        reads.append(key)
        return real(self, key)

    monkeypatch.setattr(type(store), "read", counting)
    fresh = OutcomeStore(tmp_path / "state", WS, UID, clock=lambda: now[0], max_records=20, reconciler=FakeLog())
    fresh.open()
    fresh.reconcile_all()
    fresh.sweep()
    assert len(reads) == 20  # one pass: not three
    reads.clear()
    begin(fresh, "a_01J9ZP0000000000000000000Z")  # at the limit: the oldest one becomes a tombstone
    assert len(reads) <= 2
    assert fresh.count(P) == 20


def test_the_index_follows_every_write(env):
    store, log, now = env
    a = begin(store, IDEM)
    assert store.count(P) == 1
    store.abandon(a)
    assert store.count(P) == 0 and not store._idx
    store.commit(begin(store, IDEM), ok_result({}))
    now[0] += KEEP_SECONDS + 1
    store.sweep()
    assert store.count(P) == 0 and [e[1] for e in store._idx.values()] == ["tombstone"]
    now[0] += TOMBSTONE_SECONDS + 1
    store.sweep()
    assert not store._idx and os.listdir(store.dir) == []


# --- jobs -----------------------------------------------------------------------------------------------------------


def test_job_records_leased_at_start_become_void_and_stale_ones_are_swept(jobs, tmp_path):
    store, now = jobs
    conn = object()
    store.lease(JOB, WS, "tree.check", "L" * 26, conn)
    other = "01J9ZP0000000000000000000K"
    store.lease(other, WS, "tree.check", "M" * 26, conn)
    fresh = CompletionStore(tmp_path / "state", UID, clock=lambda: now[0])
    fresh.open()  # a restart: no connection survives
    assert json.loads((fresh.dir / JOB).read_text())["state"] == "void"
    with pytest.raises(HostError, match="worker.lease"):
        fresh.done(JOB, "L" * 26, "ok", completion_digest("ok", {}), conn)
    now[0] += KEEP_SECONDS + 1
    fresh.sweep()
    assert os.listdir(fresh.dir) == []


def test_job_sweep_survives_a_concurrent_removal(jobs, monkeypatch):
    store, now = jobs
    store.lease(JOB, WS, "tree.check", "L" * 26, object())
    store._bound.clear()
    now[0] += KEEP_SECONDS + 1

    def gone(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr("orch.host.jobs.os.unlink", gone)
    store.sweep()  # no exception


# --- deferred plans (H4a1): begin(deferred), plan(), execute(planned=None, before=, redactor=) ------------------------


def reopen(tmp_path, log, now):
    """The same records read by a fresh process (a crash: nothing is running any more)."""
    store = OutcomeStore(tmp_path / "state", WS, UID, clock=lambda: now[0], reconciler=log)
    store.open()
    return store


def test_a_deferred_record_with_nothing_planned_is_removed_by_reconciliation(env, tmp_path):
    store, log, now = env
    intent = store.begin(P, IDEM, "new", {"title": "x"}, planned=(), deferred=True)
    assert intent.record["planned"] == []
    again = reopen(tmp_path, log, now)
    assert again.reconcile_all() == [("new", "removed")]  # nothing planned: nothing can have landed
    assert again.read(intent.key) is None


def test_a_crash_between_plan_and_append_removes_the_record_and_after_the_append_commits(env, tmp_path):
    store, log, now = env
    a = store.begin(P, IDEM, "new", {"title": "x"}, planned=(), deferred=True)
    store.plan(a, [pl("L", "e1"), pl("L", "e2")], "DEMO-0001")
    assert [p["id"] for p in store.read(a.key)["planned"]] == ["e1", "e2"]  # durable before any append
    b = store.begin(P, IDEM2, "new", {"title": "y"}, planned=(), deferred=True)
    store.plan(b, [pl("M", "f1")])
    log.land("M", "f1", seq=3)
    again = reopen(tmp_path, log, now)
    assert sorted(again.reconcile_all()) == [("new", "committed"), ("new", "removed")]
    assert again.read(a.key) is None  # planned, never appended: the retry runs again
    assert again.read(b.key)["step"] == "committed"


def test_a_crash_after_the_first_of_two_batches_is_partial(env, tmp_path):
    store, log, now = env
    a = store.begin(P, IDEM, "apply", {}, planned=(), deferred=True)
    store.plan(a, [pl("L", "e1")])
    log.land("L", "e1")
    store.plan(a, [pl("L", "e2")])  # the second batch was announced but did not land
    again = reopen(tmp_path, log, now)
    assert again.reconcile_all() == [("apply", "partial")]


def test_plan_dedups_by_id_and_refuses_a_record_that_no_longer_runs(env):
    store, log, now = env
    a = store.begin(P, IDEM, "new", {"title": "x"}, planned=(), deferred=True)
    store.plan(a, [pl("L", "e1")])
    store.plan(a, [pl("L", "e1"), pl("L", "e2")], "DEMO-0002")
    record = store.read(a.key)
    assert [p["id"] for p in record["planned"]] == ["e1", "e2"] and record["key"] == "DEMO-0002"
    store.release(a)
    with pytest.raises(HostError) as e:
        store.plan(a, [pl("L", "e3")])
    assert e.value.code == "internal"


def test_execute_before_raising_removes_the_record_so_the_idem_runs_again(env):
    store, log, now = env
    ran = []

    def refuse():
        raise HostError("workspace.degraded", "read-only")

    with pytest.raises(HostError) as e:
        store.execute(P, IDEM, "new", {"title": "x"}, lambda i: ran.append(1), planned=None, before=refuse)
    assert e.value.code == "workspace.degraded" and ran == []
    assert store.read(store.key(WS, P, IDEM)) is None

    def apply(intent):
        store.plan(intent, [pl("L", "e9")])
        log.land("L", "e9", seq=4)
        return ok_result({"done": True}, seq=4)

    assert store.execute(P, IDEM, "new", {"title": "x"}, apply, planned=None)["data"] == {"done": True}


def test_execute_redacts_a_refusal_before_it_is_stored(env):
    store, log, now = env

    class Redact:
        @staticmethod
        def error(e):
            return HostError(e.code, e.message.replace("SECRET", "[redacted]"))

        @staticmethod
        def obj(v):
            return json.loads(json.dumps(v).replace("SECRET", "[redacted]"))

    def apply(intent):
        raise HostError("invalid.input", "has SECRET in it")

    with pytest.raises(HostError) as e:
        store.execute(P, IDEM, "new", {"title": "x"}, apply, planned=None, redactor=Redact)
    assert "SECRET" not in e.value.message
    assert "SECRET" not in json.dumps(store.read(store.key(WS, P, IDEM)))

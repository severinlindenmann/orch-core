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

    def land(self, log, event_id, seq=1):
        self.events[(log, event_id)] = {"id": event_id, "seq": seq, "type": "x"}

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


def begin(store, idem=IDEM, op="new", args=None, **kw):
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
    intent = begin(store, planned=[("L", "e1")])
    path = store.dir / intent.key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = json.loads(path.read_text())
    assert record["step"] == "pending" and record["planned"] == [{"log": "L", "id": "e1"}]  # before any append
    assert store.lookup(P, IDEM) == {"state": "pending"}
    done = store.commit(intent, ok_result({"n": 1}, seq=4))
    assert done["step"] == "committed"
    again = begin(store, planned=[("L", "e2")])
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

        out.append(store.execute(P, IDEM, "new", {"title": "x"}, apply, planned=[("L", "e")]))

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
        store.execute(P, IDEM, "new", {"title": "x"}, boom, planned=[("L", "e")])
    assert store.execute(P, IDEM, "new", {"title": "x"}, lambda p: ok_result({"ok": 1}), planned=[("L", "e2")])[
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
    other = store.begin("uid:2", IDEM, "new", {"title": "other"})
    assert not isinstance(other, dict)


# --- reconciliation -------------------------------------------------------------------------------------------------


def crash_after_pending(store, **kw):
    intent = begin(store, **kw)
    store.release(intent)  # the process died: the record stays pending, nothing runs it
    return intent


def test_reconcile_all_landed_rebuilds_the_result_from_the_log(env):
    store, log, _ = env
    crash_after_pending(store, planned=[("L", "e1")])
    log.land("L", "e1", seq=7)
    assert store.reconcile_all() == [("new", "committed")]
    assert store.lookup(P, IDEM)["result"]["data"] == {"rebuilt": ["e1"]}
    replay = begin(store)
    assert replay["duplicate"] is True and replay["seq"] == 7


def test_a_duplicate_reconciles_a_stale_pending_record_at_its_next_lookup(env):
    store, log, _ = env
    crash_after_pending(store, planned=[("L", "e1")])
    log.land("L", "e1", seq=3)
    replay = begin(store)  # no reconcile_all ran: the retry itself reconciles
    assert replay["duplicate"] is True and replay["data"] == {"rebuilt": ["e1"]}


def test_lookup_reconciles_a_stale_pending_record(env):
    store, log, _ = env
    crash_after_pending(store, planned=[("L", "e1")])
    log.land("L", "e1")
    assert store.lookup(P, IDEM)["state"] == "committed"


def test_some_landed_is_partial_and_a_retry_gets_the_interrupted_error(env):
    store, log, _ = env
    crash_after_pending(store, op="apply", planned=[("L", "e1"), ("L", "e2")])
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
    crash_after_pending(store, op="human.submit", planned=[("L", "e1"), ("L", "e2")])
    log.land("L", "e1")
    assert store.reconcile_all() == [("human.submit", "pending")]
    assert store.lookup(P, IDEM) == {"state": "pending"}
    with pytest.raises(HostError) as exc:
        begin(store, op="human.submit")
    assert exc.value.code == "outcome.unknown" and exc.value.retryable


def test_none_landed_removes_the_record_and_the_retry_runs_again(env):
    store, _, _ = env
    intent = crash_after_pending(store, planned=[("L", "e1")])
    assert store.reconcile_all() == [("new", "removed")]
    assert not (store.dir / intent.key).exists()
    with pytest.raises(HostError, match="outcome.unknown_idem"):
        store.lookup(P, IDEM)
    again = begin(store, planned=[("L", "e9")])
    assert not isinstance(again, dict)  # it runs again, with fresh planned ids


def test_an_unreadable_log_leaves_the_record_pending(env):
    store, log, _ = env
    crash_after_pending(store, planned=[("L", "e1")])
    log.broken = True
    assert store.reconcile_all() == [("new", "pending")]
    with pytest.raises(HostError) as exc:
        begin(store)
    assert exc.value.code == "outcome.unknown"
    log.broken = False
    assert store.reconcile_all() == [("new", "removed")]


def test_a_running_call_is_not_reconciled_away(env):
    store, log, _ = env
    intent = begin(store, planned=[("L", "e1")])  # running in this process
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
    crash_after_pending(store, planned=[("L", "e1")])
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
    other = store.begin("uid:9", idems[0], "new", {"title": "x"})  # the limit is per principal
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
    crash_after_pending(store, files=files, planned=[("L", "e1")])  # died before the append
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
    assert not isinstance(begin(store, files=again, planned=[("L", "e2")]), dict)


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

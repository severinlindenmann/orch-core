"""R2 round 2 (Opus review of #467, orch-52 rulings A to D and C5, G1): regression tests, one per finding, plus the
tests that kill the review's surviving mutants. The reviewer's repros (``test_review_r2.py``) asserted the broken
behaviour; these assert the fixed one."""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time

import pytest

from orch.relay import bridge as B
from orch.relay import framing, rids
from orch.relay.bridge import BridgeLoop, Item
from orch.relay.keys import SealRefused
from orch.wsproto import wire
from orch.wsproto.state import StateError

from .bridgekit import T0, Phone, World, open_chunk, ulid


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _metas(w, chunks):
    return [open_chunk(c, w.wk, w.key.public_key)[1] for c in chunks]


def _rid(env: bytes) -> str:
    return wire.parse_header(env[: wire.HEADER_LEN]).rid.hex()


def _whoami(w, **kw):
    return w.phone.request(w.wk, {"op": "whoami", "args": {}}, **kw)


# -- R2-1 / G1: the seal gate ------------------------------------------------------------------------------------------


def test_restart_after_rotation_never_seals_committed_data_under_the_old_epoch(w):
    m = w.new_ticket()
    env = w.phone.request(w.wk, m)
    adm = w.bridge.receive(w.item(env))
    w.host.submit(adm.identity, m["idem"], "new_ticket", m["args"]["log"], m["args"]["events"], lambda: True)
    w.epochs.rotate()  # crash before T2, rotate, restart
    before = len(w.post.posts)
    w.reopen()
    chunks = [e for _, r, e in w.post.posts[before:] if r == _rid(env)]
    (h, meta, data) = open_chunk(chunks[0], w.wk, w.key.public_key)
    assert len(chunks) == 1 and h.epoch == 2 and w.epochs.cur == 3
    assert meta == {"refusal": "already_done", "status": "unknown"} and data == b""
    assert h.flags == wire.FLAG_LAST | wire.FLAG_REFUSAL


def test_the_sealer_refuses_data_under_an_old_or_gated_epoch_and_builds_refusals_itself(w):
    h = wire.parse_header(_whoami(w)[: wire.HEADER_LEN])
    old = wire.parse_header(_whoami(w, epoch=1)[: wire.HEADER_LEN])
    with w.bridge.keys.sealing(w.bridge.signer) as s:
        assert s.data_ok(2) and not s.data_ok(1)
        s.seal_response(request=h, idx=0, last=True, ts_ms=T0, plaintext=b"\x00\x00\x00\x02{}")
        with pytest.raises(SealRefused):
            s.seal_response(request=old, idx=0, last=True, ts_ms=T0, plaintext=b"\x00\x00\x00\x02{}")
        env = s.seal_refusal(request=old, idx=0, ts_ms=T0, code="stale_epoch", epoch=2)
        assert open_chunk(env, w.wk, w.key.public_key)[1] == {"refusal": "stale_epoch", "epoch": 2}
        for bad in ({"code": "stale_epoch", "epoch": 2, "data": "x"}, {"code": "busy", "epoch": 2},
                    {"code": "ticket.exists", "message": "echo"}, {"code": "x.y", "retryable": True},
                    {"code": "ticket.exists", "next_key": "K-1"}, {"code": "x.y", "seqs": []}):  # fmt: skip
            with pytest.raises(ValueError):
                s.seal_refusal(request=old, idx=0, ts_ms=T0, **bad)
    w.epochs.gated = True
    with w.bridge.keys.sealing(w.bridge.signer) as s:
        assert not s.data_ok(2)
        with pytest.raises(SealRefused):
            s.seal_response(request=h, idx=0, last=True, ts_ms=T0, plaintext=b"\x00\x00\x00\x02{}")


def test_the_gate_is_serialised_with_sealing_and_committing(w):
    """R6 raises the gate inside ``serialised()``: it waits for a sealing span to end (commit included)."""
    entered, done = threading.Event(), threading.Event()

    def r6():
        entered.set()
        with w.bridge.keys.serialised():
            w.epochs.gated = True
        done.set()

    with w.bridge.keys.sealing(w.bridge.signer) as s:
        t = threading.Thread(target=r6)
        t.start()
        entered.wait(2)
        time.sleep(0.05)
        assert not done.is_set() and s.data_ok(2)  # the snapshot holds until the span (and its commit) ends
    t.join(2)
    assert done.is_set() and w.epochs.gated


def test_a_gated_epoch_refuses_new_requests_stale_epoch_naming_the_pending_one(w):
    w.epochs.gated, w.epochs.pending = True, 3
    (meta,) = _metas(w, w.send(_whoami(w)))
    assert meta == {"refusal": "stale_epoch", "epoch": 3}


def test_a_gate_raised_while_a_request_ran_answers_already_done_not_data(w):
    env = _whoami(w)
    adm = w.bridge.receive(w.item(env))
    w.epochs.gated = True
    w.bridge.run(adm)
    assert _metas(w, w.post.for_rid(_rid(env))) == [{"refusal": "already_done", "status": "unknown"}]


# -- C5 (proto §8.5 G2b): a burned epoch above the committed one -----------------------------------------------------


def test_request_under_burned_epoch_key_gets_signed_stale_epoch(w):
    """The relay vector of this name (orch-relay ref, not in its corpus at 438b719 yet), ported: ONLY a signed
    stale_epoch under the burned key, naming the pending epoch, else EHW + 1."""
    e = w.epochs.burn()  # 3, above the committed 2
    chunks = w.send(_whoami(w, epoch=e))
    (h, meta, data) = open_chunk(chunks[0], w.wk, w.key.public_key)
    assert len(chunks) == 1 and h.epoch == e and meta == {"refusal": "stale_epoch", "epoch": e + 1} and data == b""
    w.epochs.pending = e + 1
    (meta,) = _metas(w, w.send(_whoami(w, epoch=e)))
    assert meta == {"refusal": "stale_epoch", "epoch": e + 1}
    # nothing else is ever sealed under a burned key: a stranger, a bad frame, an old timestamp are silence
    assert w.send(Phone().request(w.wk, {"op": "whoami", "args": {}}, epoch=e)) == []
    assert w.send(w.phone.request(w.wk, b"\x00\x00\x00\x02{}", epoch=e)) == []
    assert w.send(_whoami(w, epoch=e, ts_ms=T0 - 300_001)) == []
    assert w.host.reads == []


def test_an_epoch_neither_held_nor_burned_is_dropped(w):
    w.wk[3] = secrets.token_bytes(32)  # the phone has a key the host never had
    assert w.send(_whoami(w, epoch=3)) == [] and w.rids.dump()["rid"] == []


def test_epoch_0_is_dropped_never_answered(w):
    w.wk[0] = secrets.token_bytes(32)
    env = w.phone.request(w.wk, {"op": "whoami", "args": {}}, epoch=0)
    assert w.send(env) == [] and w.rids.dump()["rid"] == []


# -- R2-2 ruling A: a known identity before the window ----------------------------------------------------------------


def test_a_committed_event_resent_late_on_a_new_rid_replays_its_outcome_not_event_expired(w):
    m = w.new_ticket()
    meta, first = w.call(m)
    assert meta == {"status": "ok"}
    w.clock.ms += 901_000  # the answer was lost; the same signed event on a new rid, 15 min later
    meta, again = w.call(m, ts_ms=w.clock.ms)
    assert meta == {"status": "ok"} and again["duplicate"] is True and again["data"] == first["data"]
    assert len(w.host.runs) == 1


def test_a_never_seen_old_event_is_event_expired_c_review_against_host_time(w):
    m = w.new_ticket()
    w.clock.ms += 901_000  # host time moved on; the header is fresh, the event id is not
    meta, _ = w.call(m, ts_ms=w.clock.ms)
    assert meta == {"refusal": "event.expired", "retryable": False}
    assert framing.retry_class("event.expired") == "C-review" and w.host.runs == []


# -- R2-3: busy_allowance, the row caps -----------------------------------------------------------------------------


def test_busy_is_recorded_only_within_the_allowance_and_one_device_cannot_fill_the_host(w, monkeypatch):
    monkeypatch.setattr(B, "PER_DEVICE", 2)
    monkeypatch.setattr(B, "BUSY_ALLOWANCE", 3)
    monkeypatch.setattr(B, "MAX_RECORDS", 20)
    answers = [w.send(_whoami(w)) for _ in range(30)]
    rows_a = [r for r in w.rids.dump()["rid"] if r[0] == w.phone.device]
    assert len(rows_a) == 5  # 2 admitted + 3 busy, then dropped
    assert [len(a) for a in answers] == [1] * 5 + [0] * 25
    assert [m["refusal"] for a in answers[2:5] for m in _metas(w, a)] == ["busy"] * 3
    other = Phone()
    w.r6.add(other)
    w.phone = other
    assert _metas(w, w.send(_whoami(w))) == [{"status": "ok"}]


def test_the_day_caps_drop_beyond_them(w, monkeypatch):
    monkeypatch.setattr(B, "DEVICE_DAY_ROWS", 3)
    for _ in range(3):
        w.send(_whoami(w))
        w.clock.ms += 1_000_000  # past the 15 min window, inside the day
    assert w.send(_whoami(w, ts_ms=w.clock.ms)) == []
    monkeypatch.setattr(B, "DEVICE_DAY_ROWS", 100)
    monkeypatch.setattr(B, "MAX_RETAINED_ROWS", 3)
    other = Phone()
    w.r6.add(other)
    assert w.send(other.request(w.wk, {"op": "whoami", "args": {}}, ts_ms=w.clock.ms)) == []


def test_a_malformed_or_stale_refusal_counts_against_the_same_per_device_ceiling(w, monkeypatch):
    monkeypatch.setattr(B, "PER_DEVICE", 1)
    monkeypatch.setattr(B, "BUSY_ALLOWANCE", 1)
    w.send(w.phone.request(w.wk, b"\x00\x00\x00\x02{}"))  # malformed: recorded (1)
    assert [m["refusal"] for m in _metas(w, w.send(_whoami(w)))] == ["busy"]  # (2)
    assert w.send(_whoami(w)) == []


# -- R2-4: the stored-answer byte budget, growth bound, pragmas -----------------------------------------------------


def test_the_stored_answer_byte_budget_answers_busy_before_admission(w, monkeypatch):
    monkeypatch.setattr(B, "DEVICE_CHUNK_BYTES", 3 << 20)
    meta, _ = w.call({"op": "ticket.text", "args": {"size": 1 << 20}})
    assert meta == {"status": "ok"}
    (meta,) = _metas(w, w.send(_whoami(w)))
    assert meta == {"refusal": "busy"} and framing.retry_class("busy") == "A"
    w.rids.sweep(w.clock.ms + rids.CHUNK_KEEP_MS)  # the bytes go after 960 s
    w.clock.ms += rids.CHUNK_KEEP_MS
    assert _metas(w, w.send(_whoami(w, ts_ms=w.clock.ms))) == [{"status": "ok"}]
    monkeypatch.setattr(B, "DEVICE_CHUNK_BYTES", 64 << 20)
    monkeypatch.setattr(B, "CHUNK_BYTES", 1 << 20)
    assert _metas(w, w.send(_whoami(w, ts_ms=w.clock.ms))) == [{"refusal": "busy"}]


def test_admitted_reads_reserve_a_whole_answer_each(w, monkeypatch):
    monkeypatch.setattr(B, "DEVICE_CHUNK_BYTES", 2 * framing.RELAY_DEVICE_RESPONSE_BYTES)
    assert w.bridge.receive(w.item(_whoami(w))) is not None  # admitted, not run: 2 MiB reserved
    assert w.bridge.receive(w.item(_whoami(w))) is not None
    assert _metas(w, w.send(_whoami(w))) == [{"refusal": "busy"}]


def test_rids_sqlite_durability_and_vacuum_pragmas(w):
    assert w.rids.pragmas() == {"journal_mode": "wal", "synchronous": 2, "auto_vacuum": 2}


def test_tombstones_are_capped_oldest_first_and_the_file_returns_pages(w, monkeypatch):
    monkeypatch.setattr(rids, "MAX_TOMBSTONES", 2)
    w.send(w.phone.request(w.wk, {"op": "ticket.text", "args": {"size": 1 << 20}}))  # pages to give back later
    for _ in range(4):
        w.send(_whoami(w, ts_ms=w.clock.ms))
        w.clock.ms += 1000
    w.rids.sweep(w.clock.ms + rids.RID_KEEP_MS)
    states = [r[2] for r in w.rids.dump()["rid"]]
    assert states.count("tombstone") == 2
    assert w.rids._db.execute("PRAGMA freelist_count").fetchone()[0] == 0  # noqa: SLF001 - vacuumed


def test_a_vacuum_step_frees_exactly_its_page_budget(w, monkeypatch):
    """Pages are really returned, VACUUM_STEP per step (Python 3.11 too, where `incremental_vacuum(N)` through
    `execute` frees ONE page)."""
    monkeypatch.setattr(rids, "VACUUM_PAGES", 0)  # the sweep frees rows but returns no pages
    monkeypatch.setattr(rids, "VACUUM_STEP", 50)
    w.send(w.phone.request(w.wk, {"op": "ticket.text", "args": {"size": 1 << 20}}))
    w.rids.sweep(w.clock.ms + rids.RID_KEEP_MS)
    db = w.rids._db  # noqa: SLF001
    free, pages = (db.execute(f"PRAGMA {k}").fetchone()[0] for k in ("freelist_count", "page_count"))
    assert free > 2 * 50
    with w.rids._lock:  # noqa: SLF001
        assert w.rids._vacuum_step() is False  # noqa: SLF001
    assert db.execute("PRAGMA freelist_count").fetchone()[0] == free - 50
    assert db.execute("PRAGMA page_count").fetchone()[0] == pages - 50
    assert not db.in_transaction


def test_the_vacuum_step_refuses_an_open_transaction_and_never_commits_it(w):
    db = w.rids._db  # noqa: SLF001
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO window(device, high, bitmap) VALUES('vacuum-probe', 1, '0')")
    with w.rids._lock, pytest.raises(StateError, match="transaction is open"):  # noqa: SLF001
        w.rids._vacuum_step()  # noqa: SLF001
    assert db.in_transaction  # still open: neither committed nor joined
    db.execute("ROLLBACK")
    assert db.execute("SELECT COUNT(*) FROM window WHERE device='vacuum-probe'").fetchone()[0] == 0


class _CommitFails:
    """The store's connection, except that the next COMMIT raises (with the transaction still open)."""

    def __init__(self, conn) -> None:
        self.conn, self.armed = conn, True

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *a):
        if self.armed and sql == "COMMIT":
            self.armed = False
            raise sqlite3.OperationalError("disk I/O error (injected)")
        return self.conn.execute(sql, *a)


def test_a_failed_vacuum_commit_rolls_back_and_frees_the_writer_lock(w, monkeypatch):
    monkeypatch.setattr(rids, "VACUUM_PAGES", 0)
    w.send(w.phone.request(w.wk, {"op": "ticket.text", "args": {"size": 1 << 20}}))
    w.rids.sweep(w.clock.ms + rids.RID_KEEP_MS)
    real = w.rids._conn  # noqa: SLF001
    free = real.execute("PRAGMA freelist_count").fetchone()[0]
    assert free > 0
    w.rids._conn = _CommitFails(real)  # noqa: SLF001
    try:
        with w.rids._lock, pytest.raises(sqlite3.OperationalError, match="injected"):  # noqa: SLF001
            w.rids._vacuum_step()  # noqa: SLF001
    finally:
        w.rids._conn = real  # noqa: SLF001
    assert not real.in_transaction  # rolled back: the writer lock is free
    assert real.execute("PRAGMA freelist_count").fetchone()[0] == free  # nothing of the step kept
    assert w.bridge.receive(w.item(_whoami(w))) is not None  # a later write (a _tx) goes through


def test_a_failed_commit_of_a_write_rolls_back_and_frees_the_writer_lock(w):
    real = w.rids._conn  # noqa: SLF001
    w.rids._conn = _CommitFails(real)  # noqa: SLF001
    probe = "INSERT INTO window(device, high, bitmap) VALUES('commit-probe', 1, '0')"
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            w.rids._tx("probe", lambda db: db.execute(probe))  # noqa: SLF001
    finally:
        w.rids._conn = real  # noqa: SLF001
    assert not real.in_transaction
    assert real.execute("SELECT COUNT(*) FROM window WHERE device='commit-probe'").fetchone()[0] == 0
    assert w.bridge.receive(w.item(_whoami(w))) is not None  # a later write goes through


def test_every_transaction_statement_of_rids_is_in_the_one_helper():
    """Structural (second COMMIT-outside-try finding): BEGIN/COMMIT/ROLLBACK/END/SAVEPOINT/RELEASE appear in
    ``rids.py`` only inside ``_atomic``, which commits inside its try and rolls back on any exception."""
    import ast
    import re
    from pathlib import Path

    tree = ast.parse(Path(rids.__file__).read_text())
    word = re.compile(r"\s*(BEGIN|COMMIT|ROLLBACK|END|SAVEPOINT|RELEASE)\b", re.IGNORECASE)
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_atomic":
            inside |= {id(n) for n in ast.walk(node)}
    found = [
        (n.lineno, n.value) for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and word.match(n.value) and id(n) not in inside
    ]  # fmt: skip
    assert found == []
    statements = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and word.match(n.value) and id(n) in inside
    ]  # fmt: skip
    assert sorted(statements) == ["BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"]


# -- R2-5: the answer cap is the relay's per-device undelivered budget ------------------------------------------------


def test_max_answer_fits_the_relays_per_device_response_budget(w):
    assert framing.RELAY_DEVICE_RESPONSE_BYTES == 2 << 20 and framing.MAX_ANSWER == 2_095_587
    data_len = framing.MAX_ANSWER
    pts = framing.answer_plaintexts({"d": "x" * (data_len - len('{"d":""}'))}, room=B.CHUNK_ROOM)
    assert len(pts) == 8 and sum(len(p) + wire.OVERHEAD for p in pts) == framing.RELAY_DEVICE_RESPONSE_BYTES
    with pytest.raises(framing.Refusal) as e:
        framing.answer_plaintexts({"d": "x" * (data_len - len('{"d":""}') + 1)}, room=B.CHUNK_ROOM)
    assert e.value.code == "frame.too_large"


def test_an_answer_that_could_never_be_delivered_is_frame_too_large_and_nothing_else_is_stored(w):
    env = w.phone.request(w.wk, {"op": "ticket.text", "args": {"size": framing.MAX_ANSWER}})
    (meta,) = _metas(w, w.send(env))
    assert meta == {"refusal": "frame.too_large", "retryable": False}
    assert [c for c in w.rids.dump()["chunk"] if c[1] == _rid(env)] == [(w.phone.device, _rid(env), 0, "posted", 1)]


# -- R2-6: never a stuck admitted write ------------------------------------------------------------------------------


def test_a_write_whose_run_fails_without_a_refusal_is_retried_and_answered(w, monkeypatch):
    m = w.new_ticket()
    real = w.host.submit
    monkeypatch.setattr(w.host, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("transient")))
    env = w.phone.request(w.wk, m)
    assert w.send(env) == []
    meta, data = w.call({"op": "outcome.get", "args": {"idem": m["idem"]}})
    assert data["data"] == {"state": "pending"}  # admitted: pending, bounded by the retry schedule
    monkeypatch.setattr(w.host, "submit", real)
    w.bridge.retry_stuck()
    assert w.post.for_rid(_rid(env)) == []  # not due yet
    w.clock.ms += B.STUCK_BACKOFF_MS[0]
    w.bridge.retry_stuck()
    (meta,) = _metas(w, w.post.for_rid(_rid(env)))
    assert meta == {"status": "ok"} and len(w.host.runs) == 1


def test_a_write_that_keeps_failing_ends_already_done_unknown_within_the_pending_bound(w, monkeypatch):
    m = w.new_ticket()
    monkeypatch.setattr(w.host, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    env = w.phone.request(w.wk, m)
    w.send(env)
    for delay in B.STUCK_BACKOFF_MS:
        w.clock.ms += delay
        w.bridge.retry_stuck()
    assert sum(B.STUCK_BACKOFF_MS) < framing.PENDING_NO_RECORD_MS
    assert _metas(w, w.post.for_rid(_rid(env))) == [{"refusal": "already_done", "status": "unknown"}]
    assert w.rids.get(w.phone.device, _rid(env)).state == "answered"
    w.clock.ms = framing.ulid_ms(m["idem"]) + framing.PENDING_NO_RECORD_MS
    meta, _ = w.call({"op": "outcome.get", "args": {"idem": m["idem"]}}, ts_ms=w.clock.ms)
    assert meta == {"refusal": "outcome.unknown_idem", "retryable": False}  # past the bound: C-review


def test_outcome_get_answers_a_terminal_record_before_the_admitted_shortcut(w):
    m = w.new_ticket(title="refuse me")
    w.call(m)  # failed: ticket.exists
    meta, data = w.call({"op": "outcome.get", "args": {"idem": m["idem"]}})
    assert data["data"]["state"] == "failed" and data["data"]["error"]["code"] == "ticket.exists"


def test_outcome_get_for_an_admitted_idem_is_pending(w):
    m = w.new_ticket()
    assert w.bridge.receive(w.item(w.phone.request(w.wk, m))) is not None  # admitted, not run
    meta, data = w.call({"op": "outcome.get", "args": {"idem": m["idem"]}})
    assert data["data"] == {"state": "pending"}


# -- R2-7 rulings B and D -----------------------------------------------------------------------------------------


def test_the_same_idem_on_a_new_rid_while_admitted_is_pending_and_never_runs_twice(w):
    m = w.new_ticket()
    first = w.phone.request(w.wk, m)
    adm = w.bridge.receive(w.item(first))
    (meta,) = _metas(w, w.send(w.phone.request(w.wk, m)))
    assert meta == {"refusal": "pending", "retryable": True} and framing.retry_class("pending") == "B"
    assert w.host.runs == []
    w.bridge.run(adm)
    assert len(w.host.runs) == 1
    meta, data = w.call(m)  # once terminal: the outcome replays
    assert meta == {"status": "ok"} and data["duplicate"] is True and len(w.host.runs) == 1


def test_a_same_rid_resend_after_the_bytes_were_swept_gets_already_done_at_the_next_index(w):
    env = _whoami(w)
    w.send(env)
    w.clock.ms += rids.CHUNK_KEEP_MS + 1
    w.rids.sweep(w.clock.ms)
    chunks = w.send(env)
    (h, meta, _) = open_chunk(chunks[0], w.wk, w.key.public_key)
    assert len(chunks) == 1 and h.seq == 1 and meta == {"refusal": "already_done", "status": "unknown"}
    assert h.flags == wire.FLAG_LAST | wire.FLAG_REFUSAL
    w.clock.ms += B.REPLAY_EVERY_MS
    again = w.send(env)
    assert again == [chunks[0]]  # the closing chunk is stored now: replayed byte for byte
    assert framing.retry_class("already_done") == "B"


# -- R2-9: stop() with a full job queue ----------------------------------------------------------------------------


class _SlowBridge:
    def __init__(self, release: threading.Event):
        self.release, self.rids, self.ran = release, self, 0

    def cursor(self):
        return 0

    def set_cursor(self, n):
        pass

    def receive(self, item):
        return object()

    def run(self, adm):
        self.ran += 1
        self.release.wait(5)


def _full_loop(release):
    br = _SlowBridge(release)
    loop = BridgeLoop(br, session=None, conn=lambda: None, workspace_id="0" * 32)  # type: ignore[arg-type]
    loop._spawn("resp", loop._resp_main)  # noqa: SLF001
    for i in range(B.JOBS):
        loop._spawn(f"job{i}", loop._job_main)  # noqa: SLF001
    for n in range(1, 8):
        loop._queue.put(Item(n, "bridge", "", "", b"x"))  # noqa: SLF001
    time.sleep(0.4)  # 2 running, 2 queued, the responder waiting to hand over
    return loop


def test_stop_with_a_full_job_queue_ends_every_thread_when_the_host_is_not_stuck():
    release = threading.Event()
    loop = _full_loop(release)
    loop.stop()
    release.set()
    assert loop.join(3.0) is True
    assert [t.name for t in loop._threads if t.is_alive()] == []  # noqa: SLF001


def test_join_is_false_while_a_job_is_stuck_in_the_host():
    release = threading.Event()
    loop = _full_loop(release)
    loop.stop()
    assert loop.join(0.5) is False
    release.set()
    assert loop.join(3.0) is True


# -- M11: the poll queue bound -------------------------------------------------------------------------------------


class _PollSession:
    def __init__(self, loop_ref, items):
        self.loop_ref, self.items, self.calls = loop_ref, items, 0

    def hold(self, **kw):
        pass

    def call(self, *a, **kw):
        self.calls += 1
        if self.calls > 1:
            self.loop_ref[0]._stop.set()  # noqa: SLF001 - one page only
        return {"items": self.items, "notices": {}, "next": None}


def test_a_poll_page_stops_filling_the_queue_at_its_bound():
    import base64

    env = base64.urlsafe_b64encode(b"x" * 200).rstrip(b"=").decode()
    items = [{"n": n, "route": "bridge", "device": "a" * 32, "rid": "b" * 32, "offer": None, "envelope": env}
             for n in range(1, 41)]  # fmt: skip
    gate = threading.Event()

    class Br(_SlowBridge):
        def receive(self, item):
            gate.wait(5)  # the responder holds one item and never drains the rest
            return None

        def start(self):
            pass

        def flush(self):
            pass

    ref: list = [None]
    loop = BridgeLoop(Br(gate), session=_PollSession(ref, items), conn=lambda: _Conn(), workspace_id="0" * 32)  # type: ignore[arg-type]
    ref[0] = loop
    loop._poll_main()  # noqa: SLF001 - in this thread, one page
    try:
        assert B.QUEUE_ITEMS == 16 and loop._queue.qsize() <= 16  # noqa: SLF001 - the documented bound
    finally:
        loop.stop()
        gate.set()
        assert loop.join(3.0)


class _Conn:
    def shutdown(self):
        pass


# -- R2-10, R2-11, R2-12, R2-13 ---------------------------------------------------------------------------------------


def test_recovery_rechecks_the_ops_scope_before_a_write(w):
    m = w.new_ticket()
    assert w.bridge.receive(w.item(w.phone.request(w.wk, m))) is not None  # T1, crash before the run
    w.r6.add(w.phone, scopes=("look",))  # operate removed from the member list
    w.reopen()
    assert w.host.runs == []


def test_a_forged_step_4_refusal_never_answers_the_genuine_request_on_that_rid(w):
    victim, rid = w.phone, secrets.token_bytes(16)
    attacker = Phone()
    w.r6.add(attacker)
    forged = victim.request(w.wk, {"op": "whoami", "args": {}}, rid=rid, signer=attacker.dk)
    assert _metas(w, w.send(forged)) == [{"refusal": "bad_signature"}]
    got = w.send(victim.request(w.wk, {"op": "whoami", "args": {}}, rid=rid))
    assert _metas(w, got) == [{"status": "ok"}]


def test_an_unsigned_request_reaches_the_store_only_within_the_refusal_budget(w):
    stranger = Phone()
    calls = []
    real = w.host.standing
    w.host.standing = lambda d, p: (calls.append(d), real(d, p))[1]  # type: ignore[method-assign]
    for _ in range(B.BUDGET + 5):
        w.send(w.phone.request(w.wk, {"op": "whoami", "args": {}}, signer=stranger.dk))
    assert len(calls) == B.BUDGET


def test_rotate_rid_rereads_when_a_chunk_became_attempted_meanwhile(w, monkeypatch):
    env = _whoami(w)
    w.post.default = "retry"
    w.send(env)
    w.epochs.rotate()
    real, calls = w.rids.replace_unattempted, []

    def racy(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("replace_unattempted: an attempted index")
        return real(*a, **k)

    monkeypatch.setattr(w.rids, "replace_unattempted", racy)
    w.bridge.rotate_rid(w.phone.device, _rid(env))  # no exception: re-read
    assert len(calls) <= 3


def test_relay_entry_exposes_no_service_or_backend():
    from orch.host.relayentry import RelayEntry

    e = RelayEntry(object(), object())  # type: ignore[arg-type]
    assert not hasattr(e, "service") and not hasattr(e, "backend")


# -- M26, M29: timestamps, refusal fields --------------------------------------------------------------------------


def test_a_future_timestamp_beyond_300_s_is_stale_timestamp(w):
    (meta,) = _metas(w, w.send(_whoami(w, ts_ms=T0 + B.WINDOW_MS + 1)))
    assert meta == {"refusal": "stale_timestamp", "host_ms": T0}
    assert _metas(w, w.send(_whoami(w, ts_ms=T0 + B.WINDOW_MS))) == [{"status": "ok"}]


def test_a_host_refusal_carries_exactly_its_closed_fields(w, monkeypatch):
    def refuse(*a, **k):
        raise framing.Refusal("ticket.key_not_next", next_key="DEMO-9", message="echo", seqs=[3], retryable=True)

    monkeypatch.setattr(w.host, "read", refuse)
    (meta,) = _metas(w, w.send(_whoami(w)))
    assert meta == {"refusal": "ticket.key_not_next", "retryable": False, "next_key": "DEMO-9", "seqs": [3]}

    def other(*a, **k):
        raise framing.Refusal("not_found", next_key="DEMO-9", seqs=[0], retryable=True, high=5)

    monkeypatch.setattr(w.host, "read", other)
    (meta,) = _metas(w, w.send(_whoami(w)))
    assert meta == {"refusal": "not_found", "retryable": False}


def test_a_read_internal_is_d_and_a_write_internal_is_b(w, monkeypatch):
    monkeypatch.setattr(w.host, "read", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    assert _metas(w, w.send(_whoami(w))) == [{"refusal": "internal", "retryable": False}]
    monkeypatch.setattr(w.host, "submit", lambda *a, **k: (_ for _ in ()).throw(framing.Refusal("internal")))
    meta, _ = w.call(w.new_ticket())
    assert meta == {"refusal": "internal", "retryable": True}


def test_retryable_is_derived_from_the_table_for_every_class():
    for code, cls in framing.RETRY.items():
        assert framing.retryable(code) is (cls in ("A", "B")), code
    assert framing.retry_class("internal", effects_possible=True) == "B"
    assert framing.retry_class("outcome.unknown_idem", before_bound=True) == "B"
    assert framing.retry_class("outcome.unknown_idem") == "C-review"
    assert framing.retry_class("busy") == "A" and framing.retry_class("no.such") == "D"


def test_success_chunk_metas_are_exactly_status_ok_then_empty(w):
    chunks = w.send(w.phone.request(w.wk, {"op": "ticket.text", "args": {"size": 600_000}}))
    assert _metas(w, chunks) == [{"status": "ok"}, {}, {}]


def test_a_request_carrying_rd_is_served_and_rd_reaches_the_identity(w, monkeypatch):
    seen = []
    real = w.host.read
    monkeypatch.setattr(w.host, "read", lambda ident, *a: (seen.append(ident.rd), real(ident, *a))[1])
    rd = "sha256:" + "ab" * 32
    meta, _ = w.call({"op": "whoami", "args": {}, "rd": rd})
    assert meta == {"status": "ok"} and seen == [rd]


# -- the review's crash points on the real host are in test_round2_host.py ------------------------------------------


def test_ulid_helper_round_trip():
    assert framing.ulid_ms(ulid(T0)) == T0


# -- the extra DoS pass on bridge.py (coordinator, 15 Oct) ------------------------------------------------------------


def test_a_relay_answering_retry_forever_gets_exponentially_fewer_posts(w):
    w.post.default = "retry"
    env = _whoami(w)
    w.send(env)
    for _ in range(300):  # five minutes of aux flushes, one per second
        w.clock.ms += 1_000
        w.bridge.flush()
    n = len(w.post.for_rid(_rid(env)))
    assert 8 <= n <= 14  # 1, 2, 4, ... 60 s: not 300
    assert set(w.post.for_rid(_rid(env))) == {w.post.for_rid(_rid(env))[0]}  # always the same bytes


def test_malformed_poll_items_are_logged_once_per_page(caplog):
    with caplog.at_level("INFO", logger="orch.relay.bridge"):
        got = B._items({"items": [{"n": i} for i in range(500)]})  # noqa: SLF001
    assert got == [] and len([r for r in caplog.records if "skipped" in r.getMessage()]) == 1


def test_a_signed_device_over_its_caps_never_reaches_the_store_lock(w, monkeypatch):
    monkeypatch.setattr(B, "PER_DEVICE", 1)
    monkeypatch.setattr(B, "BUSY_ALLOWANCE", 1)
    calls = []
    real = w.host.standing
    w.host.standing = lambda d, p: (calls.append(d), real(d, p))[1]  # type: ignore[method-assign]
    for _ in range(10):
        w.send(_whoami(w))
    assert len(calls) == 3  # step 4 and the recheck of the admitted one, step 4 of the busy one; then none


def test_an_admitted_idem_past_the_no_record_bound_is_still_pending(w):
    m = w.new_ticket()
    assert w.bridge.receive(w.item(w.phone.request(w.wk, m))) is not None  # admitted, nothing recorded on the host
    w.clock.ms = framing.ulid_ms(m["idem"]) + framing.PENDING_NO_RECORD_MS + 1
    meta, data = w.call({"op": "outcome.get", "args": {"idem": m["idem"]}}, ts_ms=w.clock.ms)
    assert data["data"] == {"state": "pending"}  # this device's rid may still run: never "unknown" while admitted


def test_step_4_failures_under_a_burned_key_spend_no_refusal_budget(w):
    e = w.epochs.burn()
    stranger = Phone()
    for _ in range(B.BUDGET):
        assert w.send(stranger.request(w.wk, {"op": "whoami", "args": {}}, epoch=e)) == []
    assert _metas(w, w.send(stranger.request(w.wk, {"op": "whoami", "args": {}}))) == [{"refusal": "not_member"}]


def test_the_envelope_budget_holds_for_any_chunk_room():
    body = {"d": "x" * (framing.MAX_ANSWER - len('{"d":""}'))}
    with pytest.raises(framing.Refusal) as e:
        framing.answer_plaintexts(body, room=B.CHUNK_ROOM // 2)  # more chunks, more envelope overhead
    assert e.value.code == "frame.too_large"


def test_outcome_get_answers_a_committed_record_even_while_the_rid_is_admitted(w):
    m = w.new_ticket()
    adm = w.bridge.receive(w.item(w.phone.request(w.wk, m)))
    w.host.submit(adm.identity, m["idem"], "new_ticket", m["args"]["log"], m["args"]["events"], lambda: True)
    meta, data = w.call({"op": "outcome.get", "args": {"idem": m["idem"]}})  # crash before T2: committed already
    assert data["data"]["state"] == "committed"

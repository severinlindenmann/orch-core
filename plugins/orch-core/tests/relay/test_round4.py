"""R2 round 4 (Opus delta 3 of 39710ab1): one regression per finding (N3-1 to N3-3, the sweep's cursor) and the tests
for the delta's meaningful surviving mutants (V01, V09, V11/V12, V15 in ``test_round3``, V20 in ``test_relay_entry``,
V21 in ``test_submit``, V25, V26, V32, V34). The delta's repros (``scratchpad/r2delta3/.../test_delta3_review.py``)
asserted the broken behaviour; the ones adopted here assert the fixed one."""

from __future__ import annotations

import secrets
import threading

import pytest

from orch.relay import bridge as B
from orch.relay import framing, rids
from orch.relay.bridge import BridgeLoop, Item
from orch.relay.session import Fenced, SessionError
from orch.wsproto import wire

from .bridgekit import World, open_chunk, ulid
from .helpers import WS


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


# -- N3-1: the session's own errors stop the worker from the responder and the job threads --------------------------


class _Raising:
    def __init__(self, real, exc, *, bad=None):
        self.rids, self.alarms, self.seen, self.exc, self.bad = real.rids, [], [], exc, bad

    def receive(self, item):
        self.seen.append(item.n)
        if self.bad is None or item.n == self.bad:
            raise self.exc
        return None

    def run(self, adm):
        raise self.exc

    def alarm(self, code, detail):
        self.alarms.append(detail)


def _loop(br):
    return BridgeLoop(br, session=None, conn=lambda: None, workspace_id=WS)  # type: ignore[arg-type]


def _queue(loop, ns):
    for n in ns:
        loop._queue.put(Item(n, "bridge", "", "", b"x"))  # noqa: SLF001
        loop._queued_bytes += 1  # noqa: SLF001
    loop._queue.put(None)  # noqa: SLF001


@pytest.mark.parametrize("exc", [Fenced("fenced: writes stopped"), SessionError("lease lost")])
def test_a_fence_in_receive_stops_the_worker_and_the_item_is_not_acknowledged(w, exc):
    br = _Raising(w.bridge, exc)
    loop = _loop(br)
    _queue(loop, (1, 2, 3))
    loop._guard(loop._resp_main)()  # noqa: SLF001 - the thread body as _spawn runs it
    assert br.seen == [1] and loop.error is exc and loop._stop.is_set()  # noqa: SLF001
    assert w.rids.cursor() == 0 and br.alarms == []  # never an "item failure"; the next holder polls it again


@pytest.mark.parametrize("exc", [Fenced("fenced: writes stopped"), SessionError("lease lost")])
def test_a_fence_in_run_stops_the_worker(w, exc):
    br = _Raising(w.bridge, exc)
    loop = _loop(br)
    loop._jobs.put(object())  # noqa: SLF001
    t = threading.Thread(target=loop._guard(loop._job_main))  # noqa: SLF001
    t.start()
    t.join(5)
    assert not t.is_alive() and loop.error is exc and loop._stop.is_set() and br.alarms == []  # noqa: SLF001


def test_v01_a_poisoned_item_is_acknowledged_at_once(w):
    br = _Raising(w.bridge, ValueError("poison"), bad=2)
    loop = _loop(br)
    _queue(loop, (1, 2))
    loop._resp_main()  # noqa: SLF001
    assert br.seen == [1, 2] and w.rids.cursor() == 2  # the bad item itself, not only a later one, moves the cursor


def test_one_alarm_per_class_of_failure_bounded(w, monkeypatch):
    br = _Raising(w.bridge, ValueError())
    loop = _loop(br)
    loop._item_failed("flush", OSError())  # noqa: SLF001 - a harmless one first
    loop._item_failed("flush", OSError())  # noqa: SLF001
    loop._item_failed("receive", ValueError())  # noqa: SLF001 - a crash loop after it still alarms
    loop._item_failed("receive", ValueError())  # noqa: SLF001
    loop._item_failed("run", ValueError())  # noqa: SLF001
    assert br.alarms == ["flush: OSError", "receive: ValueError", "run: ValueError"]
    monkeypatch.setattr(B, "ALARM_CLASSES", 4)
    for i in range(10):
        loop._item_failed(f"step{i}", KeyError())  # noqa: SLF001
    assert len(br.alarms) == 5 and len(loop._alarmed) == 4  # noqa: SLF001 - bounded, plus one "exhausted" (N4-3)


# -- N3-2: an admitted read never holds its reserve for ever -------------------------------------------------------


def test_an_admitted_read_whose_key_is_gone_is_closed_and_releases_its_reserve(w):
    adms = [w.bridge.receive(w.item(_whoami(w))) for _ in range(8)]
    assert all(a is not None for a in adms) and w.rids.admitted_reads(w.phone.device) == 8
    w.epochs.rotate()  # 3 committed
    del w.epochs.keys[2]  # R6 retires epoch 2's key
    for a in adms:
        w.bridge.run(a)  # nothing can be sealed under 2: closed (tombstone), never admitted for ever
    assert w.rids.admitted_reads(w.phone.device) == 0
    assert {w.rids.get(w.phone.device, a.header.rid.hex()).state for a in adms} == {"tombstone"}
    c = w.rids.counts()
    assert c["memory"] == c["file"]
    meta, data = w.call({"op": "whoami", "args": {}}, epoch=3)
    assert meta == {"status": "ok"}  # not busy


def test_an_admitted_read_left_at_a_crash_with_its_key_gone_is_closed_at_restart(w):
    adm = w.bridge.receive(w.item(_whoami(w)))
    w.epochs.rotate()
    del w.epochs.keys[2]
    w.reopen()  # _settle: already_done cannot be sealed under 2: a tombstone
    assert w.rids.get(w.phone.device, adm.header.rid.hex()).state == "tombstone"
    assert w.rids.admitted_reads() == 0 and w.rids.admitted() == []


def test_an_old_admitted_read_is_answered_already_done_by_the_sweep_and_stops_counting(w):
    adm = w.bridge.receive(w.item(_whoami(w)))  # admitted, its run lost (never answered)
    rid = adm.header.rid.hex()
    w.clock.ms += B.READ_ADMITTED_MS
    w.bridge.sweep()
    assert w.rids.get(w.phone.device, rid).state == "admitted" and w.post.for_rid(rid) == []
    assert w.rids.admitted_reads(w.phone.device, since_ms=w.clock.ms - B.READ_ADMITTED_MS) == 1
    w.clock.ms += 1
    assert w.rids.admitted_reads(w.phone.device, since_ms=w.clock.ms - B.READ_ADMITTED_MS) == 0
    w.bridge.sweep()
    assert w.rids.get(w.phone.device, rid).state == "answered"
    assert _metas(w, w.post.for_rid(rid)) == [{"refusal": "already_done", "status": "unknown"}]
    w.bridge.run(adm)  # the lost run finishing late stores nothing (T2 needs admitted)
    assert len(w.post.for_rid(rid)) == 1


def test_the_byte_budget_ignores_admitted_reads_past_the_bound(w, monkeypatch):
    for _ in range(8):
        assert w.bridge.receive(w.item(_whoami(w))) is not None
    w.clock.ms += B.READ_ADMITTED_MS + 1
    assert w.bridge._bytes_ok(w.phone.device)  # noqa: SLF001 - before the sweep closes them


def test_the_aux_sweep_step_runs_the_read_expiry(w):
    calls = []
    w.bridge.expire_reads = lambda: calls.append("expire")  # type: ignore[method-assign]
    w.bridge.sweep()
    assert calls == ["expire"]


# -- N3-3: the 8 re-opens per rid are durable --------------------------------------------------------------------


def test_replays_never_append_past_the_per_rid_bound_after_tracker_eviction_or_a_restart(w, monkeypatch):
    monkeypatch.setattr(B, "REPLAY_TRACKED", 2)
    envs = [_whoami(w) for _ in range(3)]
    for e in envs:
        w.send(e)
    w.clock.ms += rids.CHUNK_KEEP_MS + 1000
    w.rids.sweep(w.clock.ms)

    def rounds(n):
        for _ in range(n):
            for e in envs:
                w.bridge.receive(w.item(e))
            w.clock.ms += 60_000
            w.rids.sweep(w.clock.ms)

    rounds(20)
    counts = [len(w.rids.get(w.phone.device, _rid(e)).chunks) for e in envs]
    assert counts == [1 + B.REPLAY_MAX] * 3
    assert {w.rids.get(w.phone.device, _rid(e)).reopens for e in envs} == {B.REPLAY_MAX}
    w.reopen()
    before = len(w.post.posts)
    rounds(3)
    assert len(w.post.posts) == before
    assert [len(w.rids.get(w.phone.device, _rid(e)).chunks) for e in envs] == counts


def test_replays_of_stored_bytes_are_bounded_across_a_restart(w):
    env = _whoami(w)
    w.send(env)
    for _ in range(5):
        w.clock.ms += B.REPLAY_EVERY_MS
        w.bridge.receive(w.item(env))
    w.reopen()
    for _ in range(10):
        w.clock.ms += B.REPLAY_EVERY_MS
        w.bridge.receive(w.item(env))
    assert len(w.post.for_rid(_rid(env))) == 1 + B.REPLAY_MAX


def test_an_older_file_gets_the_reopens_column(tmp_path):
    w = World(tmp_path, start=False)
    try:
        db = w.rids._db  # noqa: SLF001
        db.execute("DROP TABLE rid")
        db.execute(
            "CREATE TABLE rid(device TEXT NOT NULL, rid TEXT NOT NULL, first_ms INTEGER NOT NULL, req_digest TEXT "
            "NOT NULL, epoch INTEGER NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL, principal TEXT, idem TEXT, "
            "kind TEXT NOT NULL, PRIMARY KEY(device, rid))"
        )
        db.execute("INSERT INTO rid VALUES('d','r',1,'x',2,1,'answered',NULL,NULL,'refusal')")
        w.reopen(start=False)
        assert w.rids.get("d", "r").reopens == 0
        assert w.rids.note_reopen("d", "r", B.REPLAY_MAX) and w.rids.get("d", "r").reopens == 1
    finally:
        w.close()


# -- perf: the sweep reads each row once (a cursor, no rescans) ----------------------------------------------------


def _fill(w, n, *, first):
    db = w.rids._db  # noqa: SLF001
    devs = [secrets.token_hex(16) for _ in range(4)]
    db.execute("BEGIN")
    for i in range(n):
        d, r = devs[i % 4], secrets.token_hex(16)
        db.execute(
            "INSERT INTO rid(device, rid, first_ms, req_digest, epoch, seq, state, principal, idem, kind) "
            "VALUES(?,?,?,'sha256:x',2,?,'answered',NULL,NULL,'refusal')",
            (d, r, first - (i * 3_600_000 // n), i),
        )
        db.execute("INSERT INTO chunk VALUES(?,?,0,2,?,'posted')", (d, r, b"x" * 64))
    db.execute("COMMIT")
    w.rids._load_counts()  # noqa: SLF001


def _steps(tmp_path, n, monkeypatch):
    monkeypatch.setattr(rids, "SWEEP_BATCH", 16)
    (tmp_path / str(n)).mkdir()
    w = World(tmp_path / str(n), start=False)
    try:
        now = w.clock.ms
        _fill(w, n, first=now)
        steps = [0]

        def tick():
            steps[0] += 1
            return 0

        w.rids._db.set_progress_handler(tick, 100)  # noqa: SLF001 - SQLite VM steps, deterministic
        w.rids.sweep(now + rids.RID_KEEP_MS + 3_600_000)  # every row: bytes, chunks, tombstone
        w.rids._db.set_progress_handler(None, 0)  # noqa: SLF001
        c = w.rids.counts()
        assert c["memory"] == c["file"] and c["memory"][0] == 0
        return steps[0]
    finally:
        w.close()


def test_the_sweep_is_linear_in_the_rows_it_takes(tmp_path, monkeypatch):
    small, large = _steps(tmp_path, 512, monkeypatch), _steps(tmp_path, 2048, monkeypatch)
    assert large < 5 * small, (small, large)  # 4x the rows: quadratic rescans would be about 16x


def test_the_sweep_cursor_skips_admitted_and_young_rows_and_takes_the_rest(w, monkeypatch):
    monkeypatch.setattr(rids, "SWEEP_BATCH", 2)
    for _ in range(5):
        w.call({"op": "whoami", "args": {}})
    adm = w.bridge.receive(w.item(_whoami(w)))
    w.clock.ms += rids.RID_KEEP_MS + 1
    for _ in range(3):
        w.call({"op": "whoami", "args": {}}, ts_ms=w.clock.ms)
    w.rids.sweep(w.clock.ms)
    states = sorted(r[2] for r in w.rids.dump()["rid"])
    assert states == ["admitted"] + ["answered"] * 3 + ["tombstone"] * 5
    assert w.rids.get(w.phone.device, adm.header.rid.hex()).state == "admitted"
    c = w.rids.counts()
    assert c["memory"] == c["file"]


# -- the delta's surviving mutants ---------------------------------------------------------------------------------


def test_v09_a_ruling_d_append_is_counted_in_memory(w):
    env = _whoami(w)
    w.send(env)
    w.clock.ms += rids.CHUNK_KEEP_MS + 1000
    w.rids.sweep(w.clock.ms)
    w.bridge.receive(w.item(env))  # ruling D: a fresh already_done chunk is appended
    assert len(w.rids.get(w.phone.device, _rid(env)).chunks) == 2
    c = w.rids.counts()
    assert c["memory"] == c["file"] and c["memory"][1] > 0


def test_v11_v12_a_restart_reloads_the_counts(w):
    w.call({"op": "ticket.text", "args": {"size": 5000}})
    w.send(_whoami(w), run=False)  # an admitted read
    w.call(w.new_ticket())
    before = w.rids.counts()
    assert before["memory"] == before["file"] and before["memory"][1] > 0
    w.reopen(start=False)
    after = w.rids.counts()
    assert after["memory"] == after["file"] == before["file"]
    assert w.rids.live(0) == before["file"][0]  # the admitted row is counted too


def test_v25_outcome_get_is_pending_up_to_the_last_ms_of_the_bound(w):
    idem = ulid(w.clock.ms)
    w.clock.ms += framing.PENDING_NO_RECORD_MS - 1
    meta, data = w.call({"op": "outcome.get", "args": {"idem": idem}}, ts_ms=w.clock.ms)
    assert meta == {"status": "ok"} and data["data"] == {"state": "pending"}
    w.clock.ms += 1
    meta, _ = w.call({"op": "outcome.get", "args": {"idem": idem}}, ts_ms=w.clock.ms)
    assert meta == {"refusal": "outcome.unknown_idem", "retryable": False}


def test_v26_a_stuck_write_is_definite_before_the_bound_for_an_event_stamped_up_to_300s_early(w, monkeypatch):
    """An event id up to the request window (300 s) older than its admission: the stuck write is answered (the rid no
    longer admitted, so ``outcome.get`` stops saying ``pending``) before the event id's ULID time + the bound."""
    m = w.new_ticket()
    monkeypatch.setattr(w.host, "submit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    env = w.phone.request(w.wk, m)
    w.send(env)
    end = w.clock.ms + framing.PENDING_NO_RECORD_MS - B.WINDOW_MS
    while w.clock.ms < end:
        w.clock.ms += 10_000
        w.bridge.retry_stuck()
    assert w.rids.get(w.phone.device, _rid(env)).state == "answered"


def test_v32_rotation_keeps_answers_under_the_current_epoch(w):
    w.post.script = {1: "retry"}  # chunk 0 posted, chunk 1 attempted, chunk 2 stored: an open answer
    env = w.phone.request(w.wk, {"op": "ticket.text", "args": {"size": 3 * B.CHUNK_ROOM}})
    w.send(env)
    before = w.rids.get(w.phone.device, _rid(env))
    w.bridge.rotate()  # a rotation signal while epoch 2 is still the committed one
    after = w.rids.get(w.phone.device, _rid(env))
    assert [(c.idx, c.data) for c in after.chunks] == [(c.idx, c.data) for c in before.chunks]


def test_v34_a_failed_or_partial_outcome_keeps_its_landed_seqs_at_settle(w):
    env = w.phone.request(w.wk, w.new_ticket())
    adm = w.bridge.receive(w.item(env))
    rec = w.rids.get(w.phone.device, adm.header.rid.hex())
    got = {"state": "partial", "error": {"code": "ticket.exists"}, "seqs": [3, 4]}
    w.bridge._answer_outcome(rec, w.bridge._header_of(rec), got)  # noqa: SLF001
    (meta,) = _metas(w, w.post.for_rid(_rid(env)))
    assert meta["refusal"] == "ticket.exists" and meta["seqs"] == [3, 4]

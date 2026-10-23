"""R2 round 3 (orch-52's core review of 6c8f6513 and the Opus delta of round 2): one regression per finding (D2-1 to
D2-7, N1) and the tests for the delta's surviving mutants (A05, A07, A15, A31). The delta's repros
(``scratchpad/r2delta/.../test_delta2_review.py``) asserted the broken behaviour; these assert the fixed one."""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from orch.relay import bridge as B
from orch.relay import framing, keys, rids
from orch.relay.bridge import BridgeLoop, Item
from orch.relay.framing import Refusal
from orch.relay.keys import SealRefused
from orch.wsproto import wire
from orch.wsproto.sign import SignerRefused

from .bridgekit import Phone, World, _ok, open_chunk, ulid
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


def _whoami(w, phone=None, **kw):
    return (phone or w.phone).request(w.wk, {"op": "whoami", "args": {}}, **kw)


# -- D2-1: a swept step-4 record never crashes the loop; one bad item never stops it --------------------------------


def test_a_swept_step_4_refusal_resent_is_silence_never_an_exception(w):
    stranger = Phone()
    env = _whoami(w, stranger)
    assert _metas(w, w.send(env)) == [{"refusal": "not_member"}]
    w.clock.ms += rids.CHUNK_KEEP_MS + 1_000
    w.rids.sweep(w.clock.ms)
    before = len(w.post.posts)
    for _ in range(3):
        assert w.bridge.receive(w.item(env)) is None
        w.clock.ms += B.REPLAY_EVERY_MS + 1
    assert len(w.post.posts) == before  # step 4 needs no answer once its bytes are gone


def test_a_step_4_record_header_names_the_device_not_its_key(w):
    stranger = Phone()
    env = _whoami(w, stranger)
    w.send(env)
    (u,) = [r for r in w.rids.dump()["rid"] if r[0].startswith("u:")]
    rec = w.rids.get(u[0], u[1])
    assert w.bridge._header_of(rec).device.hex() == stranger.device  # noqa: SLF001


def test_rotation_with_an_open_step_4_record_does_not_raise_and_leaves_it_alone(w):
    stranger = Phone()
    w.post.default = "retry"  # the refusal chunk stays attempted
    env = _whoami(w, stranger)
    w.send(env)
    w.clock.ms += rids.CHUNK_KEEP_MS + 1_000
    w.rids.sweep(w.clock.ms)
    w.epochs.rotate()
    w.bridge.rotate()
    (u,) = [r for r in w.rids.dump()["rid"] if r[0].startswith("u:")]
    assert [c.idx for c in w.rids.get(u[0], u[1]).chunks] == [0]


def test_rotation_goes_on_past_one_rid_that_raises(w, monkeypatch):
    envs = [_whoami(w) for _ in range(2)]
    w.post.default = "retry"
    for env in envs:
        w.send(env)
    w.epochs.rotate()
    seen = []
    real = w.bridge.rotate_rid

    def rotate_rid(device, rid):
        seen.append(rid)
        if len(seen) == 1:
            raise RuntimeError("one bad rid")
        real(device, rid)

    monkeypatch.setattr(w.bridge, "rotate_rid", rotate_rid)
    w.bridge.rotate()
    assert len(seen) == 2


class _BoomBridge:
    """A bridge whose receive raises for one item: the responder must acknowledge it and go on."""

    def __init__(self, real, bad: int) -> None:
        self.real, self.bad, self.rids, self.alarms = real, bad, real.rids, []
        self.seen: list[int] = []

    def receive(self, item):
        self.seen.append(item.n)
        if item.n == self.bad:
            raise ValueError("poisoned item")
        return None

    def alarm(self, code, detail):
        self.alarms.append(code)


def test_one_poisoned_item_is_acknowledged_and_the_responder_goes_on_across_a_restart(w):
    br = _BoomBridge(w.bridge, bad=2)
    loop = BridgeLoop(br, session=None, conn=lambda: None, workspace_id=WS)  # type: ignore[arg-type]
    for n in (1, 2, 3):
        loop._queue.put(Item(n, "bridge", "", "", b"x"))  # noqa: SLF001
        loop._queued_bytes += 1  # noqa: SLF001
    loop._queue.put(None)  # noqa: SLF001
    loop._resp_main()  # noqa: SLF001 - returns at the None
    assert br.seen == [1, 2, 3] and br.alarms == ["relay_item_failed"] and loop.error is None
    assert w.rids.cursor() == 3
    w.reopen()  # the cursor is durable: the poisoned item is never polled again
    assert w.rids.cursor() == 3


def test_a_job_that_raises_never_ends_the_job_thread(w):
    class RunBoom:
        rids = w.rids

        def __init__(self):
            self.runs = 0
            self.alarms = []

        def run(self, adm):
            self.runs += 1
            raise RuntimeError("boom")

        def alarm(self, code, detail):
            self.alarms.append(code)

    br = RunBoom()
    loop = BridgeLoop(br, session=None, conn=lambda: None, workspace_id=WS)  # type: ignore[arg-type]
    loop._jobs.put(object())  # noqa: SLF001
    t = threading.Thread(target=loop._job_main)  # noqa: SLF001
    t.start()
    for _ in range(50):
        if br.runs:
            break
        threading.Event().wait(0.02)
    loop._jobs.put(object())  # noqa: SLF001 - still served
    for _ in range(50):
        if br.runs == 2:
            break
        threading.Event().wait(0.02)
    loop.stop()
    t.join(3)
    assert br.runs == 2 and not t.is_alive()


# -- D2-2: a definite step-4 refusal stays definite for the same bytes ----------------------------------------------


def test_a_not_member_refusal_replays_for_the_same_envelope_after_the_device_joins_and_never_runs(w):
    phone = Phone()
    log = ulid(w.clock.ms)
    ev = phone.event(log, "ticket.created", eid=ulid(w.clock.ms), key="DEMO-0009", ticket_type="bug", title="t",
                     owner="p_" + phone.person)  # fmt: skip
    env = phone.request(w.wk, {"op": "new_ticket", "args": {"log": log, "events": [ev]}, "idem": ev["id"]})
    first = w.send(env)
    assert _metas(w, first) == [{"refusal": "not_member"}]
    w.r6.add(phone)  # the member list catches up
    w.clock.ms += 30_000
    again = w.send(env)
    assert again == first and w.host.runs == []  # the same refusal bytes, never a run
    w.clock.ms += rids.CHUNK_KEEP_MS
    w.rids.sweep(w.clock.ms)
    assert w.send(env) == [] and w.host.runs == []  # swept: silence, still never a run
    # a DIFFERENT envelope on that rid (the device's own new request) is not squatted by the record (R2-11)
    m = {"op": "whoami", "args": {}}
    other = phone.request(w.wk, m, rid=bytes.fromhex(_rid(env)), ts_ms=w.clock.ms)
    assert _metas(w, w.send(other)) == [{"status": "ok"}]


# -- D2-3: few devices cannot starve writes, nor reads of devices within their share --------------------------------


def test_heavy_devices_cannot_starve_writes_or_a_light_devices_read(w, monkeypatch):
    monkeypatch.setattr(B, "DEVICE_CHUNK_BYTES", 6 << 20)
    monkeypatch.setattr(B, "CHUNK_BYTES", 12 << 20)
    monkeypatch.setattr(B, "SHARED_CHUNK_BYTES", 8 << 20)
    monkeypatch.setattr(B, "FAIR_CHUNK_BYTES", 4 << 20)
    hostile = [Phone(), Phone()]
    for p in hostile:
        w.r6.add(p)
        for _ in range(2):
            env = p.request(w.wk, {"op": "ticket.text", "args": {"size": 1_900_000}})
            assert _metas(w, w.send(env))[0] == {"status": "ok"}
    # past its fair share a heavy device sees only the shared part: busy
    env = hostile[0].request(w.wk, {"op": "ticket.text", "args": {"size": 10}})
    assert _metas(w, w.send(env)) == [{"refusal": "busy"}]
    # a light device's read still fits the reserve, and a write is never refused for bytes
    light = Phone()
    w.r6.add(light)
    assert _metas(w, w.send(light.request(w.wk, {"op": "ticket.text", "args": {"size": 10}})))[0] == {"status": "ok"}
    meta, _ = w.call(w.new_ticket())
    assert meta == {"status": "ok"}
    monkeypatch.setattr(B, "CHUNK_BYTES", 1 << 20)  # the host budget exhausted: still a write is served
    meta, _ = w.call(w.new_ticket())
    assert meta == {"status": "ok"}
    assert _metas(w, w.send(_whoami(w))) == [{"refusal": "busy"}]  # a read is busy


def test_a_byte_busy_read_does_not_consume_its_seq(w, monkeypatch):
    monkeypatch.setattr(B, "CHUNK_BYTES", 1 << 20)
    env = _whoami(w, seq=7)
    assert _metas(w, w.send(env)) == [{"refusal": "busy"}]
    assert w.rids.window(w.phone.device) == (0, 0)


# -- D2-4: the lock order (store lock, then seal lock, then the RidStore lock) -------------------------------------


def test_nothing_inside_a_seal_span_takes_the_store_lock_or_calls_job(w, monkeypatch):
    """A lock-tracking world: the port's methods hold a (fake) store lock; the epoch source asserts it is never
    called under the store lock; job() asserts it is never called inside a sealing span."""
    tl = threading.local()
    bad: list[str] = []
    store_lock = threading.RLock()

    def depth(name):
        return getattr(tl, name, 0)

    def holding(name, fn):
        def wrapped(*a, **kw):
            setattr(tl, name, depth(name) + 1)
            try:
                return fn(*a, **kw)
            finally:
                setattr(tl, name, depth(name) - 1)

        return wrapped

    for m in ("standing", "read", "submit", "lookup"):
        real = getattr(w.host, m)

        def under_store(*a, _real=real, **kw):
            with store_lock:
                return holding("store", _real)(*a, **kw)

        monkeypatch.setattr(w.host, m, under_store)
    for m in ("current_epoch", "wk", "seal_state", "refusal_key"):
        real = getattr(w.epochs, m)

        def source(*a, _real=real, _m=m, **kw):
            if depth("store"):
                bad.append("source under the store lock: " + _m)
            return _real(*a, **kw)

        monkeypatch.setattr(w.epochs, m, source)
    real_sealing = w.bridge.keys.sealing

    def sealing(signer):
        import contextlib

        @contextlib.contextmanager
        def span():
            with real_sealing(signer) as s:
                tl.seal = depth("seal") + 1
                try:
                    yield s
                finally:
                    tl.seal -= 1

        return span()

    monkeypatch.setattr(w.bridge.keys, "sealing", sealing)
    real_job = w.bridge.job

    def job(fn):
        if depth("seal"):
            bad.append("job() inside a seal span")
        return real_job(fn)

    w.bridge.job = job
    w.call({"op": "whoami", "args": {}})  # read
    m = w.new_ticket()
    w.call(m)  # write
    w.call({"op": "outcome.get", "args": {"idem": m["idem"]}})
    w.send(_whoami(w, Phone()))  # step 4
    env = _whoami(w)
    w.send(env)
    w.send(env)  # replay
    # every _answer_refusal path too (review V15): settle, a refusal after a run, a read's internal, the read expiry
    adm = w.bridge.receive(w.item(_whoami(w)))
    w.bridge._settle(w.rids.get(w.phone.device, adm.header.rid.hex()))  # noqa: SLF001
    w.call(w.new_ticket(title="refuse me"))
    real_read = w.host.read
    w.host.read = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    w.send(_whoami(w))  # internal
    w.host.read = real_read
    w.bridge.receive(w.item(_whoami(w)))  # admitted, never run
    w.post.default = "retry"
    w.send(_whoami(w))
    w.epochs.rotate()
    w.bridge.rotate()
    w.bridge.flush()
    w.clock.ms += B.READ_ADMITTED_MS
    w.bridge.sweep()  # expire_reads: already_done for the stale read
    assert bad == []


def test_the_lock_order_is_written_down():
    doc = keys.__doc__ or ""
    assert "store lock first, then the seal lock, then the RidStore lock" in doc


# -- D2-6: in-memory counts, batched sweep --------------------------------------------------------------------------


def test_the_in_memory_counts_equal_the_file_through_every_transition(w, monkeypatch):
    monkeypatch.setattr(rids, "SWEEP_BATCH", 2)
    sweeps = []
    w.rids._fault = lambda p: sweeps.append(p) if p == "before_sweep" else None  # noqa: SLF001
    for _ in range(3):
        w.call({"op": "ticket.text", "args": {"size": 5000}})
    w.call(w.new_ticket())
    w.send(_whoami(w, Phone()))  # a step-4 refusal
    env = _whoami(w)
    w.post.default = "retry"
    w.send(env)
    w.epochs.rotate()
    w.bridge.rotate()  # replace_unattempted
    c = w.rids.counts()
    assert c["memory"] == c["file"]
    w.clock.ms += rids.CHUNK_KEEP_MS
    w.rids.sweep(w.clock.ms)  # bytes go, in batches of 2
    c = w.rids.counts()
    assert c["memory"] == c["file"] and c["memory"][1] == 0
    w.clock.ms += rids.RID_KEEP_MS
    w.rids.sweep(w.clock.ms)  # tombstones
    c = w.rids.counts()
    assert c["memory"] == c["file"] and c["memory"][0] == 0
    assert len(sweeps) > 6  # several short transactions, not one long one
    w.reopen()
    c = w.rids.counts()
    assert c["memory"] == c["file"]


def test_host_wide_live_counts_come_from_memory_and_match_sql(w):
    for _ in range(4):
        w.call({"op": "whoami", "args": {}})
    w.clock.ms += 1000
    w.call({"op": "whoami", "args": {}})
    db = w.rids._db  # noqa: SLF001 - the oracle
    for since in (0, w.clock.ms - 500, w.clock.ms, w.clock.ms + 1):
        want = db.execute("SELECT COUNT(*) FROM rid WHERE first_ms>=? AND state!='tombstone'", (since,)).fetchone()[0]
        assert w.rids.live(since) == want


def test_an_admitted_rid_older_than_a_day_stays_counted_after_the_sweep(w):
    assert w.bridge.receive(w.item(_whoami(w))) is not None  # admitted, never run
    w.clock.ms += rids.RID_KEEP_MS + 1
    w.rids.sweep(w.clock.ms)
    assert w.rids.live(0) == 1 and w.rids.counts()["memory"][0] == 1


# -- D2-7: classified refusals, never a dead thread; the sealer is private ---------------------------------------------


def test_a_host_code_outside_the_closed_table_is_sealed_internal(w, monkeypatch):
    def read(*a, **kw):
        raise Refusal("unknown_command")

    monkeypatch.setattr(w.host, "read", read)
    assert _metas(w, w.send(_whoami(w))) == [{"refusal": "internal", "retryable": False}]

    def submit(*a, **kw):
        raise Refusal("human_only")

    monkeypatch.setattr(w.host, "submit", submit)
    (meta,) = _metas(w, w.send(w.phone.request(w.wk, w.new_ticket())))
    assert meta == {"refusal": "internal", "retryable": True}  # after a write was admitted: B (ruling C)


def test_a_signer_refusal_is_silence_never_an_exception(w, monkeypatch):
    class Refusing:
        def response_chunk(self, *a, **kw):
            raise SignerRefused("test")

    monkeypatch.setattr(w.bridge, "signer", Refusing())
    assert w.send(_whoami(w)) == []
    assert w.send(_whoami(w, Phone())) == []
    assert w.send(w.phone.request(w.wk, b"\x00\x00\x00\x02{}")) == []


def test_retry_stuck_survives_a_settle_that_raises(w, monkeypatch):
    calls = {"n": 0}

    def fault(point):
        if point == "after_begin" and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("x")

    w.host.fault = fault
    assert w.send(w.phone.request(w.wk, w.new_ticket())) == []

    def settle(rec):
        raise RuntimeError("settle")

    monkeypatch.setattr(w.bridge, "_settle", settle)
    w.clock.ms += B.STUCK_BACKOFF_MS[0]
    w.bridge.retry_stuck()  # never raises


def test_a_settle_whose_lookup_raises_answers_already_done(w, monkeypatch):
    m = w.new_ticket()
    env = w.phone.request(w.wk, m)
    assert w.bridge.receive(w.item(env)) is not None

    def lookup(*a, **kw):
        raise RuntimeError("store gone")

    monkeypatch.setattr(w.host, "lookup", lookup)
    rec = w.rids.get(w.phone.device, _rid(env))
    w.bridge._settle(rec)  # noqa: SLF001
    assert _metas(w, w.post.for_rid(_rid(env))) == [{"refusal": "already_done", "status": "unknown"}]


def test_the_sealer_cannot_be_built_outside_bridge_keys_sealing(w):
    st = w.epochs.seal_state()
    with pytest.raises(TypeError):
        keys._Sealer(object(), w.bridge.keys, w.bridge.signer, st, lambda e: b"")  # noqa: SLF001
    assert not hasattr(keys, "Sealer")
    root = Path(keys.__file__).parent
    builders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_Sealer":
                        builders.append((path.name, fn.name))
    assert builders == [("keys.py", "sealing")]


# -- N1: one POST per rid at a time -------------------------------------------------------------------------------


def test_two_threads_posting_one_rid_post_it_once(w):
    env = _whoami(w)
    gate, entered = threading.Event(), threading.Event()
    posts = []

    def post(device, rid, data):
        posts.append(rid)
        entered.set()
        gate.wait(3)
        return "posted"

    w.bridge.post = post
    assert w.bridge.receive(w.item(env)) is not None
    rec = w.rids.get(w.phone.device, _rid(env))
    h = wire.parse_header(env[: wire.HEADER_LEN])
    adm_thread = threading.Thread(target=lambda: w.bridge._answer_result(rec, h, _ok({})))  # noqa: SLF001
    adm_thread.start()
    assert entered.wait(3)
    w.bridge.flush()  # the aux flush meanwhile: the rid is being posted, so it is left alone
    gate.set()
    adm_thread.join(3)
    assert posts == [_rid(env)]


# -- the delta's survivors ----------------------------------------------------------------------------------------


def test_a31_a_host_pending_record_after_the_bound_is_pending_never_unknown_idem(w, monkeypatch):
    """A31: the host still holds a pending record (it may yet reconcile as landed) past the ULID bound and no rid is
    admitted here: ``outcome.get`` answers ``pending`` (B), never ``outcome.unknown_idem`` (C-review: a re-sign
    could duplicate the event)."""
    idem = ulid(w.clock.ms)
    w.clock.ms += framing.PENDING_NO_RECORD_MS + 60_000
    real = w.host.read

    def read(identity, op, args, recheck):
        if op == "outcome.get":
            return _ok({"state": "pending"})
        return real(identity, op, args, recheck)

    monkeypatch.setattr(w.host, "read", read)
    meta, data = w.call({"op": "outcome.get", "args": {"idem": idem}}, ts_ms=w.clock.ms)
    assert meta == {"status": "ok"} and data["data"] == {"state": "pending"}


def test_a05_a_proto_refusal_missing_its_field_is_refused():
    for code in ("stale_sequence", "stale_timestamp", "stale_epoch", "already_done"):
        with pytest.raises(ValueError):
            framing.refusal_meta(code)


def test_a07_nothing_but_stale_epoch_is_sealed_under_a_burned_epoch(w):
    e = w.epochs.burn()
    h = wire.parse_header(_whoami(w, epoch=e)[: wire.HEADER_LEN])
    with w.bridge.keys.sealing(w.bridge.signer) as s:
        for code, fields in (("already_done", {"status": "unknown"}), ("busy", {}), ("internal", {})):
            with pytest.raises(SealRefused):
                s.seal_refusal(request=h, idx=0, ts_ms=w.clock.ms, code=code, **fields)
        assert s.seal_refusal(request=h, idx=0, ts_ms=w.clock.ms, code="stale_epoch", epoch=e + 1)


def test_a15_step_4_records_respect_the_host_wide_row_caps(w, monkeypatch):
    for _ in range(3):
        w.call({"op": "whoami", "args": {}})
    monkeypatch.setattr(B, "MAX_RECORDS", 3)
    assert w.send(_whoami(w, Phone())) == []
    assert not any(r[0].startswith("u:") for r in w.rids.dump()["rid"])
    monkeypatch.setattr(B, "MAX_RECORDS", 16_384)
    monkeypatch.setattr(B, "MAX_RETAINED_ROWS", 3)
    assert w.send(_whoami(w, Phone())) == []
    assert not any(r[0].startswith("u:") for r in w.rids.dump()["rid"])


# -- round-3 mutation survivors (R33, R37, R39) ----------------------------------------------------------------------


def test_superseding_stored_chunks_lowers_the_byte_counts(w):
    w.rids.record_refusal(device="d" * 32, rid="e" * 32, first_ms=w.clock.ms, req_digest="sha256:x", epoch=2, seq=1,
                          kind="refusal", chunk=b"x" * 1000, window=None)  # fmt: skip
    w.rids.replace_unattempted("d" * 32, "e" * 32, [(0, 2, b"y" * 10)])
    c = w.rids.counts()
    assert c["memory"] == c["file"] and w.rids.chunk_bytes("d" * 32) == 10


def test_the_relay_submit_builder_is_registered_by_importing_outcomes_alone():
    """M1: a fresh process that imports only orch.host.outcomes (what reconcile_all needs) has the builder."""
    import subprocess
    import sys

    code = "import orch.host.outcomes as o; print(o.BUILDERS['relay.submit'].__module__)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
    assert out.stdout.strip() == "orch.host.relaybuild"


def test_an_aux_step_that_raises_is_logged_and_the_session_errors_still_stop(w):
    from orch.relay.session import Fenced

    alarms = []
    w.bridge.alarm = lambda c, d: alarms.append(c)
    loop = BridgeLoop(w.bridge, session=None, conn=lambda: None, workspace_id=WS)  # type: ignore[arg-type]

    def boom():
        raise OSError("disk")

    loop._aux_step("sweep", boom)  # noqa: SLF001 - never raises
    assert alarms == ["relay_item_failed"]

    def fenced():
        raise Fenced("x")

    with pytest.raises(Fenced):
        loop._aux_step("flush", fenced)  # noqa: SLF001

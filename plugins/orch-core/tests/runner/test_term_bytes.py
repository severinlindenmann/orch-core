"""The byte path on the term socket (§12.6.3 to §12.6.5): out frames with offsets, replay, gaps, the control queue,
input ids and lease generations, the rate limit, writer-only resize and the attachment limits."""

from __future__ import annotations

import pytest

from orch.runner import term as termmod
from orch.runner.supervise import SpawnError  # noqa: F401
from tests.runner.termhelp import RUN, FakeLive, Rig, b64, out_bytes, wait_until


@pytest.fixture
def rig():
    r = Rig()
    try:
        yield r
    finally:
        r.close()


def type_(c, gen, iid, data=b"x"):
    c.send({"t": "in", "input_id": iid, "lease_gen": gen, "b64": b64(data)})


def contiguous(frames_):
    pos = None
    for f in frames_:
        if f["t"] != "out":
            continue
        assert len(__import__("base64").b64decode(f["b64"])) <= 64 * 1024
        if pos is not None:
            assert f["off"] == pos
        pos = f["off"] + len(__import__("base64").b64decode(f["b64"]))


def test_ready_carries_offsets_size_and_lease(rig):
    rig.sup.emit(b"hello")
    c = rig.attach("a")
    assert c.ready == {"t": "ready", "start": 0, "offset": 5, "cols": 120, "rows": 40, "writer": False,
                       "lease_gen": 0, "state": "running"}  # fmt: skip


def test_replay_then_live_with_offsets(rig):
    rig.sup.emit(b"abc")
    c = rig.attach("a")
    first = c.until("out")
    assert (first["off"], out_bytes([first])) == (0, b"abc")  # below ready.offset: replay
    rig.sup.emit(b"def")
    live = c.until("out")
    assert (live["off"], out_bytes([live])) == (3, b"def")  # at or above ready.offset: live


def test_replay_from_an_offset(rig):
    rig.sup.emit(b"0123456789")
    c = rig.attach("a", start_from=4)
    f = c.until("out")
    assert (f["off"], out_bytes([f])) == (4, b"456789")


def test_large_output_is_chunked_at_64k_and_contiguous(rig):
    blob = bytes(range(256)) * 800  # 204800 bytes
    rig.sup.emit(blob)
    c = rig.attach("a")
    got = []
    while sum(len(__import__("base64").b64decode(f["b64"])) for f in got) < len(blob):
        got.append(c.until("out"))
    contiguous(got)
    assert out_bytes(got) == blob
    assert got[0]["off"] == 0 and len(got) >= 4


def test_gap_when_behind_the_ring_start():
    r = Rig(ring_size=100_000)
    try:
        r.sup.emit(b"a" * 250_000)  # the ring keeps the last 100000: start = 150000
        c = r.attach("a", start_from=0)
        assert c.ready["start"] == 150_000
        assert c.until("gap") == {"t": "gap", "from": 0, "to": 150_000}
        f = c.until("out")
        assert f["off"] == 150_000
    finally:
        r.close()


def test_gap_when_more_than_512k_behind_the_end(rig):
    rig.sup.emit(b"z" * 900_000)
    c = rig.attach("a", start_from=0)
    gap = c.until("gap")
    assert gap == {"t": "gap", "from": 0, "to": 900_000 - termmod.QUEUE}
    got = []
    total = 0
    while total < termmod.QUEUE:
        f = c.until("out")
        got.append(f)
        total += len(__import__("base64").b64decode(f["b64"]))
    assert got[0]["off"] == gap["to"]
    contiguous(got)


def test_control_frames_are_never_dropped_while_output_overruns(rig):
    w = rig.attach("w", mode="write", take=True)
    gen = w.ready["lease_gen"]
    v = rig.attach("v")  # reads nothing while the writer works and the ring overruns
    for i in range(5):
        w.send({"t": "resize", "lease_gen": gen, "cols": 100 + i, "rows": 30})
    wait_until(lambda: rig.sup.sizes[-1:] == [(104, 30)])  # a run of resizes in one read is applied once
    for _ in range(30):
        rig.sup.emit(b"q" * 60_000)
    rig.listener.end("v", "not_visible")
    seen = v.drain(10)
    sizes = [(f["cols"], f["rows"]) for f in seen if f["t"] == "resize"]
    assert sizes == [(100 + i, 30) for i in range(5)] or sizes[-1] == (104, 30)
    assert seen[-1] == {"t": "end", "reason": "not_visible"}


def test_exit_sends_remaining_output_then_exit_then_end(rig):
    c = rig.attach("a")
    rig.sup.emit(b"bye")
    rig.sup.exit_run(3)
    frames_ = c.drain()
    assert out_bytes(frames_) == b"bye"
    assert [f["t"] for f in frames_][-2:] == ["exit", "end"]
    assert frames_[-2] == {"t": "exit", "code": 3, "signal": None}
    assert frames_[-1] == {"t": "end", "reason": "exited"}


def test_attach_to_an_exited_run_gets_ring_exit_end(rig):
    rig.sup.emit(b"old")
    rig.sup.exit_run(0)
    c = rig.attach("a")
    assert c.ready["state"] == "exited"
    frames_ = c.drain()
    assert out_bytes(frames_) == b"old"
    assert [f["t"] for f in frames_][-2:] == ["exit", "end"]


def test_input_is_written_acked_and_a_repeat_is_not_typed_again(rig):
    c = rig.attach("w", mode="write", take=True)
    gen = c.ready["lease_gen"]
    type_(c, gen, 1, b"one")
    assert c.until("ack")["input_id"] == 1
    type_(c, gen, 1, b"one")  # a retry of the same id
    assert c.until("ack")["input_id"] == 1
    type_(c, gen, 5, b"gap")  # past a gap: refused (throttle), never acknowledged, never typed
    assert c.until("throttle")["retry_ms"] >= 1
    type_(c, gen, 2, b"two")  # the next id
    assert c.until("ack")["input_id"] == 2
    type_(c, gen, 1, b"old")
    assert c.until("ack")["input_id"] == 1
    assert rig.sup.typed == b"onetwo"


def test_partial_writes_are_retried_by_the_runner(rig):
    c = rig.attach("w", mode="write", take=True)
    rig.sup.max_write = 3
    rig.sup.stall = 2
    type_(c, c.ready["lease_gen"], 1, b"0123456789")
    assert c.until("ack")["input_id"] == 1
    assert rig.sup.typed == b"0123456789"


def test_stale_lease_gen_is_refused_with_writer_false(rig):
    c = rig.attach("w", mode="write", take=True)
    gen = c.ready["lease_gen"]
    type_(c, gen + 7, 1, b"bad")
    note = c.until("writer")
    assert note == {"t": "writer", "writer": False, "lease_gen": gen}
    assert rig.sup.typed == b""


def test_read_attachment_input_and_resize_are_dropped(rig):
    c = rig.attach("r")
    type_(c, 0, 1, b"nope")
    c.send({"t": "resize", "lease_gen": 0, "cols": 100, "rows": 30})
    rig.sup.emit(b"ok")
    c.until("out")
    assert rig.sup.typed == b"" and rig.sup.sizes == []
    assert not [f for f in c.collect(0.2) if f["t"] in ("ack", "resize")]


@pytest.mark.parametrize(
    "frame",
    [
        {"t": "in", "input_id": 0, "lease_gen": 1, "b64": "eA=="},
        {"t": "in", "input_id": 1, "lease_gen": 1, "b64": "!!!"},
        {"t": "in", "input_id": 1, "lease_gen": 1, "b64": b64(b"x" * (64 * 1024 + 1))},
        {"t": "in", "input_id": True, "lease_gen": 1, "b64": "eA=="},
        {"t": "resize", "lease_gen": 1, "cols": "x", "rows": 5},
        {"t": "bogus"},
    ],
)
def test_malformed_frames_end_with_protocol(rig, frame):
    c = rig.attach("w", mode="write", take=True)
    c.send(frame)
    assert c.drain()[-1] == {"t": "end", "reason": "protocol"}
    assert rig.sup.typed == b""


def test_input_over_the_rate_is_throttled_not_written_and_resend_works(rig):
    c = rig.attach("w", mode="write", take=True)
    gen = c.ready["lease_gen"]
    chunk = b"k" * 64 * 1024
    for i in range(1, 5):  # the 256 KiB reserve
        type_(c, gen, i, chunk)
        assert c.until("ack")["input_id"] == i
    type_(c, gen, 5, chunk)
    thr = c.until("throttle")
    assert thr["retry_ms"] >= 1
    assert len(rig.sup.typed) == 4 * len(chunk)  # not written, no ack
    import time

    time.sleep(thr["retry_ms"] / 1000 + 0.05)
    type_(c, gen, 5, chunk)  # the same id
    assert c.until("ack")["input_id"] == 5
    assert len(rig.sup.typed) == 5 * len(chunk)


def test_only_the_writers_resize_applies_and_every_viewer_hears_it(rig):
    w = rig.attach("w", mode="write", take=True)
    v = rig.attach("v")
    gen = w.ready["lease_gen"]
    v.send({"t": "resize", "lease_gen": gen, "cols": 50, "rows": 10})
    w.send({"t": "resize", "lease_gen": gen, "cols": 19, "rows": 10})  # out of range
    w.send({"t": "resize", "lease_gen": gen, "cols": 501, "rows": 10})
    w.send({"t": "resize", "lease_gen": gen, "cols": 90, "rows": 30})
    assert w.until("resize") == {"t": "resize", "cols": 90, "rows": 30}
    assert v.until("resize") == {"t": "resize", "cols": 90, "rows": 30}
    assert rig.sup.sizes == [(90, 30)]


def test_lease_busy_taken_and_released_on_close(rig):
    a = rig.attach("a", mode="write", take=True)
    assert a.ready["writer"] and a.ready["lease_gen"] == 1
    v = rig.attach("v")
    busy = rig.attach("b", mode="write", take=False, read_ready=False)
    assert busy.drain() == [{"t": "end", "reason": "writer_busy"}]
    a.close()
    note = v.until("writer")
    assert note == {"t": "writer", "writer": False, "lease_gen": 2}
    nxt = rig.attach("c", mode="write", take=False)  # nobody holds it now
    assert nxt.ready["writer"] is True and nxt.ready["lease_gen"] == 3


def test_a_new_writer_with_take_notifies_the_old_one_and_nudges(rig):
    a = rig.attach("a", mode="write", take=True)
    b = rig.attach("b", mode="write", take=True)
    assert a.until("writer") == {"t": "writer", "writer": False, "lease_gen": 2}
    assert b.ready["writer"] is True and b.ready["lease_gen"] == 2
    assert rig.sup.nudges == 2


def test_four_attachments_per_run(rig):
    cs = [rig.attach(f"a{i}", person=f"p{i}") for i in range(4)]
    assert all(c.ready["t"] == "ready" for c in cs)
    fifth = rig.attach("a5", person="p5", read_ready=False)
    assert fifth.drain() == [{"t": "end", "reason": "attach.limit"}]
    cs[0].close()
    wait_until(lambda: "a0" not in rig.listener.attachments)
    assert rig.attach("a6", person="p6").ready["t"] == "ready"


def test_sixteen_per_person_and_sixty_four_per_runner(rig):
    for i in range(20):
        rig.sup.live[f"run{i}"] = FakeLive()
    # person limit: 16 for one person over four runs, the 17th is refused
    for i in range(16):
        ticket, nonce = rig.allow(f"pp{i}", person="solo", run=f"run{i // 4}")
        c = termmod_client(rig, ticket, nonce)
        assert c.recv()["t"] == "ready"
    ticket, nonce = rig.allow("pp16", person="solo", run="run5")
    assert termmod_client(rig, ticket, nonce).drain() == [{"t": "end", "reason": "attach.limit"}]
    # runner limit: 48 more with distinct persons, four per run, reach 64 in all
    n = 16
    while n < 64:
        ticket, nonce = rig.allow(f"rr{n}", person=f"u{n}", run=f"run{4 + (n - 16) // 4}")
        assert termmod_client(rig, ticket, nonce).recv()["t"] == "ready"
        n += 1
    assert n == 64
    ticket, nonce = rig.allow("over", person="u_over", run="run17")
    assert termmod_client(rig, ticket, nonce).drain() == [{"t": "end", "reason": "attach.limit"}]


def termmod_client(rig, ticket, nonce):
    from tests.runner.termhelp import Client

    c = Client(rig.listener.path)
    rig.clients.append(c)
    assert c.recv()["t"] == "hello"
    c.send({"t": "attach", "ticket": ticket, "nonce": nonce})
    return c


def test_bad_and_replayed_tickets_close_with_ticket_invalid(rig):
    ticket, nonce = rig.allow("a")
    c = termmod_client(rig, ticket, nonce)
    assert c.recv()["t"] == "ready"
    again = termmod_client(rig, ticket, nonce)
    assert again.drain() == [{"t": "end", "reason": "ticket_invalid"}]
    wrong = rig.allow("b")
    c2 = termmod_client(rig, wrong[0], "0" * 32)  # right ticket, wrong nonce
    assert c2.drain() == [{"t": "end", "reason": "ticket_invalid"}]


def test_run_stop_ends_every_attachment_of_the_run(rig):
    a, b = rig.attach("a"), rig.attach("b")
    assert rig.listener.end_run(RUN, "run.stopped") == 2
    assert a.drain()[-1] == {"t": "end", "reason": "run.stopped"}
    assert b.drain()[-1] == {"t": "end", "reason": "run.stopped"}

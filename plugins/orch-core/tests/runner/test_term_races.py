"""The term socket's interleavings (PR #443 review): output after a revocation, input past a throttled frame, a
disconnect during a stalled PTY write, read and write deadlines, ``ready`` before any other frame, the bounded queues
and the client's handling of malformed runner frames."""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from orch.host import frames
from orch.runner import term
from orch.runner.termclient import TermClient
from tests.runner.termhelp import GEN, RUN, Client, Rig, b64, out_bytes, wait_until


@pytest.fixture
def rig():
    r = Rig()
    try:
        yield r
    finally:
        r.close()


def writer(rig: Rig, att: str = "att1"):
    c = rig.attach(att, mode="write", take=True)
    assert c.ready["writer"] is True
    return c, c.ready["lease_gen"]


def type_(c, gen, iid, data=b"x"):
    c.send({"t": "in", "input_id": iid, "lease_gen": gen, "b64": b64(data)})


# -- Codex 1 / Opus L2: no output byte after a revocation ------------------------------------------------------------


def test_a_revocation_between_the_check_and_the_send_sends_no_output(rig, monkeypatch):
    """Deterministic: the revocation lands while the output frame is being encoded, i.e. after ``_step``'s first
    check and before the bytes are written (the old window). The send itself is checked under the lock: nothing."""
    c = rig.attach("v1")
    real = frames.encode
    fired = []

    def encode(frame):
        if frame.get("t") == "out" and not fired:
            fired.append(1)
            rig.listener.end("v1", "cancelled")  # another thread's revocation, in effect before the send
        return real(frame)

    monkeypatch.setattr(frames, "encode", encode)
    rig.sup.emit(b"secret")
    tail = c.drain()
    assert fired
    assert out_bytes(tail) == b""
    assert tail[-1] == {"t": "end", "reason": "cancelled"}


def test_a_frame_cut_short_by_a_revocation_is_never_completed(rig, monkeypatch):
    """A revocation between two partial sends of one output frame: the rest never leaves, the socket is shut."""
    c = rig.attach("v1")
    real = term._try_send
    calls = []

    def try_send(conn, view):
        calls.append(len(view))
        n = real(conn, view[:100])  # a partial send
        rig.listener.attachments["v1"].revoked = True  # a revocation right after it, before the next check
        return n

    monkeypatch.setattr(term, "_try_send", try_send)
    rig.sup.emit(b"x" * 5000)
    got = b""
    c.sock.settimeout(3)
    while True:
        try:
            part = c.sock.recv(65536)
        except OSError:
            break
        if not part:
            break
        got += part
    assert len(got) <= 100  # only what was sent before the revocation
    assert len(calls) == 1


# -- Codex 2 / Opus L1: input is never lost silently ------------------------------------------------------------------


def test_a_frame_past_a_throttled_one_is_refused_not_acknowledged(rig):
    c, gen = writer(rig)
    chunk = b"k" * 64 * 1024
    for i in range(1, 5):  # the 256 KiB reserve
        type_(c, gen, i, chunk)
        assert c.until("ack")["input_id"] == i
    type_(c, gen, 5, chunk)  # throttled
    thr = c.until("throttle")
    type_(c, gen, 6, b"tail")  # short: it would fit, but 5 is missing
    assert c.until("throttle")["retry_ms"] >= 1
    assert len(rig.sup.typed) == 4 * len(chunk)
    time.sleep(thr["retry_ms"] / 1000 + 0.05)
    type_(c, gen, 5, chunk)
    assert c.until("ack")["input_id"] == 5
    type_(c, gen, 6, b"tail")
    assert c.until("ack")["input_id"] == 6
    assert rig.sup.typed == chunk * 5 + b"tail"


def _client(rig: Rig, mode: str = "write", take: bool = True) -> TermClient:
    ticket, nonce = rig.allow("tc", mode=mode, take=take)
    answer = {"runner": {"uid": os.getuid(), "pid": os.getpid(), "gen": GEN}, "socket": str(rig.listener.path),
              "ticket": ticket, "nonce": nonce, "lease_gen": 0}  # fmt: skip
    t = TermClient(rig.dir, answer)
    t.connect()
    return t


def test_the_client_sends_one_frame_at_a_time_and_a_throttled_chunk_keeps_its_place(rig, monkeypatch):
    monkeypatch.setattr(term, "RATE_SECOND", 64 * 1024)  # a reserve of one chunk: the second one is throttled
    t = _client(rig)
    try:
        data = b"a" * (64 * 1024) + b"b" * (64 * 1024) + b"tail"  # one fits, a throttled full chunk, a short tail
        ids = t.send_input(data)
        assert ids == [1, 2, 3]
        assert t.inflight == 1  # only the first is on the wire
        deadline = time.monotonic() + 10
        throttled = 0
        while t.unacked:
            assert time.monotonic() < deadline, t.unacked.keys()
            try:
                frame = t.recv(0.05)
            except TimeoutError:
                frame = None
            throttled += bool(frame and frame["t"] == "throttle")
            t.pump()
        assert throttled >= 1
        assert rig.sup.typed == data  # in order, nothing skipped
    finally:
        t.close()


# -- Codex 3: a disconnect interrupts a stalled PTY write ------------------------------------------------------------


def test_a_disconnect_cancels_a_write_into_a_full_pty(rig):
    c, gen = writer(rig)
    rig.sup.stall = 10**9  # the PTY never takes a byte
    type_(c, gen, 1, b"stuck")
    time.sleep(0.2)
    assert "att1" in rig.listener.attachments
    c.close()
    wait_until(lambda: "att1" not in rig.listener.attachments, 2.0)
    assert rig.listener.leases[RUN][0] is None  # the lease is free again
    assert rig.sup.typed == b""


def test_a_resize_read_ahead_during_a_stalled_write_is_applied_after_it(rig):
    c, gen = writer(rig)
    rig.sup.stall = 20  # about 100 ms of a full PTY
    type_(c, gen, 1, b"first")
    c.send({"t": "resize", "lease_gen": gen, "cols": 100, "rows": 30})
    assert c.until("ack")["input_id"] == 1
    c.until("resize")
    assert rig.sup.typed == b"first" and rig.sup.sizes == [(100, 30)]


def test_too_much_read_ahead_during_a_stalled_write_ends_with_overflow(rig, monkeypatch):
    monkeypatch.setattr(term, "INBOX_MAX", 4)
    c, gen = writer(rig)
    rig.sup.stall = 10**9
    type_(c, gen, 1, b"stuck")
    time.sleep(0.1)  # the write is stalled: what arrives now is read-ahead
    for _ in range(10):
        c.send({"t": "resize", "lease_gen": gen, "cols": 100, "rows": 30})
    assert c.drain()[-1] == {"t": "end", "reason": "overflow"}
    wait_until(lambda: "att1" not in rig.listener.attachments)
    assert rig.sup.typed == b"" and rig.sup.sizes == []


def _burst(n: int, gen: int) -> bytes:
    """``n`` small resize frames in one ``sendall`` (one or a few ``recv`` at the runner), each a new size."""
    return b"".join(frames.encode({"t": "resize", "lease_gen": gen, "cols": 100 + i % 2, "rows": 30}) for i in range(n))


def test_a_coalesced_burst_of_small_frames_never_ends_a_healthy_attachment(rig):
    """PR #443 delta D3: more than INBOX_MAX frames in one read, nothing stalled: handled one by one, no overflow."""
    c, gen = writer(rig)
    n = term.INBOX_MAX + 6
    c.sock.sendall(_burst(n, gen))
    type_(c, gen, 1, b"after")
    assert c.until("ack")["input_id"] == 1
    assert "att1" in rig.listener.attachments
    assert rig.sup.typed == b"after"
    assert rig.sup.sizes and rig.sup.sizes[-1] == (100 + (n - 1) % 2, 30)  # the last size wins
    assert len(rig.sup.sizes) < n  # a run of resizes in one read is applied once


@pytest.mark.parametrize(
    "bad", [{"cols": 19, "rows": 30}, {"cols": 100, "rows": 201}, {"cols": 90, "rows": 25, "stale": True}]
)
def test_an_ineligible_resize_never_supersedes_a_valid_one_in_the_same_read(rig, bad):
    """PR #443 Codex delta 2: (100,30) then an out-of-range or stale-lease resize in one read applies the valid one."""
    c, gen = writer(rig)
    bad = dict(bad)
    lease = gen + 1000 if bad.pop("stale", False) else gen
    c.sock.sendall(
        frames.encode({"t": "resize", "lease_gen": gen, "cols": 100, "rows": 30})
        + frames.encode({"t": "resize", "lease_gen": lease, **bad})
    )
    type_(c, gen, 1, b"after")
    assert c.until("ack")["input_id"] == 1
    assert rig.sup.sizes == [(100, 30)]
    assert "att1" in rig.listener.attachments


def test_a_flood_of_small_frames_without_a_stall_stays_bounded(rig, monkeypatch):
    """Commit-scan follow-up to D3: a peer that floods small frames with no stalled write. The runner buffers at most
    one read's frames (it reads again only once they are handled), applies each run of resizes once, and keeps
    serving: no crash, no overflow of a healthy attachment."""
    c, gen = writer(rig)
    v = rig.attach("v1")  # a viewer that reads: gets the resize notes
    listener = rig.listener
    real = listener._read_some
    seen = {"max": 0, "reads": 0}

    def read_some(att, wait, limit=None):
        ok = real(att, wait, limit)
        seen["reads"] += 1
        seen["max"] = max(seen["max"], len(att.inbox))
        return ok

    monkeypatch.setattr(listener, "_read_some", read_some)
    total = 0
    for _ in range(40):
        c.sock.sendall(_burst(500, gen))
        total += 500
    type_(c, gen, 1, b"done")
    assert c.until("ack", timeout=10)["input_id"] == 1
    smallest = len(frames.encode({"t": "resize", "lease_gen": gen, "cols": 100, "rows": 30}))
    assert seen["max"] <= term.READ_BYTES // smallest + 1  # never more than one read's frames
    assert len(rig.sup.sizes) <= seen["reads"] < total  # at most one resize per read
    assert "att1" in listener.attachments and "v1" in listener.attachments
    assert len(listener.attachments["v1"].ctl) <= term.CTL_MAX
    assert v.until("resize")["t"] == "resize"


def test_a_frame_longer_than_any_valid_one_ends_with_protocol_before_it_is_buffered(rig):
    c, gen = writer(rig)
    c.sock.sendall((10 * 1024 * 1024).to_bytes(4, "big") + b"{" * 1000)  # a 10 MiB frame announced
    assert c.drain()[-1] == {"t": "end", "reason": "protocol"}
    wait_until(lambda: "att1" not in rig.listener.attachments)


def test_a_coalesced_burst_during_a_stalled_write_ends_with_overflow(rig):
    c, gen = writer(rig)
    rig.sup.stall = 10**9
    type_(c, gen, 1, b"stuck")
    time.sleep(0.1)  # the write is stalled: what arrives now is read-ahead
    c.sock.sendall(_burst(term.INBOX_MAX + 6, gen))
    assert c.drain()[-1] == {"t": "end", "reason": "overflow"}
    assert rig.sup.typed == b"" and rig.sup.sizes == []


def test_an_overflow_releases_the_write_lease_at_once(rig, monkeypatch):
    c, gen = writer(rig)
    v = rig.attach("v1")
    att = rig.listener.attachments["att1"]
    monkeypatch.setattr(rig.listener, "_finish", lambda a, reason: None)  # the end and the drop do not happen here
    rig.listener._overflow(att)
    assert rig.listener.leases[RUN][0] is None and att.writer is False
    assert "att1" in rig.listener.attachments  # still registered: released before the drop, not by it
    assert v.until("writer")["writer"] is False


# -- Codex 4: separate read and write deadlines on one non-blocking socket ------------------------------------------


def test_an_idle_viewer_stays_attached(rig, monkeypatch):
    monkeypatch.setattr(term, "SEND_TIMEOUT", 0.3)
    c, gen = writer(rig)
    time.sleep(1.0)  # sends nothing for longer than the send deadline
    assert "att1" in rig.listener.attachments
    type_(c, gen, 1, b"later")
    assert c.until("ack")["input_id"] == 1
    rig.sup.emit(b"out")
    assert out_bytes([c.until("out")]) == b"out"


def test_a_viewer_that_does_not_read_is_dropped_after_the_send_deadline(rig, monkeypatch):
    monkeypatch.setattr(term, "SEND_TIMEOUT", 0.3)
    rig.attach("v1")  # a viewer that never reads
    for _ in range(40):
        rig.sup.emit(b"y" * 60000)  # far more than the socket buffers hold
    wait_until(lambda: "v1" not in rig.listener.attachments, 5.0)


# -- Codex 5: ``ready`` is the first frame, whatever races the attach ------------------------------------------------


class _HookLock:
    """The listener's lock, with a callback after each release (outside the lock)."""

    def __init__(self, hook) -> None:
        self.inner = threading.Lock()
        self.hook = hook

    def __enter__(self):
        self.inner.acquire()
        return self

    def __exit__(self, *exc):
        self.inner.release()
        self.hook()
        return False


def test_ready_precedes_a_resize_that_races_the_registration(rig):
    a, gen = writer(rig, "a")
    fired = threading.Event()
    once = threading.Lock()

    def hook():
        if fired.is_set() or "b" not in rig.listener.attachments or not once.acquire(blocking=False):
            return
        fired.set()  # right after b is visible, before its handler thread goes on: the writer resizes
        a.send({"t": "resize", "lease_gen": gen, "cols": 101, "rows": 31})
        deadline = time.monotonic() + 3
        while (101, 31) not in rig.sup.sizes and time.monotonic() < deadline:
            time.sleep(0.005)

    rig.listener.lock = _HookLock(hook)
    b = rig.attach("b", read_ready=False)
    first = b.recv()
    assert fired.is_set() and (101, 31) in rig.sup.sizes
    assert first["t"] == "ready"
    assert b.until("resize") == {"t": "resize", "cols": 101, "rows": 31}


def test_ready_precedes_a_takeover_that_races_the_registration(rig):
    fired = threading.Event()
    once = threading.Lock()
    clients: list[Client] = []

    def hook():
        if fired.is_set() or "w1" not in rig.listener.attachments or not once.acquire(blocking=False):
            return
        fired.set()
        clients.append(rig.attach("w2", mode="write", take=True))  # takes the lease right after w1 registered

    rig.listener.lock = _HookLock(hook)
    w1 = rig.attach("w1", mode="write", take=True, read_ready=False)
    first = w1.recv()
    assert fired.is_set()
    assert first["t"] == "ready" and first["writer"] is True
    note = w1.until("writer")
    assert note["writer"] is False and note["lease_gen"] > first["lease_gen"]


# -- Opus L3 and L4 (runner side): bounded queues -------------------------------------------------------------------


def test_the_control_queue_is_bounded(rig, monkeypatch):
    monkeypatch.setattr(term, "CTL_MAX", 8)
    c, gen = writer(rig)
    for _ in range(40):
        rig.sup.emit(b"z" * 60000)  # the sender blocks on a viewer that does not read
    time.sleep(0.2)
    for i in range(1, 30):  # each queues an ack the sender cannot deliver
        type_(c, gen, i, b"1")
    wait_until(lambda: "att1" not in rig.listener.attachments, 5.0)
    assert len(rig.sup.typed) < 29  # input stopped at the overflow


def test_pending_capabilities_are_bounded(rig, monkeypatch):
    monkeypatch.setattr(term, "MAX_ALLOWS", 3)
    for i in range(3):
        rig.allow(f"p{i}")
    with pytest.raises(term.TicketError) as exc:
        rig.allow("p3")
    assert exc.value.code == "attach.limit"


def test_a_read_attachments_input_is_not_decoded(rig):
    c = rig.attach("r")
    c.send({"t": "in", "input_id": 1, "lease_gen": 0, "b64": "!!!not base64!!!"})  # dropped before decoding
    rig.sup.emit(b"still")
    assert out_bytes([c.until("out")]) == b"still"


# -- Opus L5: the client ends with ``protocol`` on a malformed runner frame -----------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        ["not", "a", "dict"],
        {"t": "ack"},
        {"t": "ack", "input_id": "1"},
        {"t": "writer", "writer": 1, "lease_gen": 2},
        {"t": "throttle", "retry_ms": None},
        {"t": "end", "reason": 7},
        {"t": "out", "off": -1, "b64": ""},
    ],
)
def test_the_client_ends_with_protocol_on_a_malformed_frame(frame):
    ours, theirs = socket.socketpair()
    try:
        t = TermClient("/nonexistent", {"lease_gen": 0})
        t.sock = ours
        data = frames.encode(frame) if isinstance(frame, dict) else _raw(frame)
        theirs.sendall(data)
        assert t.recv(2) is None
        assert t.ended == "protocol"
        assert t.sock is None
    finally:
        theirs.close()
        ours.close()


def _raw(value) -> bytes:
    import json

    body = json.dumps(value).encode()
    return len(body).to_bytes(4, "big") + body


def test_the_client_raises_term_ended_on_send_after_a_protocol_error():
    from orch.runner.termclient import TermEnded

    ours, theirs = socket.socketpair()
    try:
        t = TermClient("/nonexistent", {"lease_gen": 0})
        t.sock, t.writer = ours, True
        theirs.sendall(frames.encode({"t": "ack"}))
        assert t.recv(2) is None and t.ended == "protocol"
        with pytest.raises(TermEnded):
            t.resize(100, 30)
        with pytest.raises(TermEnded):
            t.send({"t": "resize", "lease_gen": 0, "cols": 100, "rows": 30})
        assert t.send_input(b"x") == [1]  # queued, never sent (and reported as lost)
        assert t.lost == [1]
    finally:
        theirs.close()
        ours.close()


def test_a_stream_cut_mid_frame_ends_the_client_with_closed():
    """A revocation that cuts an output frame short: the runner shuts the socket (the frame cannot be completed
    without more of the revoked output, and nothing can follow a partial frame). The client ends with ``closed``."""
    ours, theirs = socket.socketpair()
    try:
        t = TermClient("/nonexistent", {"lease_gen": 0})
        t.sock = ours
        theirs.sendall(frames.encode({"t": "out", "off": 0, "b64": "c2VjcmV0"})[:7])
        theirs.close()
        assert t.recv(2) is None
        assert t.ended == "closed"
    finally:
        ours.close()


def test_a_send_to_a_stream_the_runner_already_shut_is_term_ended(tmp_path):
    """A frame sent after the runner shut the stream raises ``TermEnded`` (not ``BrokenPipeError``), and the end
    frame the runner sent before shutting stays readable."""
    import socket

    import pytest

    from orch.host import frames
    from orch.runner.termclient import TermClient, TermEnded

    a, b = socket.socketpair()
    t = TermClient(tmp_path, {"ticket": "x", "nonce": "y"})
    t.sock = a
    b.sendall(frames.encode({"t": "end", "reason": "cancelled"}))
    b.close()
    with pytest.raises(TermEnded):
        for _ in range(64):  # the first sendall after the close may still be buffered by the kernel
            t.send({"t": "resize", "lease_gen": 0, "cols": 80, "rows": 24})
    assert t.recv(1.0) == {"t": "end", "reason": "cancelled"} or t.ended == "cancelled"
    t.close()

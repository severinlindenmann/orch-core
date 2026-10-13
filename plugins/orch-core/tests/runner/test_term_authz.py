"""Authorization is re-checked at use (H11b security): after the host ends, downgrades or restarts an attachment, the
next byte in either direction is refused and the attachment closes."""

from __future__ import annotations

import pytest

from tests.runner.termhelp import RUN, Rig, b64, out_bytes, wait_until


@pytest.fixture
def rig():
    r = Rig()
    try:
        yield r
    finally:
        r.close()


def writer(rig: Rig, att: str = "att1", **kw):
    c = rig.attach(att, mode="write", take=True, **kw)
    assert c.ready["writer"] is True
    return c, c.ready["lease_gen"]


def type_(c, gen, iid, data=b"x"):
    c.send({"t": "in", "input_id": iid, "lease_gen": gen, "b64": b64(data)})


def test_baseline_input_is_written_and_acked(rig):
    c, gen = writer(rig)
    type_(c, gen, 1, b"ls\n")
    assert c.until("ack")["input_id"] == 1
    assert rig.sup.typed == b"ls\n"


def test_cancel_between_frames_refuses_next_input_and_closes(rig):
    c, gen = writer(rig)
    type_(c, gen, 1, b"a")
    c.until("ack")
    assert rig.listener.end("att1", "cancelled")
    type_(c, gen, 2, b"b")  # may race the close: it must never reach the PTY
    tail = c.drain()
    assert tail[-1] == {"t": "end", "reason": "cancelled"}
    assert rig.sup.typed == b"a"


def test_epoch_bump_between_frames_refuses_input_and_closes(rig):
    c, gen = writer(rig)
    type_(c, gen, 1, b"a")
    c.until("ack")
    rig.listener.host_ready(2)  # a new host connection: the old epoch's attachments end
    type_(c, gen, 2, b"b")
    tail = c.drain()
    assert tail[-1] == {"t": "end", "reason": "host.restarting"}
    assert rig.sup.typed == b"a"
    assert rig.listener.attachments == {}


def test_attachment_not_in_table_is_refused_even_if_the_connection_lives(rig):
    """The structural property: the check is against the table and epoch at use, not a flag set at consumption."""
    c, gen = writer(rig)
    att = rig.listener.attachments["att1"]
    with rig.listener.lock:  # the host's view moved on without anybody telling this thread yet
        del rig.listener.attachments["att1"]
    assert rig.listener._authorized(att) is False
    assert rig.listener._authorized(att, gen) is False
    type_(c, gen, 1, b"zzz")
    assert c.drain()[-1]["t"] == "end"
    assert rig.sup.typed == b""


def test_host_lost_ends_with_host_restarting_and_stops_output(rig):
    c = rig.attach("v1")
    rig.sup.emit(b"one")
    assert out_bytes([c.until("out")]) == b"one"
    rig.listener.host_lost()
    rig.sup.emit(b"two")
    tail = c.drain()
    assert tail[-1] == {"t": "end", "reason": "host.restarting"}
    assert b"two" not in out_bytes(tail)


def test_output_stops_after_cancel(rig):
    c = rig.attach("v1")
    rig.sup.emit(b"before")
    c.until("out")
    rig.listener.end("v1", "not_visible")
    for i in range(20):
        rig.sup.emit(b"after%d" % i)
    tail = c.drain()
    assert tail[-1] == {"t": "end", "reason": "not_visible"}
    assert out_bytes(tail) == b""


def test_take_after_revocation_is_refused(rig):
    """A capability the host revoked (cancel) or whose epoch ended never takes the lease."""
    ticket, nonce = rig.allow("att9", mode="write", take=True)
    rig.listener.end("att9", "cancelled")
    from tests.runner.termhelp import Client

    c = Client(rig.listener.path)
    rig.clients.append(c)
    assert c.recv()["t"] == "hello"
    c.send({"t": "attach", "ticket": ticket, "nonce": nonce})
    assert c.drain() == [{"t": "end", "reason": "ticket_invalid"}]
    assert rig.listener.leases.get(RUN, (None, 0))[0] is None

    ticket, nonce = rig.allow("att10", mode="write", take=True)
    rig.listener.host_ready(2)
    c = Client(rig.listener.path)
    rig.clients.append(c)
    c.recv()
    c.send({"t": "attach", "ticket": ticket, "nonce": nonce})
    assert c.drain() == [{"t": "end", "reason": "ticket_invalid"}]
    assert rig.listener.leases.get(RUN, (None, 0))[0] is None


def test_cancel_between_consume_and_take_never_registers(rig):
    """attach.cancel arrives after the ticket was consumed but before the attachment exists."""
    ticket, nonce = rig.allow("att11", mode="write", take=True)
    real = rig.listener.info
    calls = []

    def info(run):
        calls.append(run)
        if len(calls) == 1:  # right after consume
            rig.listener.end("att11", "cancelled")
        return real(run)

    rig.listener.info = info
    from tests.runner.termhelp import Client

    c = Client(rig.listener.path)
    rig.clients.append(c)
    c.recv()
    c.send({"t": "attach", "ticket": ticket, "nonce": nonce})
    assert c.drain() == [{"t": "end", "reason": "cancelled"}]
    assert rig.listener.attachments == {}
    assert rig.listener.leases.get(RUN, (None, 0))[0] is None


def test_resize_after_downgrade_is_refused(rig):
    c, gen = writer(rig)
    c.send({"t": "resize", "lease_gen": gen, "cols": 100, "rows": 30})
    c.until("resize")
    assert rig.sup.sizes == [(100, 30)]
    assert rig.listener.downgrade("att1")
    note = c.until("writer")
    assert note["writer"] is False
    c.send({"t": "resize", "lease_gen": gen, "cols": 90, "rows": 20})
    type_(c, gen, 1, b"nope")
    c.until("writer")  # refused with writer:false, not written
    assert rig.sup.sizes == [(100, 30)]
    assert rig.sup.typed == b""
    # still attached, read-only: output keeps flowing
    rig.sup.emit(b"hello")
    assert b"hello" in out_bytes([c.until("out")])


def test_lease_taken_by_another_refuses_old_writer_but_keeps_it_attached(rig):
    a, gen_a = writer(rig, "a")
    b = rig.attach("b", mode="write", take=True)
    assert a.until("writer")["writer"] is False
    type_(a, gen_a, 1, b"stale")
    assert a.until("writer")["writer"] is False
    assert rig.sup.typed == b""
    type_(b, b.ready["lease_gen"], 1, b"fresh")
    assert b.until("ack")["input_id"] == 1
    assert rig.sup.typed == b"fresh"


def test_revoke_mid_partial_write_stops_the_retry_loop(rig):
    """Re-checked on every retry of a partial write, not once per frame."""
    c, gen = writer(rig)
    rig.sup.max_write = 1
    calls = []

    def hook():
        calls.append(1)
        if len(calls) == 2:
            rig.listener.end("att1", "cancelled")

    rig.sup.before_write = hook
    type_(c, gen, 1, b"abcdef")
    tail = c.drain()
    assert tail[-1] == {"t": "end", "reason": "cancelled"}
    assert not any(f.get("t") == "ack" for f in tail)
    assert len(rig.sup.typed) <= 2


def test_end_closes_promptly_when_viewer_does_not_read(rig):
    c = rig.attach("v1")
    for _ in range(40):
        rig.sup.emit(b"x" * 60000)
    wait_until(lambda: True)
    rig.listener.end("v1", "cancelled")
    wait_until(lambda: "v1" not in rig.listener.attachments)
    assert c.drain(5)[-1] == {"t": "end", "reason": "cancelled"}

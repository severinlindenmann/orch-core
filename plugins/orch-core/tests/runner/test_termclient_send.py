"""``TermClient.send`` has its own bounded write deadline, independent of ``recv``'s timeout.

An ``ack`` read by ``recv(timeout)`` puts the next input frame on the wire from inside ``recv``; that write must not
inherit ``recv``'s short timeout (a slow runner would end a multi-frame paste), and a runner that stops reading must
not block it forever: after ``send_timeout`` the stream ends with ``send_timeout``.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from orch.host import frames
from orch.runner.termclient import CHUNK, TermClient, TermEnded


def _client(sock: socket.socket, **kw) -> TermClient:
    c = TermClient("/nonexistent", {"lease_gen": 0}, **kw)
    c.sock = sock
    c.writer = True
    return c


def _pair() -> tuple[socket.socket, socket.socket]:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    for s in (a, b):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    return a, b


class SlowRunner(threading.Thread):
    """Reads 2 KiB every 5 ms (a 64 KiB frame takes ~0.16 s to drain), acks each complete ``in`` frame; after
    ``stall_after`` acks it stops reading altogether."""

    def __init__(self, sock: socket.socket, stall_after: int | None = None) -> None:
        super().__init__(daemon=True)
        self.sock, self.stall_after = sock, stall_after
        self.acked: list[int] = []
        self.stop = threading.Event()

    def run(self) -> None:
        dec = frames.Decoder()
        self.sock.settimeout(0.05)
        while not self.stop.is_set():
            if self.stall_after is not None and len(self.acked) >= self.stall_after:
                time.sleep(0.01)
                continue
            try:
                data = self.sock.recv(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                return
            for frame in dec.feed(data):
                if isinstance(frame, dict) and frame.get("t") == "in":
                    self.acked.append(frame["input_id"])
                    self.sock.sendall(frames.encode({"t": "ack", "input_id": frame["input_id"]}))
            time.sleep(0.005)


def _pump(client: TermClient, until, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < deadline, "not done in time"
        try:
            if client.recv(0.02) is None:
                return
        except TimeoutError:
            pass


def test_a_multi_frame_paste_survives_a_slow_runner_under_a_short_recv_timeout():
    mine, theirs = _pair()
    runner = SlowRunner(theirs)
    runner.start()
    client = _client(mine)
    try:
        ids = client.send_input(b"x" * (4 * CHUNK))
        assert ids == [1, 2, 3, 4]
        _pump(client, lambda: not client.unacked)
        assert client.ended is None, client.ended
        assert runner.acked == [1, 2, 3, 4] and client.lost == []
    finally:
        runner.stop.set()
        client.close()
        theirs.close()


def test_a_runner_that_stops_reading_ends_the_stream_with_send_timeout_from_inside_recv():
    mine, theirs = _pair()
    runner = SlowRunner(theirs, stall_after=1)
    runner.start()
    client = _client(mine, send_timeout=1.5)  # > one frame's drain (~0.16 s): the stall is hit inside recv
    try:
        client.send_input(b"y" * (3 * CHUNK))
        started = time.monotonic()
        _pump(client, lambda: client.ended is not None)
        assert client.ended == "send_timeout"
        assert time.monotonic() - started < 5
        assert 1 not in client.lost and client.lost == [2, 3]
        assert client.recv(0.01) is None  # the cut frame can never be completed: the socket is gone
        with pytest.raises(TermEnded):
            client.resize(80, 24)
    finally:
        runner.stop.set()
        client.close()
        theirs.close()


def test_a_direct_send_to_a_stalled_runner_raises_termended_after_the_deadline():
    mine, theirs = _pair()  # nobody reads `theirs`
    client = _client(mine, send_timeout=0.2)
    try:
        started = time.monotonic()
        with pytest.raises(TermEnded):
            client.send_input(b"w" * CHUNK)
        assert 0.15 < time.monotonic() - started < 3
        assert client.ended == "send_timeout" and client.sock is None
    finally:
        client.close()
        theirs.close()


def test_send_leaves_the_socket_timeout_as_recv_set_it():
    mine, theirs = _pair()
    client = _client(mine)
    try:
        mine.settimeout(0.7)
        client.send({"t": "resize", "lease_gen": 0, "cols": 80, "rows": 24})
        assert mine.gettimeout() == 0.7
    finally:
        client.close()
        theirs.close()

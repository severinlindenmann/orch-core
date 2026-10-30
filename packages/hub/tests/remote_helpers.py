"""Fakes for the hub's stream router and endpoint tests: a link port, a client sink, an in-memory connection."""

from __future__ import annotations

import threading
import time
from typing import Any

from orch_hub.contract.canon import b64u
from orch_hub.link import framing
from orch_hub.link.tls import LinkClosed, LinkTimeout

HUB = "ab" * 16
WS_A = "11" * 16
WS_B = "22" * 16
DEV = "rd_" + "aa" * 16
DEV2 = "rd_" + "bb" * 16
CODE = "rc_" + "cc" * 16


class FakePort:
    """What the router sees of one link: ``send_request`` records the h2w frame and returns its seq."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str, dict[str, Any]]] = []
        self.seq = 0
        self.fail = fail
        self.lock = threading.Lock()

    def send_request(self, op: str, body: dict[str, Any]) -> int:
        if self.fail:
            raise OSError("link is not current")
        with self.lock:
            self.seq += 1
            self.sent.append((self.seq, op, body))
            return self.seq

    def ops(self, op: str) -> list[tuple[int, dict[str, Any]]]:
        with self.lock:
            return [(s, b) for s, o, b in self.sent if o == op]

    def wait_op(self, op: str, n: int = 1, timeout: float = 5.0) -> list[tuple[int, dict[str, Any]]]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = self.ops(op)
            if len(got) >= n:
                return got
            time.sleep(0.005)
        raise AssertionError(f"no {op} x{n} on the port")


class FakeSink:
    def __init__(self, accept: bool = True) -> None:
        self.frames: list[bytes] = []
        self.ended: list[str] = []
        self.accept = accept

    def offer(self, data: bytes) -> bool:
        if not self.accept:
            return False
        self.frames.append(data)
        return True

    def end(self, reason: str) -> None:
        self.ended.append(reason)


def data_frame(sid: str, seq: int, data: bytes) -> dict[str, Any]:
    return {"op": "stream_data", "body": {"stream_id": sid, "seq": seq, "bytes": b64u(data)}}


def close_frame(sid: str, reason: str) -> dict[str, Any]:
    return {"op": "stream_close", "body": {"stream_id": sid, "reason": reason}}


def answer(router: Any, ws: str, port: FakePort, body: dict[str, Any] | None = None, n: int = 1) -> str:
    """Play the host: wait for the n-th stream_open on ``port``, reply to it, return its stream id."""
    seq, b = port.wait_op("stream_open", n)[n - 1]
    router.on_reply(ws, port, {"re": seq, "body": {} if body is None else body})
    return b["stream_id"]


def open_stream(
    router: Any, ws: str, port: FakePort, user: str = "u1", dev: str = DEV, kind: str = "session", sink=None
):
    """Open a stream with a host that accepts; returns (stream_id, sink)."""
    sink = sink or FakeSink()
    n = len(port.ops("stream_open")) + 1
    out: list[str] = []
    t = threading.Thread(target=lambda: out.append(router.open(ws, user, dev, kind, sink)), daemon=True)
    t.start()
    answer(router, ws, port, None, n)
    t.join(5)
    assert out, "open did not return"
    return out[0], sink


class FakeConn:
    """One end of an in-memory connection with the ``TlsConn`` methods the endpoint uses."""

    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.buf = bytearray()
        self.peer: FakeConn | None = None
        self.closed = False

    @classmethod
    def pair(cls) -> tuple[FakeConn, FakeConn]:
        a, b = cls(), cls()
        a.peer, b.peer = b, a
        return a, b

    def recv_exact(self, n: int, deadline: float) -> bytes:
        with self.cv:
            while len(self.buf) < n:
                if self.closed:
                    raise LinkClosed("eof")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise LinkTimeout
                self.cv.wait(min(left, 0.05))
            out = bytes(self.buf[:n])
            del self.buf[:n]
            return out

    def send_all(self, data: bytes, deadline: float) -> None:
        p = self.peer
        assert p is not None
        if self.closed or p.closed:
            raise LinkClosed("closed")
        with p.cv:
            p.buf += data
            p.cv.notify_all()

    def close(self) -> None:
        for c in (self, self.peer):
            if c is not None:
                with c.cv:
                    c.closed = True
                    c.cv.notify_all()

    def exporter(self) -> bytes:
        return b"\0" * 32

    def alpn(self) -> str:
        return "orch-remote/1"

    # client-side helpers
    def put(self, obj: dict[str, Any]) -> None:
        framing.write_frame(self, obj, timeout=5, max_bytes=1 << 20)

    def get(self, timeout: float = 5.0) -> dict[str, Any]:
        return framing.read_frame(self, first_timeout=timeout, rest_timeout=5, max_bytes=1 << 20)

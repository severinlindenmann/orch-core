"""Link framing: one message = 4-byte big-endian length, then that many bytes of canonical JSON.

The length is checked against ``max_bytes`` BEFORE any payload is read or allocated; the payload is parsed with the
strict parser (duplicate keys, floats, non-ASCII keys, nesting over 16 refused) and must be a JSON object. Every read
and write takes a deadline."""

from __future__ import annotations

import time

from orch.canon.jcs import JcsError, dumps, loads_strict

from .tls import TlsConn

__all__ = ["HEADER", "MAX_FRAME", "FrameError", "read_frame", "write_frame"]

MAX_FRAME = 1_500_000  # the hub's cap (protocol 10.3): a 1 MiB envelope as base64url inside a signed message
HEADER = 4


class FrameError(Exception):
    """A frame that is oversize, empty or not a strict JSON object; the link is closed (code ``malformed``)."""


def write_frame(conn: TlsConn, obj: dict, *, timeout: float, max_bytes: int = MAX_FRAME) -> None:
    try:
        payload = dumps(obj)
    except JcsError:
        raise FrameError("not canonical json") from None
    if len(payload) > max_bytes:
        raise FrameError("frame too large")
    conn.send_all(len(payload).to_bytes(HEADER, "big") + payload, time.monotonic() + timeout)


def read_frame(conn: TlsConn, *, timeout: float, max_bytes: int = MAX_FRAME) -> dict:
    """The whole frame (header and body) must arrive within ``timeout``. Raises :class:`FrameError`,
    ``LinkTimeout`` or ``LinkClosed``."""
    end = time.monotonic() + timeout
    n = int.from_bytes(conn.recv_exact(HEADER, end), "big")
    if n == 0 or n > max_bytes:
        raise FrameError("bad frame length")
    payload = conn.recv_exact(n, end)
    try:
        obj = loads_strict(payload)
    except JcsError:
        raise FrameError("not strict json") from None
    if type(obj) is not dict:
        raise FrameError("not an object")
    return obj

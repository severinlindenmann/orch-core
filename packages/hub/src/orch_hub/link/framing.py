"""Link framing: one message = 4-byte big-endian length, then that many bytes of canonical JSON (``cj``).

The length is checked against ``max_bytes`` BEFORE any payload is read or allocated (an oversize frame is refused
with ``malformed`` and the link is closed); the payload is parsed with the strict parser of the contract
(duplicate keys, floats, non-ASCII keys, nesting over 16 refused) and must be a JSON object. Every read takes a
deadline. Frames carry the signed ``{"o","sig"}`` objects of §10.2 and the three unsigned link-open frames of
:mod:`orch_hub.link.server`.
"""

from __future__ import annotations

import time

from ..contract.canon import CanonError, cj, loads_strict
from .tls import LinkTimeout, TlsConn

# A route_out / route_in frame carries an envelope of up to 1 MiB (protocol-v2 §11.1) as base64url (x 4/3) plus the
# signed wrapper; every other frame is far smaller and is bounded by its own checks.
MAX_FRAME = 1_500_000
HEADER = 4


class FrameError(Exception):
    """A frame that is oversize, empty or not a strict JSON object; the link is closed (code ``malformed``)."""


def encode(obj: dict) -> bytes:
    try:
        payload = cj(obj)
    except CanonError as e:
        raise FrameError(str(e)) from None
    return len(payload).to_bytes(HEADER, "big") + payload


def write_frame(conn: TlsConn, obj: dict, *, timeout: float, max_bytes: int = MAX_FRAME) -> None:
    data = encode(obj)
    if len(data) - HEADER > max_bytes:
        raise FrameError("frame too large")
    conn.send_all(data, time.monotonic() + timeout)


def read_frame(conn: TlsConn, *, first_timeout: float, rest_timeout: float, max_bytes: int = MAX_FRAME) -> dict:
    """Wait up to ``first_timeout`` for the first header byte (idle wait), then the whole frame must arrive within
    ``rest_timeout``. Raises :class:`FrameError`, ``LinkTimeout`` or ``LinkClosed``."""
    first = conn.recv_exact(1, time.monotonic() + first_timeout)
    end = time.monotonic() + rest_timeout
    n = int.from_bytes(first + conn.recv_exact(HEADER - 1, end), "big")
    if n == 0 or n > max_bytes:
        raise FrameError("bad frame length")
    payload = conn.recv_exact(n, end)
    try:
        obj = loads_strict(payload)
    except CanonError:
        raise FrameError("not strict json") from None
    if type(obj) is not dict:
        raise FrameError("not an object")
    return obj


__all__ = ["MAX_FRAME", "FrameError", "LinkTimeout", "encode", "read_frame", "write_frame"]

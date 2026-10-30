"""One connection to the hub's remote-client endpoint (R5, #1593; hub contract 10.2d, vectors ``client_*``).

``hello`` -> ``ready{hub_id}`` -> optional ``rail`` -> ``open`` -> ``opened{stream_id}`` -> ``data`` frames, one stream
per connection. The hub only checks who may knock; every byte of ``data`` is an opaque Noise message.

This module is the strict reader and writer of that protocol and nothing else:

* every hub frame has **exactly** the keys of its message, else the connection is closed (before ``opened``) or the
stream
  is closed ``malformed`` / ``too_large`` (after);
* the order is the contract's: a ``rail`` it did not ask for, a second ``opened``, a ``refused`` or ``error`` after
  ``opened``, a ``data`` before ``opened`` are all bad hub frames;
* ``ready.hub_id`` must be the hub id of the pinned card;
* the hub's ``error`` / ``refused`` codes surface as :class:`ClientError` with the hub's code.

The transport is a :class:`Wire` (a TLS 1.3 connection pinned to the hub card's SPKI with ALPN ``orch-remote/1``, or
a test
double). Nothing here logs a token, a frame or a stream id."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Protocol

from orch.crypto.encoding import EncodingError, b64u, unb64u

from .codes import ClientError

__all__ = ["ALPN", "HubConn", "TlsWire", "Wire", "dial"]

ALPN = b"orch-remote/1"
FRAME_MAX = 98304
NOISE_MAX = 65535
B64_MAX = (NOISE_MAX * 4 + 2) // 3
SETUP_S = 30.0
READ_S = 10.0  # a frame must arrive whole within this long of its first byte
RAIL_MAX, NAME_MAX = 100, 200
ERRORS = frozenset({"malformed", "no_session", "remote.busy", "unavailable"})
REFUSALS = frozenset(
    {
        "malformed",
        "not_allowed",
        "unavailable",
        "remote.busy",
        "remote.off",
        "remote.device",
        "remote.no_code",
        "replay",
    }
)
CLOSE = frozenset(
    {"closed", "idle", "expired", "revoked", "handshake", "presence", "too_large", "malformed", "remote.off",
     "remote.device", "remote.busy", "remote.no_code", "remote.version"}
)  # fmt: skip
_HEX32 = re.compile(r"[0-9a-f]{32}")
_IDS = {"session": re.compile(r"rd_[0-9a-f]{32}"), "enroll": re.compile(r"rc_[0-9a-f]{32}")}


class Wire(Protocol):
    def put(self, obj: dict[str, Any]) -> None: ...
    def get(self, timeout: float) -> dict[str, Any] | None:
        """The next frame, or ``None`` when none started within ``timeout``. Raises :class:`ClientError` for a frame
        that is not strict JSON / too large, and ``remote_client.closed`` when the connection ended."""

    def close(self) -> None: ...


class TlsWire:
    """The real wire: a pinned TLS 1.3 connection, 4-byte length + strict JSON. One lock serialises the connection (the
    TLS object is not thread-safe); a read waits unlocked until bytes arrive."""

    def __init__(self, conn: Any) -> None:
        self._c, self._mu = conn, threading.Lock()

    def put(self, obj: dict[str, Any]) -> None:
        from orch.hubclient.tlslink import framing
        from orch.hubclient.tlslink.tls import LinkClosed, LinkTimeout

        try:
            with self._mu:
                framing.write_frame(self._c, obj, timeout=READ_S, max_bytes=FRAME_MAX)
        except (LinkClosed, LinkTimeout, framing.FrameError, OSError):
            raise ClientError("remote_client.closed") from None

    def get(self, timeout: float) -> dict[str, Any] | None:
        from orch.hubclient.tlslink import framing
        from orch.hubclient.tlslink.tls import LinkClosed, LinkTimeout

        try:
            if not self._c.wait_readable(timeout):
                return None
            with self._mu:
                return framing.read_frame(self._c, timeout=READ_S, max_bytes=FRAME_MAX)
        except framing.FrameError:
            raise ClientError("remote_client.protocol") from None
        except (LinkClosed, LinkTimeout, OSError):
            raise ClientError("remote_client.closed") from None

    def close(self) -> None:
        self._c.close()


def dial(host: str, port: int, spki: str, *, timeout: float = 10.0) -> TlsWire:
    """TLS 1.3 to the hub's link address, SPKI-pinned, ALPN ``orch-remote/1`` (refused when not selected)."""
    from orch.hubclient.tlslink import tls

    try:
        conn = tls.connect_pinned(host, port, spki, timeout=timeout, alpn=(ALPN,))
    except tls.PinMismatch:
        raise ClientError("remote_client.hub_key_changed") from None
    except (tls.LinkClosed, tls.LinkTimeout, OSError):
        raise ClientError("remote_client.closed") from None
    if conn.alpn() != ALPN:
        conn.close()
        raise ClientError("remote_client.alpn")
    return TlsWire(conn)


def _hex32(x: Any) -> bool:
    return type(x) is str and _HEX32.fullmatch(x) is not None


def _text_ok(s: Any) -> bool:
    from orch.hubclient.custodial_shape import text_ok

    return type(s) is str and text_ok(s, NAME_MAX)


def _rows_ok(rows: Any, more: Any) -> bool:
    if type(rows) is not list or type(more) is not bool or len(rows) > RAIL_MAX or (more and len(rows) < RAIL_MAX):
        return False
    ids = []
    for r in rows:
        if type(r) is not dict or set(r) != {"workspace_id", "name", "online"} or not _hex32(r["workspace_id"]):
            return False
        if type(r["online"]) is not bool or (r["name"] is not None and not _text_ok(r["name"])):
            return False
        ids.append(r["workspace_id"])
    return ids == sorted(set(ids))


def _data_bytes(b: Any) -> bytes:
    """One Noise message: canonical base64url of 1..65535 bytes. ``too_large`` / ``malformed`` otherwise."""
    if type(b) is str and len(b) > B64_MAX:
        raise _Bad("too_large")
    try:
        raw = unb64u(b)
    except (EncodingError, TypeError, ValueError):
        raise _Bad("malformed") from None
    if not raw:
        raise _Bad("malformed")
    if len(raw) > NOISE_MAX:
        raise _Bad("too_large")
    return raw


class _Bad(Exception):
    def __init__(self, reason: str = "malformed") -> None:
        super().__init__(reason)
        self.reason = reason


_SHAPES: dict[str, set[str]] = {
    "ready": {"t", "hub_id"},
    "error": {"t", "error"},
    "rail": {"t", "workspaces", "more"},
    "opened": {"t", "stream_id"},
    "refused": {"t", "error"},
    "data": {"t", "b"},
    "close": {"t", "reason"},
}


def _shape(o: dict[str, Any]) -> str:
    t = o.get("t")
    if type(t) is not str or t not in _SHAPES or set(o) != _SHAPES[t]:
        raise _Bad
    ok = {
        "ready": lambda: _hex32(o["hub_id"]),
        "error": lambda: type(o["error"]) is str and o["error"] in ERRORS,
        "rail": lambda: _rows_ok(o["workspaces"], o["more"]),
        "opened": lambda: _hex32(o["stream_id"]),
        "refused": lambda: type(o["error"]) is str and o["error"] in REFUSALS,
        "data": lambda: True,
        "close": lambda: type(o["reason"]) is str and o["reason"] in CLOSE,
    }[t]()
    if not ok:
        raise _Bad
    return t


class HubConn:
    """States: ``new`` -> ``ready`` -> (``rail_done``) -> ``stream`` -> ``ended``."""

    def __init__(self, wire: Wire, *, token: str, hub_id: str) -> None:
        self.wire, self._token, self.hub_id = wire, token, hub_id
        self.state = "new"
        self._rail_used = False
        self.stream_id: str | None = None
        self._deadline = time.monotonic() + SETUP_S
        self._client_closed = False

    # -- setup --

    def _fail(self, code: str) -> ClientError:
        self.state = "ended"
        self.wire.close()
        return ClientError(code)

    def _next(self, expect: set[str]) -> dict[str, Any]:
        """The next hub frame during setup, before ``opened``: anything out of shape or order closes the connection."""
        left = self._deadline - time.monotonic()
        o = self.wire.get(max(left, 0.0))
        if o is None:
            raise self._fail("remote_client.timeout")
        try:
            t = _shape(o)
        except _Bad:
            raise self._fail("remote_client.protocol") from None
        if t == "error":
            raise self._fail(o["error"])
        if t == "refused":
            raise self._fail(o["error"]) if "refused" in expect else self._fail("remote_client.protocol")
        if t not in expect:
            raise self._fail("remote_client.protocol")
        return o

    def hello(self) -> str:
        if self.state != "new":
            raise ClientError("remote_client.protocol")
        self.wire.put({"t": "hello", "v": 1, "token": self._token})
        o = self._next({"ready"})
        if o["hub_id"] != self.hub_id:
            raise self._fail("remote_client.hub_mismatch")
        self.state = "ready"
        return o["hub_id"]

    def rail(self) -> list[dict[str, Any]]:
        if self.state != "ready" or self._rail_used:
            raise ClientError("remote_client.protocol")
        self._rail_used = True
        self.wire.put({"t": "rail"})
        o = self._next({"rail"})
        return list(o["workspaces"])

    def open(self, workspace_id: str, ident: str, kind: str) -> str:
        if self.state != "ready" or kind not in _IDS or not _hex32(workspace_id) or not _IDS[kind].fullmatch(ident):
            raise ClientError("remote_client.protocol")
        self.wire.put({"t": "open", "workspace_id": workspace_id, "device_id": ident, "kind": kind})
        o = self._next({"opened", "refused"})
        self.stream_id, self.state = o["stream_id"], "stream"
        return o["stream_id"]

    # -- the stream --

    def send_data(self, raw: bytes) -> None:
        if self.state != "stream" or self._client_closed or not 1 <= len(raw) <= NOISE_MAX:
            raise ClientError("remote_client.closed")
        self.wire.put({"t": "data", "b": b64u(raw)})

    def send_close(self, reason: str) -> None:
        """Our ``close``; the hub answers with one (and may send data first, which :meth:`recv` drops)."""
        if self.state != "stream" or self._client_closed:
            return
        self._client_closed = True
        try:
            self.wire.put({"t": "close", "reason": reason if reason in CLOSE else "closed"})
        except ClientError:
            self.state = "ended"

    def recv(self, timeout: float) -> tuple[str, Any] | None:
        """``("data", bytes)``, ``("close", reason)`` or ``None`` (nothing within ``timeout``). A hub frame out of
        shape or order closes the stream ``malformed`` / ``too_large`` and raises ``remote_client.protocol``."""
        while True:
            if self.state == "ended":
                return ("close", "closed")
            try:
                o = self.wire.get(timeout)
            except ClientError as exc:
                self.state = "ended"
                if exc.code == "remote_client.closed":
                    return ("close", "closed")  # closing the connection is the same as `close closed`
                raise
            if o is None:
                return None
            try:
                t = _shape(o)
                if t == "data":
                    raw = _data_bytes(o["b"])
                    if self._client_closed:
                        continue  # data the host sent before it saw our close: dropped, not an error
                    return ("data", raw)
                if t == "close":
                    self.state = "ended"
                    self.wire.close()
                    return ("close", o["reason"])
                raise _Bad  # error / refused / opened / rail / ready after `opened`
            except _Bad as bad:
                self.send_close(bad.reason)
                self.state = "ended"
                self.wire.close()
                raise ClientError("remote_client.protocol") from None

    def close(self) -> None:
        self.state = "ended"
        self.wire.close()

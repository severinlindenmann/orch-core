"""Host end of the hub link over TLS 1.3 (orch-hub ``docs/protocol.md`` section 10).

Opening (10.1): dial, handshake TLS 1.3, compare the hub certificate's SPKI hash with the held card's
``tls_spki_sha256`` (nothing is sent before that), read the challenge frame (its ``hub_id`` must be the card's), answer
with ``Sign(WSK, link_auth bytes over challenge and this connection's TLS exporter)``, wait for ``ready``. Pre-auth
refusals arrive as one unsigned ``{"t": "error"}`` frame and the link closes.

Messages (10.2): :meth:`LinkClient.send` carries one already signed w2h ``hub.link`` object and returns the h2w reply
after verifying it under the card's ``HK_op`` (signature, direction, ``op``, ``re`` == the request's ``seq``, hub,
workspace, clock window, reply body shape). It is the ``HubTransport`` shape C2a's ``HubLink`` uses. Only the closed
w2h operation list is ever sent. Any failure after the hub spoke closes the link; a signed ``{"error": code}`` reply
raises :class:`LinkError` and keeps it open.

``sign`` is a callable over the full message (label || data), the same shape as C2a's ``wire.Sign``: core's custody
decides what it may sign. Nothing here logs keys, signatures or message bodies."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from orch.canon.jcs import JcsError
from orch.canon.jcs import dumps as cj
from orch.crypto import suite
from orch.crypto.encoding import EncodingError, b64u, unb64u, unhex

from . import framing, tls
from .framing import FrameError
from .tls import LinkClosed, LinkTimeout, PinMismatch

__all__ = [
    "LINK_WINDOW_MS",
    "W2H_OPS",
    "LinkClient",
    "LinkError",
    "FrameError",
    "LinkClosed",
    "LinkTimeout",
    "PinMismatch",
    "link_auth_bytes",
]

SIG_LINK = b"orch/v2/sig/hub-link|"
SIG_ROUTE = b"orch/v2/hub-route|"
SIG_LINK_AUTH = b"orch/v2/sig/hub-link-auth|"
HV = 0
LINK_WINDOW_MS = 120_000
MODES = ("selective", "full")
#: the closed w2h op list: op -> the mode it exists in (None: both); and the shape of its reply body
W2H_OPS: dict[str, str | None] = {
    "join": None,
    "card_put": None,
    "binding_put": None,
    "snapshot_get": None,
    "publish_out": None,
    "route_out": None,
    "audit_ack": None,
    "share_put": "selective",
    "share_revoke": "selective",
    "wk_seal": "full",
}
_REPLY: dict[str, Callable[[dict[str, Any]], bool]] = {
    "snapshot_get": lambda b: set(b) == {"snapshot"} and type(b["snapshot"]) is dict,
    "publish_out": lambda b: (
        set(b) == {"status", "body_hex"} and type(b["status"]) is int and type(b["body_hex"]) is str
    ),
}
_HEAD_FIELDS = {"v", "suite", "kind", "hv", "hub_id", "workspace_id", "dir", "seq", "ts_ms", "op", "re", "body"}


class LinkError(Exception):
    """The hub refused, or a reply failed verification: ``code`` is a refusal code of protocol section 14."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def link_auth_bytes(hub_id: str, workspace_id: str, challenge: bytes, exporter: bytes) -> bytes:
    """What WSK signs to open a link: the label, the suite, both ids, the 32-byte challenge, the 32-byte exporter."""
    if len(challenge) != 32 or len(exporter) != 32:
        raise LinkError("malformed")
    try:
        return (
            SIG_LINK_AUTH + bytes([suite.SUITE_ID]) + unhex(hub_id, 16) + unhex(workspace_id, 16) + challenge + exporter
        )
    except EncodingError:
        raise LinkError("malformed") from None


def _code(x: Any) -> str:
    return x if type(x) is str and 0 < len(x) <= 40 else "malformed"


class LinkClient:
    """An authenticated link. Use :meth:`open`; one request is in flight at a time."""

    def __init__(
        self,
        conn: tls.TlsConn,
        *,
        hub_id: str,
        workspace_id: str,
        hk_op_pub: bytes,
        mode: str,
        now_ms: Callable[[], int],
        timeout: float,
        max_frame: int,
    ) -> None:
        self._c, self.hub_id, self.workspace_id, self._hk_op = conn, hub_id, workspace_id, hk_op_pub
        self.mode, self._now, self._t, self._max = mode, now_ms, timeout, max_frame
        self._lock = threading.Lock()
        self._open = True

    @staticmethod
    def open(
        host: str,
        port: int,
        *,
        tls_spki_sha256: str,
        hub_id: str,
        workspace_id: str,
        sign: Callable[[bytes], bytes],
        hk_op_pub: bytes,
        wsk_pub: bytes | None = None,
        mode: str = "selective",
        timeout: float = 10.0,
        max_frame: int = framing.MAX_FRAME,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> LinkClient:
        """Open and authenticate. ``wsk_pub`` is passed only for the first link of a join (the hub has not pinned the
        workspace yet); otherwise leave it ``None``. Raises ``PinMismatch`` before anything is sent when the hub's
        certificate is not the card's, ``LinkError`` for a refusal, ``LinkClosed`` / ``LinkTimeout`` otherwise."""
        if mode not in MODES:
            raise LinkError("malformed")
        conn = tls.connect_pinned(host, port, tls_spki_sha256, timeout=timeout)
        try:
            ch = framing.read_frame(conn, timeout=timeout, max_bytes=max_frame)
            if ch.get("t") == "error":
                raise LinkError(_code(ch.get("error")))
            if set(ch) != {"t", "hub_id", "challenge"} or ch["t"] != "challenge":
                raise LinkError("malformed")
            if ch["hub_id"] != hub_id:
                raise LinkError("other_hub")
            msg = link_auth_bytes(hub_id, workspace_id, unb64u(ch["challenge"], 32), conn.exporter())
            auth: dict[str, Any] = {"t": "auth", "workspace_id": workspace_id, "sig": b64u(sign(msg))}
            if wsk_pub is not None:
                auth["wsk_pub"] = b64u(wsk_pub)
            framing.write_frame(conn, auth, timeout=timeout, max_bytes=max_frame)
            r = framing.read_frame(conn, timeout=timeout, max_bytes=max_frame)
            if r.get("t") == "error":
                raise LinkError(_code(r.get("error")))
            if r != {"t": "ready", "hub_id": hub_id}:
                raise LinkError("malformed")
        except EncodingError:
            conn.close()
            raise LinkError("malformed") from None
        except BaseException:
            conn.close()
            raise
        return LinkClient(
            conn,
            hub_id=hub_id,
            workspace_id=workspace_id,
            hk_op_pub=hk_op_pub,
            mode=mode,
            now_ms=now_ms,
            timeout=timeout,
            max_frame=max_frame,
        )

    # --- messages -------------------------------------------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        """``HubTransport.send``: write one signed w2h message, return the verified signed h2w reply. Transport
        failures raise ``OSError`` subclasses (``LinkClosed``, ``LinkTimeout``); anything the hub sent that fails
        verification raises :class:`LinkError` and closes the link."""
        o = self._check_request(message)
        with self._lock:
            if not self._open:
                raise LinkClosed("closed")
            try:
                framing.write_frame(self._c, message, timeout=self._t, max_bytes=self._max)
                reply = framing.read_frame(self._c, timeout=self._t, max_bytes=self._max)
                if reply.get("t") == "error":  # unsigned link-level refusal; the hub closes
                    raise LinkError(_code(reply.get("error")))
                self._verify_reply(reply, o)
            except FrameError:
                self.close()
                raise LinkError("malformed") from None
            except (LinkError, LinkClosed, LinkTimeout):
                self.close()
                raise
        return reply

    def _check_request(self, m: Any) -> dict[str, Any]:
        """Never send anything outside the closed list or for another hub / workspace / mode."""
        o = m.get("o") if type(m) is dict else None
        if type(o) is not dict or set(m) != {"o", "sig"} or set(o) != _HEAD_FIELDS:
            raise LinkError("malformed")
        if o["kind"] != "hub.link" or o["dir"] != "w2h" or o["re"] is not None or type(o["seq"]) is not int:
            raise LinkError("malformed")
        if o["op"] not in W2H_OPS:
            raise LinkError("unknown_op")
        if W2H_OPS[o["op"]] not in (None, self.mode):
            raise LinkError("wrong_mode")
        if o["hub_id"] != self.hub_id:
            raise LinkError("other_hub")
        if o["workspace_id"] != self.workspace_id:
            raise LinkError("other_workspace")
        return o

    def _verify_reply(self, r: dict[str, Any], want: dict[str, Any]) -> None:
        """Section 10.2 steps 1-6 for an h2w reply under the held card's HK_op, then the reply rules."""
        o = r.get("o") if set(r) == {"o", "sig"} else None
        if type(o) is not dict or o.get("kind") != "hub.link":
            raise LinkError("malformed")
        if type(o.get("suite")) is not int or o["suite"] != suite.SUITE_ID:
            raise LinkError("wrong_suite")
        if type(o.get("v")) is not int or o["v"] != 2 or type(o.get("hv")) is not int or o["hv"] != HV:
            raise LinkError("malformed")
        try:
            sig = unb64u(r["sig"], suite.SIG_LEN)
            msg = SIG_ROUTE + cj(o)
        except (EncodingError, JcsError):
            raise LinkError("malformed") from None
        if not suite.verify(self._hk_op, sig, msg):
            raise LinkError("bad_signature")
        if set(o) != _HEAD_FIELDS:
            raise LinkError("malformed")
        for k in ("hub_id", "workspace_id"):
            try:
                unhex(o[k], 16)
            except EncodingError:
                raise LinkError("malformed") from None
        if o["dir"] != "h2w" or type(o["op"]) is not str or type(o["body"]) is not dict:
            raise LinkError("malformed")
        if type(o["seq"]) is not int or o["seq"] < 1 or type(o["ts_ms"]) is not int:
            raise LinkError("malformed")
        # An unknown or failed op is answered with the literal "error", never an echo of a peer-chosen string.
        body, is_error = o["body"], o["op"] == "error"
        if o["op"] != want["op"] and not is_error:
            raise LinkError("malformed")
        if type(o["re"]) is not int or o["re"] != want["seq"]:
            raise LinkError("malformed")
        if o["hub_id"] != self.hub_id:
            raise LinkError("other_hub")
        if o["workspace_id"] != self.workspace_id:
            raise LinkError("other_workspace")
        if abs(self._now() - o["ts_ms"]) > LINK_WINDOW_MS:
            raise LinkError("stale_timestamp")
        if set(body) == {"error"}:
            return
        if is_error or not _REPLY.get(want["op"], lambda b: b == {})(body):
            raise LinkError("malformed")

    def request(self, op: str, body: dict[str, Any], *, sign: Callable[[bytes], bytes], seq: int) -> dict[str, Any]:
        """Convenience for callers without a ``HubLink``: sign and send one request, return the reply body, raise
        :class:`LinkError` for ``{"error": code}``."""
        o = {
            "v": 2,
            "suite": suite.SUITE_ID,
            "kind": "hub.link",
            "hv": HV,
            "hub_id": self.hub_id,
            "workspace_id": self.workspace_id,
            "dir": "w2h",
            "seq": seq,
            "ts_ms": self._now(),
            "op": op,
            "re": None,
            "body": body,
        }
        reply = self.send({"o": o, "sig": b64u(sign(SIG_LINK + cj(o)))})
        rb = reply["o"]["body"]
        if set(rb) == {"error"}:
            raise LinkError(_code(rb["error"]))
        return rb

    def close(self) -> None:
        self._open = False
        self._c.close()

    def __enter__(self) -> LinkClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

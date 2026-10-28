"""Host end of the hub link over TLS 1.3 (``contracts/hub/protocol.md`` section 10).

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
#: Most h2w requests (``route_in``, ``event_deliver``) kept per queue while the host drains only the other one.
MAX_QUEUED = 64
LINK_WINDOW_MS = 120_000
MODES = ("selective", "full")
#: the closed w2h op list: op -> the mode it exists in (None: both); and the shape of its reply body
W2H_OPS: dict[str, str | None] = {
    "join": None,
    "card_put": None,
    "binding_put": None,
    "snapshot_get": None,
    "org_skills_get": None,
    "mcp_servers_get": None,
    "publish_out": None,
    "route_out": None,
    "audit_ack": None,
    "space_list": None,
    "space_accept": None,
    "space_put": None,
    "space_revoke": None,
    "space_get": None,
    "switch_ticket": None,
    "switch_redeem": None,
    "rail_get": None,
    "owner_get": None,
    "share_put": "selective",
    "share_revoke": "selective",
    "wk_seal": "full",
    "log_sync": "full",
}
_REPLY: dict[str, Callable[[dict[str, Any]], bool]] = {
    "space_list": lambda b: set(b) == {"spaces"} and type(b["spaces"]) is list,
    "space_put": lambda b: set(b) == {"seq"} and type(b["seq"]) is int,
    "space_revoke": lambda b: set(b) == {"seq"} and type(b["seq"]) is int,
    "space_get": lambda b: (
        set(b) - {"records"} == {"space", "wraps", "entries", "next"}
        and type(b["space"]) is dict
        and type(b["wraps"]) is list
        and type(b["entries"]) is list
        and type(b["next"]) is int
        and type(b.get("records", [])) is list
    ),
    "switch_ticket": lambda b: set(b) == {"ticket", "url"} and type(b["ticket"]) is str and type(b["url"]) is str,
    "switch_redeem": lambda b: (
        set(b) == {"user_id", "name", "return_to", "kind"}
        and type(b["user_id"]) is str
        and type(b["return_to"]) is str
        and b["kind"] in ("open", "signout")
    ),
    "rail_get": lambda b: (
        set(b) == {"hub", "user_id", "workspaces"} and type(b["hub"]) is dict and type(b["workspaces"]) is list
    ),
    "owner_get": lambda b: set(b) == {"user_id"} and type(b["user_id"]) is str,
    "org_skills_get": lambda b: set(b) == {"bundle"} and type(b["bundle"]) is dict,
    "mcp_servers_get": lambda b: set(b) == {"bundle"} and type(b["bundle"]) is dict,
    "log_sync": lambda b: set(b) == {"seq", "head"} and type(b["seq"]) is int and type(b["head"]) is str,
    "route_out": lambda b: set(b) == {"state"} and b["state"] in ("forwarded", "queued"),
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
        self._sign: Callable[[bytes], bytes] | None = None
        self._seq = 0  # the highest w2h seq used on this link (requests carry the caller's, acks continue from here)
        self._push: list[dict[str, Any]] = []  # verified h2w ``route_in`` requests not yet handed out
        self._pending: dict[str, Any] | None = None  # the handed-out one, awaiting its ack
        self._events: list[dict[str, Any]] = []  # verified h2w ``event_deliver`` requests (custodial events, p4)
        self._pending_ev: dict[str, Any] | None = None
        self._last_in = 0  # highest h2w request seq accepted (route_in and event_deliver share the hub's counter)

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
        end = time.monotonic() + timeout  # one overall deadline for dial, handshake, challenge and auth

        def left() -> float:
            return max(end - time.monotonic(), 0.001)

        conn = tls.connect_pinned(host, port, tls_spki_sha256, timeout=timeout)
        try:
            ch = framing.read_frame(conn, timeout=left(), max_bytes=max_frame)
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
            framing.write_frame(conn, auth, timeout=left(), max_bytes=max_frame)
            r = framing.read_frame(conn, timeout=left(), max_bytes=max_frame)
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
        client = LinkClient(
            conn,
            hub_id=hub_id,
            workspace_id=workspace_id,
            hk_op_pub=hk_op_pub,
            mode=mode,
            now_ms=now_ms,
            timeout=timeout,
            max_frame=max_frame,
        )
        client._sign = sign
        return client

    # --- messages -------------------------------------------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        """``HubTransport.send``: write one signed w2h message, return the verified signed h2w reply. Transport
        failures raise ``OSError`` subclasses (``LinkClosed``, ``LinkTimeout``); anything the hub sent that fails
        verification raises :class:`LinkError` and closes the link. A ``route_in`` the hub pushes meanwhile is kept
        for :meth:`next_route_in`."""
        o = self._check_request(message)
        with self._lock:
            return self._exchange(message, o)

    def _exchange(self, message: dict[str, Any], o: dict[str, Any]) -> dict[str, Any]:
        """Write one request and read its reply. The caller holds ``_lock``."""
        if not self._open:
            raise LinkClosed("closed")
        try:
            framing.write_frame(self._c, message, timeout=self._t, max_bytes=self._max)
            self._seq = max(self._seq, o["seq"])
            while True:
                reply = framing.read_frame(self._c, timeout=self._t, max_bytes=self._max)
                if reply.get("t") == "error":  # unsigned link-level refusal; the hub closes
                    raise LinkError(_code(reply.get("error")))
                if self._take_push(reply):
                    continue
                self._verify_reply(reply, o)
                return reply
        except FrameError:
            self.close()
            raise LinkError("malformed") from None
        except (LinkError, LinkClosed, LinkTimeout):
            self.close()
            raise

    def send_next(self, build: Callable[[int], dict[str, Any]]) -> dict[str, Any]:
        """The one w2h seq allocator of this link: take ``seq`` = the link's highest + 1 under the link lock, let
        ``build(seq)`` sign the request (inside the lock, so no other frame, call or ack, can take a number in
        between or overtake it), send it and return the verified signed reply. Every request the host sends on
        this link goes through here (``HubLink`` via ``TlsTransport``, and :meth:`call`); acks use the same counter."""
        with self._lock:
            message = build(self._seq + 1)
            o = self._check_request(message)
            return self._exchange(message, o)

    def call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        """Sign and send one request with the link's own next ``seq`` (see :meth:`send_next`); return the reply body
        or raise :class:`LinkError` (a signed ``{"error": code}`` keeps the link open)."""
        sign = self._sign
        if sign is None:
            raise LinkClosed("closed")

        def build(seq: int) -> dict[str, Any]:
            o = {
                "v": 2, "suite": suite.SUITE_ID, "kind": "hub.link", "hv": HV, "hub_id": self.hub_id,
                "workspace_id": self.workspace_id, "dir": "w2h", "seq": seq, "ts_ms": self._now(),
                "op": op, "re": None, "body": body,
            }  # fmt: skip
            return {"o": o, "sig": b64u(sign(SIG_LINK + cj(o)))}

        reply = self.send_next(build)
        rb = reply["o"]["body"]
        if set(rb) == {"error"}:
            raise LinkError(_code(rb["error"]))
        return rb

    # --- route_in (h2w requests the hub pushes; protocol 10.3) -----------------------------------------------------

    def _take_push(self, frame: dict[str, Any]) -> bool:
        """If ``frame`` is an h2w ``route_in`` or ``event_deliver`` request: verify it, keep it, return True. A frame
        that is not one returns False (it is the reply being waited for). Only the link layer shape is checked here
        (``{"log", "event"}`` for a custodial event); the event itself is verified by the host, never by the link."""
        o = frame.get("o")
        if type(o) is not dict or o.get("dir") != "h2w" or o.get("re") is not None:
            return False
        self._verify_signed(frame)
        body = frame["o"]["body"]
        if o["op"] == "route_in":
            if set(body) != {"envelope"} or type(body["envelope"]) is not str:
                raise LinkError("malformed")
            queue = self._push
        elif o["op"] == "event_deliver":
            if set(body) != {"log", "event"} or type(body["log"]) is not str or type(body["event"]) is not dict:
                raise LinkError("malformed")
            queue = self._events
        else:
            raise LinkError("malformed")
        if type(o["seq"]) is not int or o["seq"] <= self._last_in:
            raise LinkError("malformed")
        if len(queue) >= MAX_QUEUED:
            # L2-C3-5: REFUSE, never drop. The hub resends what was not acknowledged after a reconnect, so closing
            # loses nothing; dropping the oldest would silently discard a request the hub still holds as undelivered.
            raise LinkError("queue_full")
        self._last_in = o["seq"]
        queue.append(o)
        return True

    def next_route_in(self, wait: float = 0.0) -> bytes | None:
        """The next envelope the hub delivers, the exact bytes the sender signed, or ``None`` when nothing arrives
        within ``wait`` seconds. At most one is outstanding: call :meth:`ack_route_in` after the host verified and
        stored it (the hub resends an unacknowledged one after a reconnect)."""
        with self._lock:
            if not self._open:
                raise LinkClosed("closed")
            if self._pending is not None:
                raise LinkError("malformed")  # a caller bug: the last one was never acknowledged
            try:
                if not self._push:
                    if not self._c.wait_readable(wait):
                        return None
                    frame = framing.read_frame(self._c, timeout=self._t, max_bytes=self._max)
                    if frame.get("t") == "error":
                        raise LinkError(_code(frame.get("error")))
                    if not self._take_push(frame):
                        raise LinkError("malformed")
                    if not self._push:
                        return None  # it was an event_deliver: next_event_deliver hands it out
                self._pending = self._push.pop(0)
                try:
                    return unb64u(self._pending["body"]["envelope"])
                except EncodingError:
                    raise LinkError("malformed") from None
            except FrameError:
                self.close()
                raise LinkError("malformed") from None
            except (LinkError, LinkClosed, LinkTimeout):
                self.close()
                raise

    def ack_route_in(self, error: str | None = None) -> None:
        """Answer the outstanding ``route_in`` (a w2h reply: same op, ``re`` = its seq, body ``{}`` or
        ``{"error": code}``). Only this removes the entry at the hub."""
        with self._lock:
            if not self._open or self._sign is None:
                raise LinkClosed("closed")
            if self._pending is None:
                raise LinkError("malformed")
            if error is not None and not (type(error) is str and 0 < len(error) <= 40):
                raise LinkError("malformed")
            self._seq += 1
            o = {
                "v": 2, "suite": suite.SUITE_ID, "kind": "hub.link", "hv": HV, "hub_id": self.hub_id,
                "workspace_id": self.workspace_id, "dir": "w2h", "seq": self._seq, "ts_ms": self._now(),
                "op": "route_in", "re": self._pending["seq"], "body": {} if error is None else {"error": error},
            }  # fmt: skip
            try:
                framing.write_frame(
                    self._c, {"o": o, "sig": b64u(self._sign(SIG_LINK + cj(o)))}, timeout=self._t, max_bytes=self._max
                )
            except (FrameError, LinkClosed, LinkTimeout):
                self.close()
                raise
            self._pending = None

    # --- event_deliver (h2w custodial events; protocol 8.4, 10.2) --------------------------------------------------

    def next_event_deliver(self, wait: float = 0.0) -> tuple[str, dict[str, Any]] | None:
        """The next custodial event the hub delivers, as ``(log, event)``, or ``None`` within ``wait`` seconds. The
        link verified the signed message and its body shape only: NOTHING about the event is trusted (the host runs
        :func:`orch.hubclient.custodial.verify_hub_event` and its own log checks). At most one is outstanding: call
        :meth:`ack_event_deliver` once it was admitted or refused; the hub resends an unacknowledged one."""
        with self._lock:
            if not self._open:
                raise LinkClosed("closed")
            if self._pending_ev is not None:
                raise LinkError("malformed")  # a caller bug: the last one was never acknowledged
            try:
                if not self._events:
                    if not self._c.wait_readable(wait):
                        return None
                    frame = framing.read_frame(self._c, timeout=self._t, max_bytes=self._max)
                    if frame.get("t") == "error":
                        raise LinkError(_code(frame.get("error")))
                    if not self._take_push(frame):
                        raise LinkError("malformed")
                    if not self._events:
                        return None  # it was a route_in: next_route_in hands it out
                self._pending_ev = self._events.pop(0)
                b = self._pending_ev["body"]
                return b["log"], b["event"]
            except FrameError:
                self.close()
                raise LinkError("malformed") from None
            except (LinkError, LinkClosed, LinkTimeout):
                self.close()
                raise

    def ack_event_deliver(self, error: str | None = None) -> None:
        """Answer the outstanding ``event_deliver`` (a w2h reply: same op, ``re`` = its seq, body ``{}`` or
        ``{"error": code}``, protocol 10.2). A refused event is acknowledged WITH its code, so the hub learns why."""
        with self._lock:
            if not self._open or self._sign is None:
                raise LinkClosed("closed")
            if self._pending_ev is None:
                raise LinkError("malformed")
            if error is not None and not (type(error) is str and 0 < len(error) <= 40):
                raise LinkError("malformed")
            self._seq += 1
            o = {
                "v": 2, "suite": suite.SUITE_ID, "kind": "hub.link", "hv": HV, "hub_id": self.hub_id,
                "workspace_id": self.workspace_id, "dir": "w2h", "seq": self._seq, "ts_ms": self._now(),
                "op": "event_deliver", "re": self._pending_ev["seq"], "body": {} if error is None else {"error": error},
            }  # fmt: skip
            try:
                framing.write_frame(
                    self._c, {"o": o, "sig": b64u(self._sign(SIG_LINK + cj(o)))}, timeout=self._t, max_bytes=self._max
                )
            except (FrameError, LinkClosed, LinkTimeout):
                self.close()
                raise
            self._pending_ev = None

    def route_out(self, envelope: bytes) -> str:
        """Send one ws->ws envelope (the sender's exact bytes); returns ``"forwarded"`` or ``"queued"``. Raises
        :class:`LinkError` with the hub's code (``not_allowed``, ``queue_full``, ``too_large``, ``replay``, ...)."""
        return self.call("route_out", {"envelope": b64u(envelope)})["state"]

    def _check_request(self, m: Any) -> dict[str, Any]:
        """Never send anything outside the closed list or for another hub / workspace / mode."""
        o = m.get("o") if type(m) is dict else None
        if type(o) is not dict or set(m) != {"o", "sig"} or set(o) != _HEAD_FIELDS:
            raise LinkError("malformed")
        if o["kind"] != "hub.link" or o["dir"] != "w2h" or o["re"] is not None or type(o["seq"]) is not int:
            raise LinkError("malformed")
        if type(o["op"]) is not str:
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

    def _verify_signed(self, r: dict[str, Any]) -> dict[str, Any]:
        """Any h2w frame: envelope shape, suite, signature under HK_op, direction, hub, workspace, clock window."""
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
        if o["hub_id"] != self.hub_id:
            raise LinkError("other_hub")
        if o["workspace_id"] != self.workspace_id:
            raise LinkError("other_workspace")
        if abs(self._now() - o["ts_ms"]) > LINK_WINDOW_MS:
            raise LinkError("stale_timestamp")
        return o

    def _verify_reply(self, r: dict[str, Any], want: dict[str, Any]) -> None:
        """Section 10.2 steps 1-6 for an h2w reply under the held card's HK_op, then the reply rules."""
        o = self._verify_signed(r)
        # An unknown or failed op is answered with the literal "error", never an echo of a peer-chosen string.
        body, is_error = o["body"], o["op"] == "error"
        if o["op"] != want["op"] and not is_error:
            raise LinkError("malformed")
        if type(o["re"]) is not int or o["re"] != want["seq"]:
            raise LinkError("malformed")
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

    @property
    def is_open(self) -> bool:
        """False once the link was closed (by :meth:`close`, or by a transport or verification failure)."""
        return self._open

    def close(self) -> None:
        self._open = False
        self._c.close()

    def __enter__(self) -> LinkClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

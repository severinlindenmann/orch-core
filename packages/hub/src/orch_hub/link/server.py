"""The hub side of the host<->hub link (contracts/hub/protocol.md §10).

Wire summary (all frames per :mod:`orch_hub.link.framing`: 4-byte big-endian length + canonical JSON):

1. The host dials; TLS 1.3 only; the host compares the hub certificate's SPKI hash with the card's
   ``tls_spki_sha256`` (the server has nothing to check at this layer).
2. hub -> host  ``{"t":"challenge","hub_id","challenge":b64u(32)}``
3. host -> hub  ``{"t":"auth","workspace_id","sig":b64u,["wsk_pub":b64u]}`` where ``sig = Sign(WSK, link_auth_bytes(
   hub_id, workspace_id, challenge, exporter))`` and ``exporter`` is the TLS exporter of THIS connection.
   ``wsk_pub`` is required exactly when the workspace is not yet pinned (the first link of a join); then the link may
   only ``join``, and the join's card must carry that same key (§9.5, §10.1).
4. hub -> host  ``{"t":"ready","hub_id"}`` or ``{"t":"error","error":code}`` and close.
5. Then signed ``hub.link`` messages (``{"o","sig"}``): w2h requests answered by h2w replies (``re`` = request
   ``seq``, signed with the hub's ``HK_op``), one reply per request, in order. A request that fails the receiver
   verification (§10.2 steps 1-7) gets ``{"t":"error","error":code}`` and the link is closed. An op that fails on its
   own terms gets a signed reply with body ``{"error": code}`` and the link stays open.

Every message signature binds this connection's TLS exporter (``label || exporter || cj(o)``, the exporter is never
sent), and ``seq`` is strictly consecutive per direction and connection, from 1 (R1, #1589): a frame is valid on one
connection, at one position, once. The counter lives in memory for the life of the connection; nothing is persisted.

Fail closed: any read timeout, oversize or malformed frame closes the link. SHORTCUT (L7): the transport is TLS with the
pinned SPKI only (no socket peer-UID transport).
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, Self

from ..backup.hubop import FENCE as RESTORE_FENCE
from ..closing import LINK_ALLOWED as CLOSING_ALLOWED_OPS
from ..contract import hub as H
from ..contract import linkws as W
from ..contract import remote as R
from ..contract import suite as S
from ..contract.canon import CanonError, b64u, unb64u, unhex
from ..contract.hub import Refused
from ..contract.keystore import OpSigner
from ..keys import HubKeys, KeysError
from ..keys.store import PLACEHOLDER_TLS
from . import framing, ops, tls, wsaccept
from .registry import Registry
from .tls import LinkClosed, LinkTimeout
from .wkseal import WkSeals

if TYPE_CHECKING:
    from ..router import Router

_log = logging.getLogger("orch_hub.link")


def _now_ms() -> int:
    return int(time.time() * 1000)


class StreamSink(Protocol):
    """R4 (#1592): what the link feeds the remote stream router (``LinkServer(streams=...)``). Every method runs on a
    link thread, never blocks and never raises (the server also guards each call). ``port`` is the ``_Port`` of the
    link that carried the frame; the sink must match a frame to its stream by id AND port."""

    def attach(self, ws: str, port: _Port) -> None:
        """Called once, at the moment the router's ``attach`` is (workspace pinned to this link's key and bound)."""

    def detach(self, ws: str, port: _Port) -> None:
        """The link is gone, its pin rotated, or the workspace unbound: close every stream of ``port``."""

    def on_frame(self, ws: str, port: _Port, o: dict[str, Any], cost: int) -> None:
        """A verified w2h ``stream_data`` / ``stream_close`` (exporter-bound, link ``seq`` checked). ``o`` is the
        message, ``cost`` its :func:`contract.remote.frame_cost`. No reply is ever sent for it."""

    def on_reply(self, ws: str, port: _Port, o: dict[str, Any]) -> None:
        """A verified w2h reply to a ``stream_open`` (``o["re"]`` = the h2w seq ``port.send_request`` returned; body
        ``{}`` or ``{"error": code}``)."""


class RemoteEndpoint(Protocol):
    """R4 (#1592): the client side of a connection that negotiated ALPN ``orch-remote/1`` (``LinkServer(remote=...)``).
    The connection has left the link pools; ``serve`` owns it until it returns and never raises. Optional: call
    ``conn.signed_in()`` once the client is signed in (frees its slot in the 16-wide "not yet signed in" pool; the
    server frees it after ``remote_signin_timeout`` regardless). Also on ``conn``: ``recv_exact``, ``send_all``,
    ``wait_readable``, ``exporter``, ``alpn``, ``close``."""

    def serve(self, conn: tls.TlsConn) -> None: ...


class _PreAuth:
    """One unauthenticated-connection slot; :meth:`release` is idempotent (auth done, or the connection ended)."""

    def __init__(self, sem: threading.BoundedSemaphore) -> None:
        self._sem, self._held, self._lock = sem, True, threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._held:
                self._held = False
                self._sem.release()


#: #1463: the only link ops served once the hub is closing (default deny; see :mod:`orch_hub.closing`)
CLOSING_LINK_ALLOWED = CLOSING_ALLOWED_OPS


class LinkServer:
    def __init__(
        self,
        *,
        identity: tls.TlsIdentity,
        op_signer: OpSigner,
        hub_id: str,
        registry: Registry,
        invites: ops.InviteGate,
        snapshots: ops.SnapshotProvider,
        publish: ops.PublishForwarder | None = None,
        downstream: ops.Downstream | None = None,
        router: Router | None = None,
        access: Any = None,
        spaces: Any = None,
        audit: Any = None,
        wk_seals: WkSeals | None = None,
        views: Any = None,
        shares: Any = None,
        switch: Any = None,
        hooks: Any = None,
        activity: Any = None,
        where: Callable[[], dict[str, Any]] | None = None,
        closing: Any = None,
        deliveries: Any = None,
        streams: StreamSink | None = None,
        remote: RemoteEndpoint | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        dev_mode: bool,
        now_ms: Callable[[], int] = _now_ms,
        max_frame: int = framing.MAX_FRAME,
        handshake_timeout: float = 10.0,
        read_timeout: float = 10.0,
        idle_timeout: float = 300.0,
        unjoined_idle_timeout: float = 30.0,
        unjoined_lifetime: float = 30.0,
        max_connections: int = 64,
        max_unauthenticated: int = 16,
        max_remote_connections: int = 64,
        max_remote_unauthenticated: int = 16,
        remote_signin_timeout: float = 30.0,
        ws_host: str | None = None,
        ws_port: int = 0,
        ws_head_timeout: float = wsaccept.HEAD_TIMEOUT,
        ws_ping_after: float = W.PING_AFTER_S,
        ws_idle_close: float = W.IDLE_CLOSE_S,
    ) -> None:
        offered = (
            *((tls.REMOTE_ALPN,) if remote is not None else ()),
            *((tls.STREAM_LINK_ALPN,) if streams is not None else ()),
        )
        self._ctx = tls.server_context(identity, alpn=offered)
        self._streams, self._remote = streams, remote
        self._signer, self.hub_id, self._reg = op_signer, hub_id, registry
        self._invites, self._snaps = invites, snapshots
        self._publish = publish or ops.NoPublish()
        self._down = downstream or ops.NoDownstream()
        self._router = router
        self._access = access
        self._spaces = spaces
        self._audit = audit
        self._wk_seals = wk_seals
        self._views = views
        self._shares = shares
        self._switch = switch
        self._hooks = hooks
        self._activity = activity
        self._where = where
        self._closing = closing  # orch_hub.closing.Closing: the guard, the notice (#1463); None = never closing
        self._live: dict[
            str, set[_Port]
        ] = {}  # ws -> the plain links attached now (the closing notice goes out on them)
        self._live_mu = threading.Lock()
        self._deliveries = deliveries  # custodial.DeliveryQueue: event_deliver out, its replies in (§8.7)
        self._retry_at = float("-inf")
        self._dev, self._now = dev_mode, now_ms
        self._max, self._t_hs, self._t_rd, self._t_idle = max_frame, handshake_timeout, read_timeout, idle_timeout
        self._t_unjoined = unjoined_idle_timeout
        self._t_unjoined_life = unjoined_lifetime
        self._slots = threading.BoundedSemaphore(max_connections)
        # D12b G8: connections that have not finished auth (handshake included) hold one of these, so peers that stall
        # the handshake cannot take all the slots from bound hosts.
        self._pre = threading.BoundedSemaphore(min(max_unauthenticated, max_connections))
        # R4: remote clients take their own pools once the handshake shows the ALPN, so they never starve host links (G8).
        self._rslots = threading.BoundedSemaphore(max_remote_connections)
        self._rpre = threading.BoundedSemaphore(min(max_remote_unauthenticated, max_remote_connections))
        self._t_rsignin = remote_signin_timeout
        self._stop = threading.Event()
        self._listen = socket.socket()
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind((host, port))
        self._listen.listen(64)
        self._listen.settimeout(0.2)
        # R6 (#1594, protocol 10.10.7): the WebSocket listener is its own socket (never a sniff of the link port) and
        # shares the pools above. ``ws_host=None``: no WebSocket accept at all.
        self._ws_t = (ws_head_timeout, ws_ping_after, ws_idle_close)
        self._ws_listen: socket.socket | None = None
        if ws_host is not None:
            self._ws_listen = socket.socket(socket.AF_INET6 if ":" in ws_host else socket.AF_INET)
            self._ws_listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._ws_listen.bind((ws_host, ws_port))
            except OSError:
                self._ws_listen.close()
                self._listen.close()
                raise
            self._ws_listen.listen(64)
            self._ws_listen.settimeout(0.2)
        self._threads: list[threading.Thread] = []
        self._accept_thread = threading.Thread(
            target=self._accept_loop, args=(self._listen, False), name="link-accept", daemon=True
        )
        self._ws_accept_thread = (
            threading.Thread(target=self._accept_loop, args=(self._ws_listen, True), name="link-ws-accept", daemon=True)
            if self._ws_listen is not None
            else None
        )

    @classmethod
    def from_keys(
        cls,
        keys: HubKeys,
        *,
        identity: tls.TlsIdentity | None = None,
        ephemeral_tls: bool = False,
        snapshots: ops.SnapshotProvider | None = None,
        now_ms: Callable[[], int] = _now_ms,
        **kw: Any,
    ) -> LinkServer:
        """Start from a hub key store: ``HK_op`` signs the replies, the hub card pins the TLS key.

        The TLS key is the store's persistent one (``keys init`` / ``keys rotate-tls``): a restart never changes the pin.
        If the card does not name that key (or still carries the placeholder), the server refuses to start with
        ``tls_pin_mismatch``; a store without a TLS key is refused with ``tls_key_missing``. Only ``ephemeral_tls=True``
        (loopback tests) or an explicit ``identity=`` generates or accepts another key and re-issues the card (card_seq + 1)."""
        pinned = keys.card()["o"]["tls_spki_sha256"]
        if identity is None and not ephemeral_tls:
            identity = keys.tls_identity()
            if identity is None:
                raise KeysError("tls_key_missing", "this key store has no TLS key: run `orch-hub keys rotate-tls`")
            if pinned != identity.spki_sha256 or pinned == PLACEHOLDER_TLS:
                raise KeysError(
                    "tls_pin_mismatch", "the card does not name the stored TLS key: run `orch-hub keys rotate-tls`"
                )
        else:
            identity = identity or tls.TlsIdentity.generate()
            if pinned != identity.spki_sha256:
                keys.reissue_card(tls_spki_sha256=identity.spki_sha256)
        signer = keys.op_signer
        snapshots = snapshots or ops.StubSnapshots(signer, keys.hub_id, keys.op_cert()["o"]["cert_id"], now_ms)
        return cls(identity=identity, op_signer=signer, hub_id=keys.hub_id, snapshots=snapshots, now_ms=now_ms, **kw)

    @property
    def address(self) -> tuple[str, int]:
        return self._listen.getsockname()[:2]

    @property
    def ws_address(self) -> tuple[str, int] | None:
        """Where the WebSocket listener answers, ``None`` when there is none."""
        return None if self._ws_listen is None else self._ws_listen.getsockname()[:2]

    def start(self) -> LinkServer:
        self._accept_thread.start()
        if self._ws_accept_thread is not None:
            self._ws_accept_thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._accept_thread.join(5)
        if self._ws_accept_thread is not None:
            self._ws_accept_thread.join(5)
        for t in list(self._threads):
            t.join(5)
        self._listen.close()
        if self._ws_listen is not None:
            self._ws_listen.close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- connections ------------------------------------------------------------------------------------------

    def _accept_loop(self, listen: socket.socket, via_ws: bool) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = listen.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                sock.close()
                continue
            if not self._pre.acquire(blocking=False):
                sock.close()
                self._slots.release()
                continue
            t = threading.Thread(
                target=self._serve,
                args=(sock, _PreAuth(self._pre), _PreAuth(self._slots), via_ws),
                name="link-ws-conn" if via_ws else "link-conn",
                daemon=True,
            )
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
            t.start()

    def _serve(self, sock: socket.socket, pre: _PreAuth, slot: _PreAuth, via_ws: bool = False) -> None:
        conn = None
        try:
            if via_ws:  # the pool slots are held already: the head is read under them (slow upgrades cannot pile up)
                head_t, ping_after, idle_close = self._ws_t
                conn = wsaccept.accept_ws(
                    self._ctx,
                    sock,
                    time.monotonic() + self._t_hs,
                    self._stop,
                    head_timeout=head_t,
                    ping_after=ping_after,
                    idle_close=idle_close,
                )
            else:
                conn = tls.accept(self._ctx, sock, time.monotonic() + self._t_hs, self._stop)
            if self._remote is not None and conn.alpn() == tls.REMOTE_ALPN:
                self._serve_remote(conn, pre, slot)  # never reaches the link's auth
                return
            peer_addr = ""  # a WebSocket link has none: the socket peer is the front, and no header is believed
            if not via_ws:
                with contextlib.suppress(OSError):
                    peer_addr = str(sock.getpeername()[0])
            self._link(conn, pre, peer_addr)
        except framing.FrameError:
            if conn is not None:
                self._refuse(conn, "malformed")
        except LinkClosed, LinkTimeout:
            pass
        except Exception:  # noqa: BLE001, S110  # fail closed on anything unexpected; never echo it
            pass
        finally:
            if conn is not None:
                conn.close()
            else:
                sock.close()
            pre.release()
            slot.release()

    def _serve_remote(self, conn: tls.TlsConn, pre: _PreAuth, slot: _PreAuth) -> None:
        """A client connection (ALPN ``orch-remote/1``): leave the link pools, take the remote ones, hand the
        connection to the endpoint. Pool full: close, nothing is read."""
        pre.release()
        slot.release()
        if not self._rslots.acquire(blocking=False):
            return
        try:
            if not self._rpre.acquire(blocking=False):
                return
            rpre = _PreAuth(self._rpre)
            signed = threading.Event()

            def on_signed_in() -> None:
                signed.set()
                rpre.release()

            def deadline() -> None:  # fail closed: a client that never signed in loses its connection and slot
                if not signed.is_set():
                    conn.close()
                    rpre.release()

            conn.on_signed_in = on_signed_in
            timer = threading.Timer(self._t_rsignin, deadline)
            timer.daemon = True
            timer.start()
            try:
                assert self._remote is not None
                self._remote.serve(conn)
            except Exception as e:  # noqa: BLE001  # the endpoint must not raise; if it does, fail closed
                _log.warning("remote endpoint raised %s", type(e).__name__)
            finally:
                timer.cancel()
                rpre.release()
        finally:
            self._rslots.release()

    def _send(self, conn: tls.TlsConn, obj: dict[str, Any]) -> None:
        framing.write_frame(conn, obj, timeout=self._t_rd, max_bytes=self._max)

    def _recv(self, conn: tls.TlsConn, first: float) -> dict[str, Any]:
        return framing.read_frame(conn, first_timeout=first, rest_timeout=self._t_rd, max_bytes=self._max)

    def _refuse(self, conn: tls.TlsConn, code: str) -> None:
        try:
            self._send(conn, {"t": "error", "error": code})
        except LinkClosed, LinkTimeout, framing.FrameError:
            pass

    # --- the link ---------------------------------------------------------------------------------------------

    def _auth(self, conn: tls.TlsConn) -> tuple[str, bytes] | None:
        """Challenge, verify the auth frame. Returns (workspace_id, wsk_pub) or None after refusing."""
        challenge = os.urandom(32)
        self._send(conn, {"t": "challenge", "hub_id": self.hub_id, "challenge": b64u(challenge)})
        a = self._recv(conn, self._t_rd)
        try:
            keys = set(a)
            if a.get("t") != "auth" or not {"t", "workspace_id", "sig"} <= keys <= {
                "t",
                "workspace_id",
                "sig",
                "wsk_pub",
            }:
                raise Refused("malformed")
            ws, sig = a["workspace_id"], unb64u(a["sig"], 64)
            claimed = unb64u(a["wsk_pub"], 65) if "wsk_pub" in a else None
            unhex(ws, 16)  # validates the id
            pin = self._reg.pin(ws)
            if pin is None:
                if claimed is None:
                    raise Refused("auth_failed", why="unknown_workspace")
                wsk = claimed
            else:
                wsk = unb64u(pin["wsk_pub"], 65)
                if claimed is not None and claimed != wsk:
                    raise Refused("auth_failed", why="wsk_mismatch")
            msg = H.link_auth_bytes(self.hub_id, ws, challenge, conn.exporter())
            if not S.verify(wsk, sig, msg):
                raise Refused("auth_failed", why="bad_signature")
        except (Refused, CanonError) as e:
            if isinstance(e, Refused) and "why" in e.extra:  # the reason stays here, the peer sees one code
                _log.info("link auth refused: %s", e.extra["why"])
            self._refuse(conn, e.code if isinstance(e, Refused) else "malformed")
            return None
        self._send(conn, {"t": "ready", "hub_id": self.hub_id})
        return ws, wsk

    def _link(self, conn: tls.TlsConn, pre: _PreAuth, peer_addr: str = "") -> None:
        who = self._auth(conn)
        if who is None:
            return
        ws, wsk = who
        port = _Port(conn, self._signer, self.hub_id, ws, self._now, self._t_rd, self._max, conn.exporter())
        port.wsk = wsk
        port.stream_link = self._streams is not None and conn.alpn() == tls.STREAM_LINK_ALPN
        port.is_current = lambda: (
            self._bound_seq(ws, wsk) is not None
            and (port.attached_seq is None or self._bound_seq(ws, wsk) == port.attached_seq)
        )
        try:
            self._serve_link(conn, port, ws, wsk, pre, peer_addr)
        finally:
            self._detach(ws, port)

    def _bound_seq(self, ws: str, wsk: bytes) -> int | None:
        """The ``binding_seq`` of the live binding when ``wsk`` is the workspace's CURRENT pin and the binding is bound,
        else None (fail closed)."""
        try:
            pin = self._reg.pin(ws)
            st = self._reg.binding_state(ws)
            if pin is None or unb64u(pin["wsk_pub"], 65) != wsk or st is None or st["status"] != "bound":
                return None
            return int(st["binding_seq"])
        except Exception:  # noqa: BLE001  # fail closed
            return None

    def _stream_current(self, ws: str, port: _Port) -> bool:
        """The one gate before every forward to the stream sink (frames and ``stream_open`` replies): the port is
        attached to the sink AND the workspace is still bound under the ``binding_seq`` it attached under. A stale
        binding detaches the port (the sink closes its streams) and the caller drops the message. Logs ids only."""
        if self._streams is None or not port.streams_attached:
            return False
        seq = self._bound_seq(ws, port.wsk)
        if seq is not None and seq == port.attached_seq:
            return True
        _log.warning("stream binding changed ws=%s: frame dropped, port detached", ws)
        self._detach(ws, port)
        return False

    def push_closing(self, ws: str, notice: dict[str, Any]) -> bool:
        """Send the signed ``hub.closing`` notice (h2w ``closing_notice``) on every plain link of ``ws`` that is still its
        current bound one. Best effort: True if at least one write went out; a host that is offline pulls it (``closing_get``)."""
        with self._live_mu:
            ports = list(self._live.get(ws, ()))
        sent = False
        for port in ports:
            try:
                if self._bound_seq(ws, port.wsk) is not None and port.is_current():  # the current pin, still bound
                    port.send_request("closing_notice", {"notice": notice})
                    sent = True
            except Exception:  # noqa: BLE001, S112  # a dead link: the host pulls it
                continue
        return sent

    def _detach(self, ws: str, port: _Port) -> None:
        with self._live_mu:
            ports = self._live.get(ws)
            if ports is not None:
                ports.discard(port)
                if not ports:
                    del self._live[ws]
        if self._router is not None and port.live is not None:
            self._router.detach(ws, port.live)
            port.live = None
        if self._deliveries is not None and port.delivery is not None:
            self._deliveries.detach(ws, port.delivery)
            port.delivery = None
        if self._streams is not None and port.streams_attached:
            port.streams_attached = False
            try:
                self._streams.detach(ws, port)
            except Exception as e:  # noqa: BLE001  # a sink fault must not keep the link attached
                _log.warning("stream sink detach raised %s", type(e).__name__)
        port.attached_seq = None

    def _attach(self, ws: str, port: _Port) -> None:
        """Join the router's live set and the delivery queue once the workspace is pinned and bound with THIS link's
        key (queued ``route_in`` goes out now). The link stays attached only to the binding (``binding_seq``) and pin it
        attached under: a rotated pin, an unbind or a rebind detaches it, and a still-current link attaches afresh."""
        seq = self._bound_seq(ws, port.wsk)
        if port.attached_seq is not None and seq != port.attached_seq:
            self._detach(ws, port)
        if seq is None:
            return
        if not port.stream_link:  # a plain link never carries streams, a stream link never carries anything else
            if self._router is not None and port.live is None:
                port.live = self._router.attach(ws, port)
            if self._deliveries is not None and port.delivery is None:
                port.delivery = self._deliveries.attach(ws, port)
        if port.stream_link and not port.streams_attached:
            port.streams_attached = True
            try:
                self._streams.attach(ws, port)
            except Exception as e:  # noqa: BLE001
                _log.warning("stream sink attach raised %s", type(e).__name__)
        if port.live is not None or port.delivery is not None or port.streams_attached:
            port.attached_seq = seq
        if not port.stream_link:
            with self._live_mu:
                self._live.setdefault(ws, set()).add(port)

    def _pinned_to(self, ws: str, wsk: bytes) -> bool:
        pin = self._reg.pin(ws)
        return pin is not None and unb64u(pin["wsk_pub"], 65) == wsk

    def _serve_link(
        self, conn: tls.TlsConn, port: _Port, ws: str, wsk: bytes, pre: _PreAuth, peer_addr: str = ""
    ) -> None:
        # G8: a link counts as unauthenticated until the workspace is pinned to THIS link's key. A self-asserted key
        # (first link of a join) proves nothing, so it keeps its slot until the join pins it; one that dies sooner
        # than the lifetime below, or never joins, frees it at the end of the connection.
        if self._pinned_to(ws, wsk):
            pre.release()
        started = time.monotonic()
        if port.stream_link and self._bound_seq(ws, wsk) is None:
            self._refuse(conn, "not_bound")  # a stream link exists only for a pinned, bound workspace (no join on it)
            return
        if port.stream_link and self._closing is not None and self._closing.active():
            self._refuse(conn, "hub_closing")  # no new remote access stream link while the hub is being removed
            return
        self._attach(ws, port)
        last_seq = 0  # R1: the w2h seq of the previous frame on THIS link (the first frame must be 1)
        exporter = port._exporter  # the port's single copy: auth, h2w signing and w2h verification share it
        while not self._stop.is_set():
            first = self._t_idle
            if not self._pinned_to(ws, wsk):  # unjoined, or pinned to another key: a hard lifetime no peer frame resets
                left = self._t_unjoined_life - (time.monotonic() - started)
                if left <= 0:
                    return
                first = min(self._t_unjoined, left)
            try:
                signed = self._recv(conn, first)
            except LinkTimeout:
                return
            # The link's key must be the CURRENT pinned key: a link opened before the pin (offering its own key)
            # dies as soon as another link has pinned the workspace.
            pin = self._reg.pin(ws)
            if pin is not None and unb64u(pin["wsk_pub"], 65) != wsk:
                self._refuse(conn, "wsk_mismatch")
                return
            mode = (
                self._reg.mode(ws) or self._reg.last_mode(ws) or "selective"
            )  # unbound: its last mode, so ops get not_bound
            now = self._now()
            try:
                if _is_stream_frame(signed):  # one-way stream_data / stream_close (and a refused w2h stream_open)
                    o = R.verify_stream_link(
                        signed,
                        direction="w2h",
                        hub_id_=self.hub_id,
                        workspace_id=ws,
                        pub=wsk,
                        now_ms=now,
                        exporter=exporter,
                    )
                else:
                    o = H.verify_link(
                        signed,
                        direction="w2h",
                        hub_id_=self.hub_id,
                        workspace_id=ws,
                        mode=mode,
                        pub=wsk,
                        now_ms=now,
                        exporter=exporter,
                    )
            except Refused as e:
                self._log_refused(ws, signed, e.code, now)
                self._refuse(conn, e.code)
                return
            # §10.2 step 7: one counter per direction and connection, requests and replies alike. The first frame is 1,
            # then every frame is exactly the previous + 1. A repeat, a skip or a wrong start closes the link before
            # any side effect. The signature binds the connection's exporter (step 1), so a frame is accepted on one
            # connection, at one position, once.
            if o["seq"] != last_seq + 1:
                self._log_refused(ws, signed, "malformed", now)
                self._refuse(conn, "malformed")
                return
            last_seq = o["seq"]
            if port.stream_link and not (
                o["op"] in R.STREAM_OPS or (o["op"] == "hub_where" and o["re"] is None)
            ):  # a stream link carries stream frames, stream_open replies and the hub_where keepalive, nothing else
                self._log_refused(ws, signed, "unknown_op", now)
                self._refuse(conn, "unknown_op")
                return
            self._reg.seen(ws, now, peer_addr)
            if o["re"] is None and o["op"] in R.STREAM_OPS:
                # R4: a verified one-way stream frame. It gets NO reply, ever (the host would count it as a reply to a
                # request it never sent); a link with no stream sink drops it.
                if self._stream_current(ws, port):
                    try:
                        self._streams.on_frame(ws, port, o, R.frame_cost(signed))
                    except Exception as e:  # noqa: BLE001  # the link must survive a sink fault
                        _log.warning("stream sink on_frame raised %s", type(e).__name__)
                continue
            if o["re"] is not None and o["op"] == "stream_open":  # the host's answer to a stream_open we sent
                if self._stream_current(ws, port):
                    try:
                        self._streams.on_reply(ws, port, o)
                    except Exception as e:  # noqa: BLE001
                        _log.warning("stream sink on_reply raised %s", type(e).__name__)
                continue
            if o["re"] is not None:  # a reply to one of our h2w requests (the host's ack of a route_in): no answer
                if self._router is not None and port.live is not None:
                    self._router.on_reply(ws, port.live, o)
                if self._deliveries is not None and port.delivery is not None:
                    self._deliveries.on_reply(ws, port.delivery, o)
                continue
            was_unjoined = self._reg.pin(ws) is None
            body = self._dispatch(ws, wsk, o, now, conn.same_computer())
            if was_unjoined and self._pinned_to(ws, wsk):
                self._reg.seen(ws, now, peer_addr)
            port.send_signed(
                o["op"] if o["op"] in H.LINK_OPS else "error", o["seq"], body
            )  # never sign a peer-chosen string
            if self._pinned_to(ws, wsk):
                pre.release()  # the join pinned this link's key: now it is authenticated
            elif was_unjoined and o["op"] != "join":
                return  # an unjoined link may send one join; anything else ends it (after the refusal above)
            self._attach(ws, port)  # a join on this link just made the workspace routable

    def _count(self, ws: str, op: object, accepted: bool) -> None:
        """Dev-only aggregate op counters. Pinned workspaces get their own row; unjoined links share ws ``""``; an op
        outside the closed list is ``other``. Bounded: pinned workspaces x closed ops x 2, plus the fixed bucket."""
        if not self._dev:
            return
        name = op if type(op) is str and op in H.LINK_OPS else "other"
        self._reg.count_op(ws if self._reg.pin(ws) is not None else "", name, accepted)

    def _log_refused(self, ws: str, signed: Any, code: str, now: int) -> None:
        """Count a refused attempt of a known op (e.g. ``wk_seal`` in selective mode). Not when the signature failed:
        that is not the workspace speaking."""
        o = signed.get("o") if type(signed) is dict else None
        op = o.get("op") if type(o) is dict else None
        if code not in ("bad_signature", "wrong_suite") and type(op) is str and op in H.LINK_OPS:
            self._count(ws, op, False)

    def _dispatch(self, ws: str, wsk: bytes, o: dict[str, Any], now: int, same: bool = False) -> dict[str, Any]:
        body = self._run(ws, wsk, o, now, same)
        self._count(ws, o["op"], "error" not in body)
        return body

    def _run(self, ws: str, wsk: bytes, o: dict[str, Any], now: int, same: bool = False) -> dict[str, Any]:
        handler = ops.HANDLERS.get(o["op"])
        try:
            if RESTORE_FENCE.is_set():
                raise Refused("restore_busy")  # a restore is swapping the state dir; the host retries
            if self._closing is not None and self._closing.active() and o["op"] not in CLOSING_LINK_ALLOWED:
                raise Refused(
                    "hub_closing"
                )  # the hub is being removed: default deny (allow list), nothing else is served
            if handler is None:
                raise Refused("unknown_op")  # an op of a later phase: in the table, not served yet
            if o["op"] != "join" and self._reg.pin(ws) is None:
                raise Refused("unknown_workspace")  # an unjoined link may only join
            if o["op"] not in ("join", "binding_put", "status_put"):
                st = self._reg.binding_state(ws)
                if st is not None and st["status"] != "bound":
                    if self._shares is not None and time.monotonic() - self._retry_at >= 30.0:
                        self._retry_at = time.monotonic()  # at most once per 30 s, globally (registry lock held by it)
                        with contextlib.suppress(
                            Exception
                        ):  # D9-1: best effort; purge stale share items if the sink is back
                            self._shares.retry_pending()
                    raise Refused("not_bound")  # after hub.unbound only join and binding_put are served
            ctx = ops.OpContext(
                hub_id=self.hub_id,
                workspace_id=ws,
                link_wsk_pub=wsk,
                body=o["body"],
                registry=self._reg,
                invites=self._invites,
                snapshots=self._snaps,
                publish=self._publish,
                downstream=self._down,
                now_ms=now,
                dev_mode=self._dev,
                router=self._router,
                access=self._access,
                spaces=self._spaces,
                audit=self._audit,
                wk_seals=self._wk_seals,
                views=self._views,
                shares=self._shares,
                switch=self._switch,
                hooks=self._hooks,
                activity=self._activity,
                where=self._where,
                closing=self._closing,
                same_computer=same,
            )
            return handler(ctx)
        except Refused as e:
            return {"error": e.code}
        except CanonError, ValueError, KeyError, TypeError:
            return {"error": "malformed"}
        except Exception:  # noqa: BLE001
            return {"error": "internal"}


def _is_stream_frame(signed: Any) -> bool:
    """A w2h request whose op is a stream op: it verifies through ``remote.verify_stream_link``. Anything else (also a
    stream op with ``re`` set, which ``verify_link`` refuses as ``unknown_op`` unless it is a ``stream_open`` reply)
    goes through ``verify_link``."""
    o = signed.get("o") if type(signed) is dict else None
    return type(o) is dict and o.get("re") is None and type(o.get("op")) is str and o["op"] in R.STREAM_OPS


class _Port:
    """Writes the hub's signed h2w messages on one link. Replies (from the link's own thread) and ``route_in`` requests
    (from the router, on any thread) share the h2w ``seq`` counter and the write lock."""

    def __init__(self, conn, signer, hub_id, ws, now_ms, timeout, max_bytes, exporter) -> None:
        self._c, self._signer, self._hub, self._ws = conn, signer, hub_id, ws
        self._exporter = exporter  # this connection's TLS exporter: every h2w signature binds it (§10.2)
        self._now, self._t, self._max = now_ms, timeout, max_bytes
        self._lock = threading.Lock()
        self._seq = 0
        self.live = None
        self.delivery = None
        self.streams_attached = False
        self.stream_link = False  # ALPN orch-link-streams/1: attached to the stream sink ONLY, few ops (R4)
        self.wsk = b""
        self.attached_seq: int | None = None
        self.is_current: Callable[[], bool] = lambda: True

    def _send(self, op: str, re: int | None, body: dict[str, Any]) -> int:
        with self._lock:
            self._seq += 1
            o = {
                "v": 2,
                "suite": 2,
                "kind": "hub.link",
                "hv": H.HV,
                "hub_id": self._hub,
                "workspace_id": self._ws,
                "dir": "h2w",
                "seq": self._seq,
                "ts_ms": self._now(),
                "op": op,
                "re": re,
                "body": body,
            }
            try:
                framing.write_frame(
                    self._c, self._signer.sign_link(o, self._exporter), timeout=self._t, max_bytes=self._max
                )
            except BaseException:
                self._c.close()  # a half-written frame corrupts the stream: the link is over
                raise
            return self._seq

    def send_signed(self, op: str, re: int, body: dict[str, Any]) -> None:
        self._send(op, re, body)

    def send_request(self, op: str, body: dict[str, Any]) -> int:
        if not self.is_current():  # h2w requests only go to the link holding the current pin
            raise OSError("link is not current")
        return self._send(op, None, body)

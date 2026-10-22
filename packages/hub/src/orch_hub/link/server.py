"""The hub side of the host<->hub link (docs/protocol.md §10).

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
   verification (§10.2 steps 1-6) gets ``{"t":"error","error":code}`` and the link is closed. An op that fails on its
   own terms gets a signed reply with body ``{"error": code}`` and the link stays open.

Fail closed: any read timeout, oversize or malformed frame closes the link. SHORTCUT (L7): ``seq`` is carried and
type-checked, there is no replay store.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import Any, Self

from ..contract import hub as H
from ..contract import suite as S
from ..contract.canon import CanonError, b64u, unb64u, unhex
from ..contract.hub import Refused
from ..contract.keystore import OpSigner
from ..keys import HubKeys
from . import framing, ops, tls
from .registry import Registry
from .tls import LinkClosed, LinkTimeout

_log = logging.getLogger("orch_hub.link")


def _now_ms() -> int:
    return int(time.time() * 1000)


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
        host: str = "127.0.0.1",
        port: int = 0,
        dev_mode: bool = True,
        now_ms: Callable[[], int] = _now_ms,
        max_frame: int = framing.MAX_FRAME,
        handshake_timeout: float = 10.0,
        read_timeout: float = 10.0,
        idle_timeout: float = 300.0,
        unjoined_idle_timeout: float = 30.0,
        max_connections: int = 64,
    ) -> None:
        self._ctx = tls.server_context(identity)
        self._signer, self.hub_id, self._reg = op_signer, hub_id, registry
        self._invites, self._snaps = invites, snapshots
        self._publish = publish or ops.NoPublish()
        self._down = downstream or ops.NoDownstream()
        self._dev, self._now = dev_mode, now_ms
        self._max, self._t_hs, self._t_rd, self._t_idle = max_frame, handshake_timeout, read_timeout, idle_timeout
        self._t_unjoined = unjoined_idle_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        self._stop = threading.Event()
        self._listen = socket.socket()
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind((host, port))
        self._listen.listen(64)
        self._listen.settimeout(0.2)
        self._threads: list[threading.Thread] = []
        self._accept_thread = threading.Thread(target=self._accept_loop, name="link-accept", daemon=True)

    @classmethod
    def from_keys(
        cls,
        keys: HubKeys,
        *,
        identity: tls.TlsIdentity | None = None,
        snapshots: ops.SnapshotProvider | None = None,
        now_ms: Callable[[], int] = _now_ms,
        **kw: Any,
    ) -> LinkServer:
        """Start from a hub key store: ``HK_op`` signs the replies, the hub card pins the TLS key. If the held card
        does not carry this TLS identity's SPKI hash, the card is re-issued with it (card_seq + 1), so the card the
        hosts verify always names the certificate this server presents."""
        identity = identity or tls.TlsIdentity.generate()
        if keys.card()["o"]["tls_spki_sha256"] != identity.spki_sha256:
            keys.reissue_card(tls_spki_sha256=identity.spki_sha256)
        signer = keys.op_signer
        snapshots = snapshots or ops.StubSnapshots(signer, keys.hub_id, keys.op_cert()["o"]["cert_id"], now_ms)
        return cls(identity=identity, op_signer=signer, hub_id=keys.hub_id, snapshots=snapshots, now_ms=now_ms, **kw)

    @property
    def address(self) -> tuple[str, int]:
        return self._listen.getsockname()[:2]

    def start(self) -> LinkServer:
        self._accept_thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._accept_thread.join(5)
        for t in list(self._threads):
            t.join(5)
        self._listen.close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- connections ------------------------------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = self._listen.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                sock.close()
                continue
            t = threading.Thread(target=self._serve, args=(sock,), name="link-conn", daemon=True)
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
            t.start()

    def _serve(self, sock: socket.socket) -> None:
        conn = None
        try:
            conn = tls.accept(self._ctx, sock, time.monotonic() + self._t_hs, self._stop)
            self._link(conn)
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
            self._slots.release()

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

    def _link(self, conn: tls.TlsConn) -> None:
        who = self._auth(conn)
        if who is None:
            return
        ws, wsk = who
        seq = 0
        while not self._stop.is_set():
            pin = self._reg.pin(ws)
            try:
                signed = self._recv(conn, self._t_idle if pin is not None else self._t_unjoined)
            except LinkTimeout:
                return
            # The link's key must be the CURRENT pinned key: a link opened before the pin (offering its own key)
            # dies as soon as another link has pinned the workspace.
            pin = self._reg.pin(ws)
            if pin is not None and unb64u(pin["wsk_pub"], 65) != wsk:
                self._refuse(conn, "wsk_mismatch")
                return
            mode = self._reg.mode(ws) or "selective"
            now = self._now()
            try:
                o = H.verify_link(
                    signed, direction="w2h", hub_id_=self.hub_id, workspace_id=ws, mode=mode, pub=wsk, now_ms=now
                )
            except Refused as e:
                self._log_refused(ws, signed, e.code, now)
                self._refuse(conn, e.code)
                return
            seq += 1
            body = self._dispatch(ws, wsk, o, now)
            reply = {
                "v": 2,
                "suite": 2,
                "kind": "hub.link",
                "hv": H.HV,
                "hub_id": self.hub_id,
                "workspace_id": ws,
                "dir": "h2w",
                "seq": seq,
                "ts_ms": now,
                "op": o["op"] if o["op"] in H.LINK_OPS else "error",  # never sign a peer-chosen string
                "re": o["seq"],
                "body": body,
            }
            self._send(conn, self._signer.sign_object(reply))

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

    def _dispatch(self, ws: str, wsk: bytes, o: dict[str, Any], now: int) -> dict[str, Any]:
        body = self._run(ws, wsk, o, now)
        self._count(ws, o["op"], "error" not in body)
        return body

    def _run(self, ws: str, wsk: bytes, o: dict[str, Any], now: int) -> dict[str, Any]:
        handler = ops.HANDLERS.get(o["op"])
        try:
            if handler is None:
                raise Refused("unknown_op")  # an op of a later phase: in the table, not served yet
            if o["op"] != "join" and self._reg.pin(ws) is None:
                raise Refused("unknown_workspace")  # an unjoined link may only join
            if o["op"] not in ("join", "binding_put"):
                st = self._reg.binding_state(ws)
                if st is not None and st["status"] != "bound":
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
            )
            return handler(ctx)
        except Refused as e:
            return {"error": e.code}
        except CanonError, ValueError, KeyError, TypeError:
            return {"error": "malformed"}
        except Exception:  # noqa: BLE001
            return {"error": "internal"}

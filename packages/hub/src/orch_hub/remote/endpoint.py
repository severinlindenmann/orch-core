"""The remote-client endpoint: the client protocol on ONE connection (R4 design §1). Transport-agnostic: it needs only
``recv_exact``, ``send_all`` and ``close`` of the connection (a TLS connection today, a tunnel carrier in R6).

One stream per connection: the stream ends when the connection ends and the reverse; the client never names a stream
id. Frames (4-byte length + strict canonical JSON, at most 96 KiB), exact keys everywhere, else ``malformed``
(contract: protocol.md §10.2d, reference ``orch_hub.contract.remote.ClientConn``, vectors ``client_*``)::

    c->h {"t":"hello","v":1,"token"}  h->c {"t":"ready","hub_id"} | {"t":"error","error"} + close
    c->h {"t":"rail"} (optional, once)  h->c {"t":"rail","workspaces":[{workspace_id,name,online}],"more"} | error
    c->h {"t":"open","workspace_id","device_id","kind"}  h->c {"t":"opened","stream_id"} | {"t":"refused","error"} + close
    c<->h {"t":"data","b":b64u(1..65535)}     c<->h {"t":"close","reason"}  (reason in CLOSE_REASONS)

Fail closed: any parse error, unexpected ``t``, data before ``opened`` or a second ``open`` ends the stream and the
connection. The hub never logs frame bytes, the token or the device id.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ..contract import remote as R
from ..contract.canon import CanonError, b64u, unb64u
from ..link import framing
from ..link.tls import LinkClosed, LinkTimeout
from .authz import Authz, AuthzError
from .streams import Refused, StreamRouter

_log = logging.getLogger("orch_hub.remote")
MAX_CLIENT_FRAME = 96 * 1024
_WS = re.compile(r"[0-9a-f]{32}")
_B64_MAX = -(-R.FRAME_MAX * 4 // 3)  # the longest legal encoded Noise message


class _Sink:
    """Host -> client frames: a bounded queue drained in order by one writer thread. Never blocks the link thread."""

    def __init__(self, conn: Any, limit: int, timeout: float) -> None:
        self._conn, self._limit, self._t = conn, limit, timeout
        self._q: deque[tuple[str, Any]] = deque()
        self._cv = threading.Condition()
        self._ended = False
        self.on_fail: Any = None

    def offer(self, data: bytes) -> bool:
        with self._cv:
            if self._ended:
                return True
            if sum(1 for k, _ in self._q if k == "d") >= self._limit:
                return False
            self._q.append(("d", data))
            self._cv.notify()
            return True

    def end(self, reason: str) -> None:
        with self._cv:
            if not self._ended:
                self._ended = True
                self._q.append(("c", reason))
                self._cv.notify()

    def run(self) -> None:
        try:
            while True:
                with self._cv:
                    while not self._q:
                        self._cv.wait()
                    kind, v = self._q.popleft()
                frame = {"t": "data", "b": b64u(v)} if kind == "d" else {"t": "close", "reason": v}
                framing.write_frame(self._conn, frame, timeout=self._t, max_bytes=MAX_CLIENT_FRAME)
                if kind == "c":
                    return
        except LinkClosed, LinkTimeout, framing.FrameError, OSError:
            if self.on_fail is not None:
                self.on_fail()
        finally:
            self._conn.close()


class _Once:
    """One slot of the not-yet-signed-in pool; :meth:`release` is idempotent."""

    def __init__(self, sem: threading.BoundedSemaphore) -> None:
        self._sem, self._held = sem, False

    def take(self) -> bool:
        self._held = self._sem.acquire(blocking=False)
        return self._held

    def release(self) -> None:
        if self._held:
            self._held = False
            self._sem.release()


class RemoteEndpoint:
    def __init__(
        self,
        router: StreamRouter,
        authz: Authz,
        *,
        max_connections: int = 64,
        max_unauthenticated: int = 16,
        max_idle_per_user: int = 8,
        setup_timeout: float = 30.0,
        write_timeout: float = 10.0,
        read_timeout: float = 10.0,
        idle_poll: float = 30.0,
        queue_frames: int | None = None,
    ) -> None:
        self._router, self._authz = router, authz
        self._conns = threading.BoundedSemaphore(max_connections)
        self._pre = threading.BoundedSemaphore(min(max_unauthenticated, max_connections))
        self._t_setup, self._t_w, self._t_r, self._poll = setup_timeout, write_timeout, read_timeout, idle_poll
        self._idle_cap, self._idle_users, self._idle_mu = max_idle_per_user, {}, threading.Lock()
        self._queue = queue_frames if queue_frames is not None else router.lim.queue_frames

    def serve(self, conn: Any) -> None:
        """Own ``conn`` until the stream ends. Never raises."""
        if not self._conns.acquire(blocking=False):
            conn.close()
            return
        pre = _Once(self._pre)
        try:
            if not pre.take():
                return
            try:
                self._serve(conn, pre)
            except LinkClosed, LinkTimeout, framing.FrameError, OSError:
                pass
            except Exception as e:  # noqa: BLE001  # fail closed; the type only, never the message
                _log.warning("remote_endpoint_error type=%s", type(e).__name__)
        finally:
            pre.release()
            self._conns.release()
            conn.close()

    def _idle_take(self, user: str) -> bool:
        with self._idle_mu:
            n = self._idle_users.get(user, 0)
            if n >= self._idle_cap:
                return False
            self._idle_users[user] = n + 1
            return True

    def _idle_release(self, user: str) -> None:
        with self._idle_mu:
            n = self._idle_users.get(user, 0) - 1
            if n > 0:
                self._idle_users[user] = n
            else:
                self._idle_users.pop(user, None)

    def _recheck(self, token: str, user: str, ws: str) -> Callable[[], bool]:
        """The router sweep's question for one stream: the token that opened it is still live for the same user, and that
        user may still reach ``ws``. Fails closed."""

        def alive() -> bool:
            try:
                return self._authz.authenticate(token) == user and self._authz.may_open(user, ws)
            except AuthzError:
                return False

        return alive

    # --- one connection ----------------------------------------------------------------------------------------

    def _read(self, conn: Any, deadline: float) -> dict[str, Any]:
        left = max(0.001, deadline - time.monotonic())
        return framing.read_frame(conn, first_timeout=left, rest_timeout=self._t_r, max_bytes=MAX_CLIENT_FRAME)

    def _send(self, conn: Any, obj: dict[str, Any]) -> None:
        framing.write_frame(conn, obj, timeout=self._t_w, max_bytes=MAX_CLIENT_FRAME)

    def _fail(self, conn: Any, code: str) -> None:
        try:
            self._send(conn, {"t": "error", "error": code})
        except LinkClosed, LinkTimeout, framing.FrameError, OSError:
            pass

    def _refuse(self, conn: Any, code: str) -> None:
        try:
            self._send(conn, {"t": "refused", "error": code})
        except LinkClosed, LinkTimeout, framing.FrameError, OSError:
            pass

    def _rail(self, conn: Any, token: str) -> bool:
        """Answer ``rail``: the person's rail rows (only id, name, online), re-checking the token first. False when the
        connection ends here (``error``)."""
        try:
            user = self._authz.authenticate(token)
        except AuthzError as e:
            self._fail(conn, e.code)
            return False
        try:
            msg = R.rail_message(self._authz.rail_rows(user))
            R.client_msg_check("h2c", msg)
        except Exception as e:  # noqa: BLE001  # fail closed: no partial or unchecked rail
            _log.warning("remote_rail_error type=%s", type(e).__name__)
            self._fail(conn, "unavailable")
            return False
        self._send(conn, msg)
        return True

    def _serve(self, conn: Any, pre: _Once) -> None:
        deadline = time.monotonic() + self._t_setup
        try:
            hello = self._read(conn, deadline)
        except framing.FrameError:
            self._fail(conn, "malformed")
            return
        if (
            set(hello) != {"t", "v", "token"}
            or hello["t"] != "hello"
            or hello["v"] != 1
            or type(hello["token"]) is not str
        ):
            self._fail(conn, "malformed")
            return
        try:
            user = self._authz.authenticate(hello["token"])
        except AuthzError as e:
            self._fail(conn, e.code)
            return
        if not self._idle_take(user):  # per-user cap on signed-in connections that have not opened a stream yet
            self._fail(conn, "remote.busy")
            return
        idle_user, sid = user, ""
        sink: _Sink | None = None
        try:
            self._send(conn, {"t": "ready", "hub_id": self._router.hub_id})
            pre.release()  # signed in: out of the not-yet-authenticated pool
            signed_in = getattr(conn, "signed_in", None)
            if signed_in is not None:
                signed_in()  # the link server's own slot too (idempotent)
            try:
                op = self._read(conn, deadline)
                if op == {"t": "rail"}:  # at most once, before open (§10.2d)
                    if not self._rail(conn, hello["token"]):
                        return
                    op = self._read(conn, deadline)
            except framing.FrameError:
                self._refuse(conn, "malformed")
                return
            if set(op) != {"t", "workspace_id", "device_id", "kind"} or op["t"] != "open":
                self._refuse(conn, "malformed")  # also a second rail, data or close before opened
                return
            ws, dev, kind = op["workspace_id"], op["device_id"], op["kind"]
            want = R.DEVICE_ID if kind == "session" else R.CODE_ID
            if (
                type(ws) is not str
                or _WS.fullmatch(ws) is None
                or kind not in ("session", "enroll")
                or type(dev) is not str
                or want.fullmatch(dev) is None
            ):
                self._refuse(conn, "malformed")
                return
            try:
                user = self._authz.authenticate(hello["token"])  # re-checked at every open (removed users, sign-out)
            except AuthzError as e:
                self._fail(conn, e.code)
                return
            if not self._authz.may_open(user, ws):
                self._refuse(conn, "not_allowed")
                return
            sink = _Sink(conn, self._queue, self._t_w)
            try:
                sid = self._router.open(ws, user, dev, kind, sink, self._recheck(hello["token"], user, ws))
            except Refused as r:
                self._refuse(conn, r.code)
                return
        finally:
            self._idle_release(idle_user)
        assert sink is not None
        sink.on_fail = lambda: self._router.close(sid, "closed", by="client")
        try:
            self._send(conn, {"t": "opened", "stream_id": sid})
        except LinkClosed, LinkTimeout, framing.FrameError, OSError:
            self._router.close(sid, "closed", by="client")
            return
        writer = threading.Thread(target=sink.run, name="remote-writer", daemon=True)
        writer.start()
        try:
            self._pump(conn, sid)
        finally:
            self._router.close(sid, "closed", by="client")  # no-op when it already ended another way
            writer.join(self._t_w + 2)

    def _pump(self, conn: Any, sid: str) -> None:
        """Client -> host until the stream or the connection ends."""
        while True:
            try:
                f = framing.read_frame(
                    conn, first_timeout=self._poll, rest_timeout=self._t_r, max_bytes=MAX_CLIENT_FRAME
                )
            except LinkTimeout:
                if self._router.alive(sid):
                    continue
                return
            except framing.FrameError:
                self._router.close(sid, "malformed", by="hub")
                return
            except LinkClosed, OSError:
                return
            t = f.get("t")
            if t == "data" and set(f) == {"t", "b"}:
                b = f["b"]
                if type(b) is str and len(b) > _B64_MAX:
                    self._router.close(sid, "too_large", by="hub")
                    return
                try:
                    raw = unb64u(b)
                except CanonError:
                    self._router.close(sid, "malformed", by="hub")
                    return
                if not raw:
                    self._router.close(sid, "malformed", by="hub")
                    return
                if len(raw) > R.FRAME_MAX:
                    self._router.close(sid, "too_large", by="hub")
                    return
                try:
                    self._router.send(sid, raw)
                except Refused:
                    return
            elif t == "close" and set(f) == {"t", "reason"} and f["reason"] in R.CLOSE_REASONS:
                self._router.close(sid, f["reason"], by="client")
                return
            else:  # unknown t, extra or missing keys, a second open, a bad close reason
                self._router.close(sid, "malformed", by="hub")
                return

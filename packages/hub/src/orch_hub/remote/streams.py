"""The hub's stream router for remote access (R4, #1592; design note ``state/devA/r4-router-design.md`` §2-§3).

The hub is UNTRUSTED for the session (Noise IK runs device <-> host; the hub can only drop or break streams) and
TRUSTED for abuse limits (the link to a workspace must stay healthy). It holds no key, no Noise state and no
plaintext, and it never logs frame bytes, a token or a device id: only ids, sizes and timing.

One :class:`StreamRouter` keeps, per workspace, the live link port and a table ``stream_id -> stream``. A stream
belongs to the port that carried its ``stream_open``; a w2h frame is matched by id AND port, so another workspace's
link can never inject into or close a stream it does not own. A link change (:meth:`detach`) closes every stream of
the old port; nothing is ever resumed.

Locks: ``_mu`` guards the table and counters; each port has its own ``lock`` so that the open reply cannot overtake the
bookkeeping of the request that caused it. Order is always port lock, then ``_mu``. Frames are sent with no lock held
except the port lock of an open.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..backup.hubop import FENCE as RESTORE_FENCE
from ..contract import hub as H
from ..contract import remote as R
from ..contract.canon import b64u, unb64u

_log = logging.getLogger("orch_hub.remote")

#: Host answers that reach the client verbatim; anything else (free text, an unknown code) is ``unavailable``.
HOST_CODES = frozenset({"remote.off", "remote.device", "remote.busy", "remote.no_code", "replay"})
WINDOW_S = 60.0


class Refused(Exception):
    """An open (or a send) the router refuses; ``code`` is what the client sees."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Limits:
    """Defaults per design §3. The hub may only lower the host's limits (4 per device, 16 per workspace)."""

    frame_max: int = R.FRAME_MAX
    per_device: int = R.LIMITS["streams_per_device"]
    per_workspace: int = R.LIMITS["streams_per_workspace"]
    per_user: int = 8
    hub_wide: int = 256
    opens_per_device_min: int = 10
    enroll_opens_per_ws_min: int = 3
    opens_per_user_min: int = 20
    life_bytes: int = R.BYTE_BUDGET["life"]
    min_bytes: int = R.BYTE_BUDGET["per_min"]
    link_bytes_per_min: int = 128 << 20
    queue_frames: int = 64
    life_s: float = 12 * 3600 + 300
    open_timeout_s: float = 30.0


class ClientSink(Protocol):
    """The client end of one stream. Both calls are non-blocking and never raise (they run on the link's read thread)."""

    def offer(self, data: bytes) -> bool:
        """Queue one host frame for the client; False when the client's queue is full."""

    def end(self, reason: str) -> None:
        """The stream is over: tell the client ``reason`` and close its connection."""


class _Window:
    """A fixed one-minute window plus a lifetime total, for one direction of one stream (or one link)."""

    __slots__ = ("life", "start", "used")

    def __init__(self) -> None:
        self.life, self.start, self.used = 0, 0.0, 0

    def charge(self, now: float, cost: int, min_cap: int, life_cap: int | None) -> bool:
        if now - self.start >= WINDOW_S:
            self.start, self.used = now, 0
        if self.used + cost > min_cap or (life_cap is not None and self.life + cost > life_cap):
            return False
        self.used += cost
        self.life += cost
        return True


@dataclass
class _PortState:
    port: Any
    lock: threading.Lock = field(default_factory=threading.Lock)
    window: _Window = field(default_factory=_Window)  # all stream frames of this link, both directions


@dataclass(eq=False)
class _Stream:
    stream_id: str
    ws: str
    pst: _PortState
    user: str
    kind: str
    device_id: str
    sink: ClientSink
    t_open: float
    state: str = "opening"  # opening | open | closed
    seq_out: int = 0  # last h2w stream_data seq
    seq_in: int = 0  # last w2h stream_data seq
    win_out: _Window = field(default_factory=_Window)
    win_in: _Window = field(default_factory=_Window)
    frames_in: int = 0
    frames_out: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    done: threading.Event = field(default_factory=threading.Event)
    result: str = ""  # "" = host accepted; else the code the client sees
    abandoned: bool = False
    #: the sweep's per-stream question: "is whatever authorised this stream still true?" (token live, user active, rail)
    recheck: Callable[[], bool] | None = None


class StreamRouter:
    def __init__(
        self,
        *,
        hub_id: str,
        limits: Limits | None = None,
        allowed: Callable[[str, str], bool] | None = None,
        count_op: Callable[[str, str, bool], None] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hub_id = hub_id
        self.lim = limits or Limits()
        self._allowed = allowed or (lambda user, ws: True)  # the sweep re-asks it: user removed / left the rail
        self._count = count_op
        self._now = now
        self._mu = threading.Lock()
        self._ports: dict[str, _PortState] = {}
        self._streams: dict[str, _Stream] = {}
        self._pending: dict[tuple[int, int], _Stream] = {}  # (id(port state), h2w seq of the stream_open) -> stream
        self._opens: dict[tuple[str, str], deque[float]] = {}  # ("dev"|"enr"|"usr", key) -> open times
        self.dropped = 0  # frames for an unknown stream or another port's stream (a count only, never logged per frame)
        self._stop = threading.Event()
        self._sweeper: threading.Thread | None = None

    # --- the link side (the LinkServer's StreamSink) ---------------------------------------------------------------

    def attach(self, ws: str, port: Any) -> None:
        with self._mu:
            old = self._ports.get(ws)
            if old is not None and old.port is port:
                return
        if old is not None:
            self.detach(ws, old.port)
        with self._mu:
            self._ports[ws] = _PortState(port)

    def detach(self, ws: str, port: Any) -> None:
        """The link is gone, rotated or unbound: every stream of that port ends ``closed`` (no h2w frame: the link is
        gone). A new link is a new port, so old ids never match it and nothing is resumed."""
        with self._mu:
            st = self._ports.get(ws)
            if st is not None and st.port is port:
                del self._ports[ws]
            mine = [s for s in self._streams.values() if s.pst.port is port]
            for k in [k for k, s in self._pending.items() if s.pst.port is port]:
                del self._pending[k]
        for s in mine:
            self._close(s, "closed", by="hub", tell_host=False)

    def on_reply(self, ws: str, port: Any, o: dict[str, Any]) -> None:
        """A verified w2h reply (``re`` = our h2w seq). Only ``stream_open`` replies matter here. Never raises."""
        try:
            with self._mu:
                st = self._ports.get(ws)
            if st is None or st.port is not port:
                return
            with st.lock:  # the opener registers the seq under this lock, so the reply cannot come first
                with self._mu:
                    s = self._pending.pop((id(st), o["re"]), None)
                if s is None:
                    return
                body = o.get("body")
                if body == {}:
                    code = ""
                elif type(body) is dict and set(body) == {"error"} and body["error"] in HOST_CODES:
                    code = body["error"]
                else:
                    code = "unavailable"
                with self._mu:
                    gone = s.state == "closed" or s.abandoned
                    if not gone:
                        s.result = code
                        if not code:
                            s.state = "open"
                if gone:
                    if not code:  # the host opened it after we gave up: close its side
                        self._tell_host(s, "closed")
                    return
                if code:
                    self._drop(s)
                s.done.set()
        except Exception as e:  # noqa: BLE001  # runs on the link thread; fail closed, name only the type
            _log.warning("stream_reply_error type=%s", type(e).__name__)

    def on_frame(self, ws: str, port: Any, o: dict[str, Any], cost: int) -> None:
        """A verified w2h ``stream_data`` / ``stream_close`` (exporter-bound, link seq checked by the link). Never blocks
        on a client, never raises."""
        try:
            b = o["body"]
            with self._mu:
                s = self._streams.get(b["stream_id"])
                if s is None or s.pst.port is not port or s.ws != ws or s.state == "closed":
                    self.dropped += 1  # unknown, closed or ANOTHER link's id: dropped, nothing else
                    return
                over = not s.pst.window.charge(self._now(), cost, self.lim.link_bytes_per_min, None)
            if o["op"] == "stream_close":
                self._close(s, b["reason"], by="host", tell_host=False)
                return
            if s.state != "open" or b["seq"] != s.seq_in + 1:
                self._close(s, "malformed", by="hub")
                return
            if over or not s.win_in.charge(self._now(), cost, self.lim.min_bytes, self.lim.life_bytes):
                self._close(s, "remote.busy", by="hub")
                return
            s.seq_in = b["seq"]
            data = unb64u(b["bytes"])
            s.frames_in += 1
            s.bytes_in += cost
            if not s.sink.offer(data):  # the client cannot keep up: this stream ends, the link thread never waits
                self._close(s, "remote.busy", by="hub")
        except Exception as e:  # noqa: BLE001
            _log.warning("stream_frame_error type=%s", type(e).__name__)

    # --- the client side -------------------------------------------------------------------------------------------

    def open(
        self,
        ws: str,
        user: str,
        device_id: str,
        kind: str,
        sink: ClientSink,
        recheck: Callable[[], bool] | None = None,
    ) -> str:
        """Open a stream to ``ws`` for hub user ``user`` (already authorized by the caller). Blocks up to the open timeout
        for the host's answer. Returns the stream id; raises :class:`Refused` with the code the client sees.

        Invariant: a remote stream lives only while the token that opened it is live. ``recheck`` is the caller's closure
        for that (token still valid for ``user``, user still allowed to reach ``ws``); the sweep is the ONE place that
        calls it, in addition to the router's own ``allowed`` rail check."""
        try:
            sid = self._open(ws, user, device_id, kind, sink, recheck)
        except Refused as r:
            _log.info("stream_open ws=%s stream=- kind=%s user=%s result=%s", ws, kind, user, r.code)
            self._counted(ws, False)
            raise
        _log.info("stream_open ws=%s stream=%s kind=%s user=%s result=ok", ws, sid, kind, user)
        self._counted(ws, True)
        return sid

    def _counted(self, ws: str, accepted: bool) -> None:
        """The registry's aggregate op counter (``orch-hub inspect``); never raises into the open path."""
        if self._count is not None:
            try:
                self._count(ws, "stream_open", accepted)
            except Exception:  # noqa: BLE001, S110
                pass

    def _open(
        self, ws: str, user: str, device_id: str, kind: str, sink: ClientSink, recheck: Callable[[], bool] | None
    ) -> str:
        if RESTORE_FENCE.is_set():
            raise Refused("unavailable")
        lim, now = self.lim, self._now()
        with self._mu:
            st = self._ports.get(ws)
            if st is None:
                raise Refused("not_allowed")  # no live link: the same answer as an invisible workspace
            streams = list(self._streams.values())
            if (
                len(streams) >= lim.hub_wide
                or sum(1 for s in streams if s.ws == ws) >= lim.per_workspace
                or sum(1 for s in streams if s.ws == ws and s.device_id == device_id) >= lim.per_device
                or sum(1 for s in streams if s.user == user) >= lim.per_user
            ):
                raise Refused("remote.busy")
            rates = [("dev", ws + device_id, lim.opens_per_device_min), ("usr", user, lim.opens_per_user_min)]
            if kind == "enroll":
                rates.append(("enr", ws, lim.enroll_opens_per_ws_min))
            qs = []
            for k, key, cap in rates:
                q = self._opens.setdefault((k, key), deque())
                while q and now - q[0] >= WINDOW_S:
                    q.popleft()
                if len(q) >= cap:
                    raise Refused("remote.busy")
                qs.append(q)
            for q in qs:
                q.append(now)
            sid = os.urandom(16).hex()
            while sid in self._streams:
                sid = os.urandom(16).hex()
            s = _Stream(sid, ws, st, user, kind, device_id, sink, now, recheck=recheck)
            self._streams[sid] = s
        try:
            with st.lock:
                seq = st.port.send_request("stream_open", {"stream_id": sid, "device_id": device_id, "kind": kind})
                with self._mu:
                    if s.state == "closed":  # detached while we were writing
                        raise Refused("unavailable")
                    self._pending[(id(st), seq)] = s
        except Refused:
            self._drop(s)
            raise
        except Exception:  # noqa: BLE001  # the link broke under the write
            self._drop(s)
            raise Refused("unavailable") from None
        if not s.done.wait(lim.open_timeout_s):
            with self._mu:
                answered = s.done.is_set()
                if not answered:
                    # gave up: out of the table, but the pending entry stays, so a late "opened" is answered with a
                    # h2w stream_close (the host must not keep a stream nobody holds)
                    s.abandoned, s.state = True, "closed"
                    self._streams.pop(s.stream_id, None)
            if not answered:
                raise Refused("unavailable")
        if s.state == "closed" and not s.result:
            raise Refused("unavailable")  # the link went away between the answer and now
        if s.result:
            raise Refused(s.result)
        return sid

    def send(self, stream_id: str, data: bytes) -> None:
        """One client frame (one Noise message) to the host. Synchronous: a slow host back-pressures this one client.
        Raises :class:`Refused` when the stream is over (the caller ends the connection)."""
        with self._mu:
            s = self._streams.get(stream_id)
            if s is None or s.state != "open":
                raise Refused("closed")
        if not 1 <= len(data) <= self.lim.frame_max:
            self._close(s, "too_large" if data else "malformed", by="hub")
            raise Refused("closed")
        cost = self._out_cost(s, len(data))
        with self._mu:
            ok = s.win_out.charge(self._now(), cost, self.lim.min_bytes, self.lim.life_bytes) and (
                s.pst.window.charge(self._now(), cost, self.lim.link_bytes_per_min, None)
            )
            s.seq_out += 1
            seq = s.seq_out
            s.frames_out += 1
            s.bytes_out += cost
        if not ok:
            self._close(s, "remote.busy", by="hub")
            raise Refused("closed")
        try:
            s.pst.port.send_request("stream_data", {"stream_id": stream_id, "seq": seq, "bytes": b64u(data)})
        except Exception:  # noqa: BLE001  # the link broke under the write
            self._close(s, "closed", by="hub", tell_host=False)
            raise Refused("closed") from None

    def close(self, stream_id: str, reason: str, by: str = "client") -> bool:
        """End a stream (idempotent). ``by``: who asked, for the log; the host is told unless it asked itself."""
        with self._mu:
            s = self._streams.get(stream_id)
        if s is None:
            return False
        return self._close(s, reason, by=by, tell_host=by != "host")

    def alive(self, stream_id: str) -> bool:
        with self._mu:
            s = self._streams.get(stream_id)
            return s is not None and s.state != "closed"

    def open_count(self) -> int:
        with self._mu:
            return len(self._streams)

    # --- shared ----------------------------------------------------------------------------------------------------

    def _out_cost(self, s: _Stream, n: int) -> int:
        """The cost of the h2w frame we are about to send: :func:`frame_cost` of a frame of the same shape with every
        variable number at its widest, so it never under-counts the real signed frame (F5)."""
        o = {
            "v": 2,
            "suite": 2,
            "kind": "hub.link",
            "hv": H.HV,
            "hub_id": self.hub_id,
            "workspace_id": s.ws,
            "dir": "h2w",
            "seq": 2**53 - 1,
            "ts_ms": 2**53 - 1,
            "op": "stream_data",
            "re": None,
            "body": {"stream_id": s.stream_id, "seq": 2**53 - 1, "bytes": "A" * (-(-n * 4 // 3))},
        }
        return R.frame_cost({"o": o, "sig": "A" * 86})

    def _drop(self, s: _Stream) -> None:
        """Forget a stream that never opened (no frames, no log, no client frame: the opener reports it)."""
        with self._mu:
            s.state = "closed"
            self._streams.pop(s.stream_id, None)
            for k in [k for k, v in self._pending.items() if v is s]:
                del self._pending[k]

    def _tell_host(self, s: _Stream, reason: str) -> None:
        try:
            s.pst.port.send_request("stream_close", {"stream_id": s.stream_id, "reason": reason})
        except Exception:  # noqa: BLE001, S110  # link gone: nothing to tell
            pass

    def _close(self, s: _Stream, reason: str, *, by: str, tell_host: bool = True) -> bool:
        """The one close path: idempotent; the table and counters are updated first, frames go out after."""
        with self._mu:
            if s.state == "closed":
                return False
            s.state = "closed"
            self._streams.pop(s.stream_id, None)
            for k in [k for k, v in self._pending.items() if v is s]:
                del self._pending[k]
        if tell_host:
            self._tell_host(s, reason)
        s.sink.end(reason)
        s.done.set()
        _log.info(
            "stream_close stream=%s reason=%s by=%s frames_in=%d frames_out=%d bytes_in=%d bytes_out=%d ms=%d",
            s.stream_id,
            reason,
            by,
            s.frames_in,
            s.frames_out,
            s.bytes_in,
            s.bytes_out,
            int((self._now() - s.t_open) * 1000),
        )
        return True

    # --- housekeeping ----------------------------------------------------------------------------------------------

    def revoke_user(self, user: str) -> int:
        with self._mu:
            mine = [s for s in self._streams.values() if s.user == user]
        return sum(self._close(s, "revoked", by="hub") for s in mine)

    def sweep(self) -> None:
        """Expire streams past their life, close those whose user lost access (``revoked``), forget stale pending opens."""
        now = self._now()
        with self._mu:
            streams = list(self._streams.values())
        for s in streams:
            if s.state == "closed":
                continue
            if now - s.t_open > self.lim.life_s:
                self._close(s, "expired", by="hub")
                continue
            try:
                ok = self._allowed(s.user, s.ws) and (s.recheck is None or bool(s.recheck()))
            except Exception:  # noqa: BLE001  # fail closed
                ok = False
            if not ok:
                self._close(s, "revoked", by="hub")
        with self._mu:
            for k in [k for k, s in self._pending.items() if s.abandoned and now - s.t_open > 120.0]:
                del self._pending[k]
            for k in [k for k, q in self._opens.items() if not q or now - q[-1] >= WINDOW_S]:
                del self._opens[k]

    def start(self, interval: float = 60.0) -> StreamRouter:
        if self._sweeper is None:

            def run() -> None:
                while not self._stop.wait(interval):
                    try:
                        self.sweep()
                    except Exception as e:  # noqa: BLE001
                        _log.warning("stream_sweep_error type=%s", type(e).__name__)

            self._sweeper = threading.Thread(target=run, name="stream-sweep", daemon=True)
            self._sweeper.start()
        return self

    def close_all(self) -> None:
        self._stop.set()
        with self._mu:
            mine = list(self._streams.values())
        for s in mine:
            self._close(s, "closed", by="hub", tell_host=False)


__all__ = ["HOST_CODES", "ClientSink", "Limits", "Refused", "StreamRouter"]

"""The host's gate on a remote access stream (R2, #1590; hub contract 10.2c, vectors ``gate_*`` and ``frame_cost_*``).

Three things live here and nothing else touches them:

- :func:`open` judges one h2w ``stream_open`` in the contract's order (replay, enroll, off, device, limits) from the
  workspace **model** (the policy and the device list are never read from a file, a cache or the hub), under the
  workspace store lock. Until R3 builds the session (Noise, presence, the in-process terminator) a passing open is
  answered ``remote.version`` and the stream is never registered (:data:`STREAMS_ACCEPT` is the one switch R3 flips).
- :class:`StreamTable` holds the open streams of the workspace and the revokers R3 registers on them. :func:`on_commit`
  runs from the store's commit hook, so it is called **inside the store lock**: a stream is closed in the same hold
  that appended the line that revokes it, and a gate that registers a stream also holds that lock, so no stream can
  open between a ``remote.disabled`` and its close.
- :class:`Budget` is the byte budget of one stream in one direction (1 GiB over its life, 64 MiB per rolling minute;
  a frame costs :func:`frame_cost`). R3 charges every frame; over the budget the stream is closed ``remote.busy``.

Nothing here logs a body, a key or a secret."""

from __future__ import annotations

import collections
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from orch.canon.jcs import dumps as cj
from orch.hubclient.tlslink.client import stream_body_ok
from orch.model import remote as model_remote

__all__ = [
    "BYTE_BUDGET", "CODES", "FRAME_MIN_COST", "LIMITS", "STREAMS_ACCEPT", "Budget", "StreamTable", "decide",
    "frame_cost", "on_commit", "open", "table",
]  # fmt: skip

_log = logging.getLogger("orch.host.remote.gate")

#: contract 10.2c / section 16 rows 50-58 (``LIMITS``, ``BYTE_BUDGET``, ``FRAME_MIN_COST`` of the reference)
LIMITS = {"streams_per_device": 4, "streams_per_workspace": 16, "handshakes_per_min": 10, "enrolls_per_min": 3}
BYTE_BUDGET = {"life": 1 << 30, "per_min": 64 << 20}
FRAME_MIN_COST = 1024
MINUTE_MS = 60_000
SEEN_TTL_MS = 12 * 3600 * 1000
SEEN_MAX = 4096
WORKSPACE = "workspace"

#: R3 flips this when the host can answer a remote API range. Until then every open that passes the gate is answered
#: ``remote.version`` and nothing is registered, consumed or shown to the dashboard.
STREAMS_ACCEPT = False

#: the outstanding enrollment codes of a workspace as ``{code_id: expires_ms}``; ``remote.enrollment`` (T2a) installs
#: the real source (memory only). Without one no enroll stream passes (``remote.no_code``).
CODES: Callable[[Any], Mapping[str, int]] = lambda service: {}  # noqa: E731, ARG005

#: events of the workspace log that close streams: all of them, or only one device's (a close reason for each)
_ALL = {"remote.disabled": "remote.off", "remote.changed": "revoked", "remote.key_rotated": "revoked"}


def frame_cost(signed: Any) -> int:
    """What one stream frame costs against its stream's byte budget: its whole signed form, at least 1 KiB."""
    return max(FRAME_MIN_COST, len(cj(signed)))


class Budget:
    """The byte budget of one stream in one direction. Host config may lower the limits, never raise them."""

    def __init__(self, *, life: int | None = None, per_min: int | None = None) -> None:
        self.life = BYTE_BUDGET["life"] if life is None else min(life, BYTE_BUDGET["life"])
        self.per_min = BYTE_BUDGET["per_min"] if per_min is None else min(per_min, BYTE_BUDGET["per_min"])
        self.spent = 0
        self._window: collections.deque[tuple[int, int]] = collections.deque()
        self._in_window = 0
        self.over = False

    def charge(self, cost: int, now_ms: int) -> bool:
        """Charge one frame. ``False``: the frame is over the budget and the stream must be closed ``remote.busy``
        (and stays over: nothing more is charged). A refused frame is not counted."""
        if self.over or cost < 0:
            return False
        while self._window and self._window[0][0] <= now_ms - MINUTE_MS:
            self._in_window -= self._window.popleft()[1]
        if self.spent + cost > self.life or self._in_window + cost > self.per_min:
            self.over = True
            return False
        self.spent += cost
        self._in_window += cost
        self._window.append((now_ms, cost))
        return True

    def charge_frame(self, signed: Any, now_ms: int) -> bool:
        return self.charge(frame_cost(signed), now_ms)


class _Stream:
    __slots__ = ("device_id", "kind", "revoke", "budget_in", "budget_out", "seq_in")

    def __init__(self, device_id: str, kind: str, revoke: Callable[[str], None] | None) -> None:
        self.device_id, self.kind, self.revoke = device_id, kind, revoke
        self.budget_in, self.budget_out = Budget(), Budget()
        self.seq_in = 0


class StreamTable:
    """The open streams of one workspace, the counters the gate limits by, and the stream ids seen in the last 12 h.

    Closing a stream calls its revoker (R3: ends the session, wipes its keys, closes the terminator) with the reason
    and queues a ``stream_close`` frame for the pump (:meth:`take_closing`); the frame is sent after the lock is
    released. Revokers run in whatever thread closes the stream, normally inside the store lock (:func:`on_commit`)."""

    def __init__(self) -> None:
        self._mu = threading.RLock()
        self._streams: dict[str, _Stream] = {}
        self._seen: collections.OrderedDict[str, int] = collections.OrderedDict()
        self._handshakes: dict[str, collections.deque[int]] = {}
        self._enrolls: collections.deque[int] = collections.deque()
        self._closing: list[tuple[str, str]] = []

    # --- registry ---------------------------------------------------------------------------------------------------

    def register(
        self, stream_id: str, device_id: str, revoke: Callable[[str], None] | None = None, kind: str = "session"
    ) -> None:
        with self._mu:
            self._streams[stream_id] = _Stream(device_id, kind, revoke)

    def attach(self, stream_id: str, revoke: Callable[[str], None]) -> bool:
        """R3 registers (or replaces) the revoker of an open stream. ``False``: it is already closed."""
        with self._mu:
            s = self._streams.get(stream_id)
            if s is None:
                return False
            s.revoke = revoke
            return True

    def unregister(self, stream_id: str) -> None:
        """The stream ended by itself (peer close, idle): forget it without a revoker call or a close frame."""
        with self._mu:
            self._streams.pop(stream_id, None)

    def is_open(self, stream_id: str) -> bool:
        with self._mu:
            return stream_id in self._streams

    def count_device(self, device_id: str) -> int:
        with self._mu:
            return sum(1 for s in self._streams.values() if s.device_id == device_id and s.kind == "session")

    def count_workspace(self) -> int:
        with self._mu:
            return len(self._streams)  # enroll streams count too: they hold a slot

    # --- rate counters and replay -----------------------------------------------------------------------------------

    def handshakes(self, device_id: str, now_ms: int) -> int:
        with self._mu:
            return _recent(self._handshakes.get(device_id), now_ms)

    def note_handshake(self, device_id: str, now_ms: int) -> None:
        with self._mu:
            self._handshakes.setdefault(device_id, collections.deque()).append(now_ms)
            if len(self._handshakes) > 4 * LIMITS["streams_per_workspace"]:  # bounded: drop the idle ones
                for k in [k for k, q in self._handshakes.items() if _recent(q, now_ms) == 0]:
                    del self._handshakes[k]

    def enrolls(self, now_ms: int) -> int:
        with self._mu:
            return _recent(self._enrolls, now_ms)

    def note_enroll(self, now_ms: int) -> None:
        with self._mu:
            self._enrolls.append(now_ms)

    def seen(self, stream_id: str, now_ms: int) -> bool:
        with self._mu:
            self._expire_seen(now_ms)
            return stream_id in self._seen

    def remember(self, stream_id: str, now_ms: int) -> bool:
        """Record a stream id as used. ``False``: the set of live ids is full (4096): the open is ``busy``."""
        with self._mu:
            self._expire_seen(now_ms)
            if len(self._seen) >= SEEN_MAX:
                return False
            self._seen[stream_id] = now_ms
            return True

    def _expire_seen(self, now_ms: int) -> None:
        while self._seen:
            sid, t = next(iter(self._seen.items()))
            if now_ms - t < SEEN_TTL_MS:
                break
            del self._seen[sid]

    # --- closing ----------------------------------------------------------------------------------------------------

    def close(self, stream_id: str, reason: str) -> bool:
        with self._mu:
            return self._close_locked([stream_id], reason) > 0

    def close_device(self, device_id: str, reason: str = "revoked") -> int:
        with self._mu:
            return self._close_locked([k for k, s in self._streams.items() if s.device_id == device_id], reason)

    def close_all(self, reason: str = "revoked") -> int:
        with self._mu:
            return self._close_locked(list(self._streams), reason)

    def _close_locked(self, ids: list[str], reason: str) -> int:
        n = 0
        for sid in ids:
            s = self._streams.pop(sid, None)
            if s is None:
                continue
            n += 1
            self._closing.append((sid, reason))
            if s.revoke is not None:
                try:
                    s.revoke(reason)
                except Exception:  # noqa: BLE001 - one broken revoker must not keep the others open
                    _log.warning("remote.revoker_failed")
        return n

    def take_closing(self) -> list[tuple[str, str]]:
        """The ``(stream_id, reason)`` close frames the pump still sends (call after the store lock is released)."""
        with self._mu:
            out, self._closing = self._closing, []
            return out

    # --- frames of an open stream -----------------------------------------------------------------------------------

    def inbound(self, body: Mapping[str, Any], now_ms: int, *, cost: int | None = None) -> str | None:
        """One h2w ``stream_data`` of an open stream: its ``seq`` must be the previous + 1 and it is charged against
        the stream's inbound budget. A frame for a stream the table does not hold is dropped silently (``None``, the
        stream is closed or never opened). A bad ``seq`` closes the stream ``malformed``, an over-budget frame
        ``remote.busy``; the return value is that reason (the close frame is queued)."""
        with self._mu:
            s = self._streams.get(body["stream_id"])
            if s is None:
                return None
            if body["seq"] != s.seq_in + 1:
                reason = "malformed"
            elif not s.budget_in.charge(cost if cost is not None else FRAME_MIN_COST + len(body["bytes"]), now_ms):
                reason = "remote.busy"
            else:
                s.seq_in = body["seq"]
                return None
            self._close_locked([body["stream_id"]], reason)
            return reason

    def outbound_ok(self, stream_id: str, cost: int, now_ms: int) -> bool:
        """Charge one frame the host is about to send. ``False`` closes the stream ``remote.busy`` (queued)."""
        with self._mu:
            s = self._streams.get(stream_id)
            if s is None:
                return False
            if s.budget_out.charge(cost, now_ms):
                return True
            self._close_locked([stream_id], "remote.busy")
            return False

    def peer_closed(self, stream_id: str) -> None:
        """h2w ``stream_close``: the revoker still runs (it wipes the session) but no frame goes back."""
        with self._mu:
            s = self._streams.pop(stream_id, None)
            if s is not None and s.revoke is not None:
                try:
                    s.revoke("closed")
                except Exception:  # noqa: BLE001
                    _log.warning("remote.revoker_failed")

    def on_commit(self, log: str, event: Mapping[str, Any]) -> None:
        """The store's commit hook, inside the store lock: close what the committed line revokes."""
        if log != WORKSPACE:
            return
        t = event.get("type")
        if t in _ALL:
            self.close_all(_ALL[t])
        elif t == "hub.joined" and event.get("mode") == "full":
            self.close_all("revoked")  # D93: the model also turned remote off, whatever the premises answer
        elif t == "remote.device_removed" and type(event.get("device_id")) is str:
            self.close_device(event["device_id"], "revoked")


def _recent(q: collections.deque[int] | None, now_ms: int) -> int:
    if q is None:
        return 0
    while q and q[0] <= now_ms - MINUTE_MS:
        q.popleft()
    return len(q)


_TABLE_ATTR = "_remote_stream_table"
_TABLE_MU = threading.Lock()


def table(service: Any) -> StreamTable:
    """The workspace's one :class:`StreamTable` (made on first use, kept on the service)."""
    t = getattr(service, _TABLE_ATTR, None)
    if t is None:
        with _TABLE_MU:
            t = getattr(service, _TABLE_ATTR, None)
            if t is None:
                t = StreamTable()
                setattr(service, _TABLE_ATTR, t)
    return t


def on_commit(service: Any, log: str, event: Mapping[str, Any]) -> None:
    """``workspace._committed_hook`` calls this with the committed line, inside the store lock. Never raises."""
    try:
        table(service).on_commit(log, event)
    except Exception:  # noqa: BLE001 - a hook must not break the append
        _log.warning("remote.on_commit_failed")


# --- the gate -------------------------------------------------------------------------------------------------------


def decide(
    body: Mapping[str, Any],
    *,
    seen: bool,
    on: bool,
    codes: Mapping[str, int],
    enrolls: int,
    device: Mapping[str, Any] | None,
    streams_device: int,
    streams_workspace: int,
    handshakes_device: int,
    now_ms: int,
) -> str | None:
    """The contract's gate order over plain facts (reference ``stream_open_gate``). ``None``: it passes; otherwise
    the code of the ``stream_open`` reply. ``on`` is "remote access is on and its end date is not reached"."""
    if seen:
        return "replay"
    if body["kind"] == "enroll":
        exp = codes.get(body["device_id"])
        if type(exp) is not int or now_ms >= exp:
            return "remote.no_code"
        return "remote.busy" if enrolls >= LIMITS["enrolls_per_min"] else None
    if not on:
        return "remote.off"  # first, and about nothing else: an opted-out workspace says nothing about its devices
    if (
        device is None
        or type(device.get("expires_ms")) is not int
        or now_ms >= device["expires_ms"]
        or device.get("level_cap") not in model_remote.LEVELS
    ):
        return "remote.device"
    if (
        streams_device >= LIMITS["streams_per_device"]
        or streams_workspace >= LIMITS["streams_per_workspace"]
        or handshakes_device >= LIMITS["handshakes_per_min"]
    ):
        return "remote.busy"
    return None


def open(  # noqa: A001 - the gate's verb, as in the contract
    service: Any, body: Mapping[str, Any], now_ms: int, *, codes: Mapping[str, int] | None = None
) -> str | None:
    """Judge one h2w ``stream_open`` ``body`` at ``now_ms``. Returns ``None`` when the host takes the stream (only
    once R3 sets :data:`STREAMS_ACCEPT`) or the code of the reply: ``replay``, ``remote.off``, ``remote.device``,
    ``remote.busy``, ``remote.no_code``, ``remote.version``, or ``malformed`` for a body the link should have
    refused. Runs under the store lock, which is also the lock of :func:`on_commit`."""
    if not stream_body_ok("stream_open", dict(body)):
        return "malformed"
    t = table(service)
    store = service.store
    with store.locked():
        core = store.state._core  # noqa: SLF001 - the model's own state, read under the lock
        rem = core.remote or {}
        sid, dev, kind = body["stream_id"], body["device_id"], body["kind"]
        code = decide(
            body,
            seen=t.seen(sid, now_ms),
            on=model_remote.is_on(core, now_ms),
            codes=(CODES(service) if codes is None else codes) if kind == "enroll" else {},
            enrolls=t.enrolls(now_ms),
            device=(rem.get("devices") or {}).get(dev) if kind == "session" else None,
            streams_device=t.count_device(dev),
            streams_workspace=t.count_workspace(),
            handshakes_device=t.handshakes(dev, now_ms),
            now_ms=now_ms,
        )
        if code is not None:
            return code
        if not t.remember(sid, now_ms):
            return "remote.busy"
        if kind == "enroll":
            t.note_enroll(now_ms)
        else:
            t.note_handshake(dev, now_ms)
        if not STREAMS_ACCEPT:
            return "remote.version"  # the host offers no remote API range yet: nothing is registered or shown
        t.register(sid, dev, kind=kind)
        return None

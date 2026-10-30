"""One remote access stream, from ``stream_open`` to its close (R3, #1591; hub contract 10.2c; design note
r3-session-design.md sections 1, 2, 5, 6).

A :class:`Session` is the host's end of a stream: the Noise responder (:mod:`.noise`), the device's first message
(``open`` on a session, ``enroll`` on an enrollment stream), and then, for an open session, the app frames between the
device and the dashboard's adapter (:mod:`.adapter`). The host reads only the kind byte and the id of an app frame
(:class:`Tracker`: ids, the open exchanges, the 1 MiB request body, ``PING``/``PONG``); the adapter parses the rest.

**I1, identity comes from the handshake only.** The device sends no level, person or expiry. On every ``HEAD`` and
``WS_OPEN`` the host recomputes ``{workspace_id, device_id, person, level}`` from its model under the store lock
(:meth:`Session._facts`: remote on, the device listed for a current owner, the person the adapter's, level = the lower
of the device's cap and the policy's) and hands it to the adapter beside the frame; any failure closes the stream
(``remote.off`` / ``revoked``) and the frame is never forwarded. Nothing is cached per stream.

Threads: :func:`feed` runs on the pump thread (it must not hold a lock while it waits for the worker); the parts that
read the model or append run on the workspace worker through ``service.submit`` (:meth:`Session._on_worker`).
:meth:`Session.close` is the stream's revoker: the stream table calls it, normally inside the store lock; it only wipes
the keys, marks the session closed and queues the adapter's notice. ``remote.session_closed`` is appended later by
:func:`log_ended` (a commit hook never appends). Lock order is always stream table -> session -> adapter/registry.

Nothing here logs a key, a challenge, a signature or a frame."""

from __future__ import annotations

import collections
import hmac
import logging
import secrets
import threading
from collections.abc import Callable
from typing import Any

from orch.canon.jcs import JcsError, loads_strict
from orch.canon.jcs import dumps as cj
from orch.crypto.encoding import b64u, unb64u
from orch.hubclient.errors import HubRefused
from orch.model import remote as model_remote

from . import adapter as ad
from . import counters, devices, enrollment, gate, noise, wrk

__all__ = [
    "HOST_API", "Registry", "Session", "Tracker", "check_open_msg", "feed", "flush_out", "log_ended", "registry",
    "start", "stream_facts", "sweep",
]  # fmt: skip

_log = logging.getLogger("orch.host.remote.session")

HOST_API = (1, 1)
HANDSHAKE_MS = 60_000
CHALLENGE_MS = 60_000
LIFE_MS = 12 * 3600 * 1000
IDLE_MS = 2 * 3600 * 1000
OUT_MAX = 64  # unsent frames per stream: a device that stops reading closes the stream ``remote.busy``
BODY_MAX = 1 << 20
MAX_HTTP, MAX_WS = 8, 2
HEAD_MAX = 16384
HEAD, BODY, END, RESET, WS_OPEN, WS_TEXT, WS_BINARY, WS_CLOSE, PING, PONG = range(1, 11)
RESETS = frozenset({"cancelled", "timeout", "too_large", "remote.busy"})
_REASONS = frozenset(
    {"malformed", "handshake", "presence", "remote.version", "remote.no_code", "remote.device", "remote.off",
     "remote.busy"}
)  # fmt: skip


class _Fail(Exception):
    """The stream ends with this contract close reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _reason(code: str) -> str:
    return code if code in _REASONS else "presence"


def check_open_msg(msg: Any, host_api: tuple[int, int]) -> int:
    """The device's ``open`` message (vectors ``open_msg_*``): exact keys, an API range that overlaps the host's
    (``remote.version``), the highest common version."""
    if type(msg) is not dict or set(msg) != {"t", "api", "presence"} or msg["t"] != "open":
        raise HubRefused("malformed")
    api = msg["api"]
    if type(api) is not list or len(api) != 2 or any(type(v) is not int or not 1 <= v < 2**31 for v in api):
        raise HubRefused("malformed")
    if api[0] > api[1]:
        raise HubRefused("malformed")
    if max(api[0], host_api[0]) > min(api[1], host_api[1]):
        raise HubRefused("remote.version")
    return min(api[1], host_api[1])


def app_header(pt: bytes) -> tuple[int, int]:
    """``(kind, id)`` of one app frame, checking only what needs no JSON: size, kind, the id rule and the payload
    shape of the kinds with a fixed one. Anything else is ``malformed``."""
    if not 5 <= len(pt) <= noise.PLAIN_MAX or not 1 <= pt[0] <= 10:
        raise _Fail("malformed")
    kind, i, n = pt[0], int.from_bytes(pt[1:5], "big"), len(pt) - 5
    if (kind in (PING, PONG)) is not (i == 0):
        raise _Fail("malformed")
    if kind in (END, PING, PONG) and n:
        raise _Fail("malformed")
    if kind == BODY and not n:
        raise _Fail("malformed")
    if kind in (HEAD, WS_OPEN) and n > HEAD_MAX:
        raise _Fail("malformed")
    if kind == RESET and pt[5:].decode("latin-1") not in RESETS:
        raise _Fail("malformed")
    if kind == WS_CLOSE:
        c = int.from_bytes(pt[5:], "big") if n == 2 else 0
        if not (1000 <= c <= 1003 or 1007 <= c <= 1011 or 3000 <= c <= 4999):
            raise _Fail("malformed")
    return kind, i


def frame(kind: int, i: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + i.to_bytes(4, "big") + payload


class Tracker:
    """The exchanges of one stream as the host sees them (kind and id only): which ids are open, the 8 and 2 limits,
    the request body size, the ``PING``/``PONG`` pairing. ``step`` returns ``"ok"``, ``"drop"`` (a frame for an exchange
    that already ended) or ``"reset:<code>"`` (the host resets that one exchange); a violation raises ``_Fail``."""

    def __init__(self) -> None:
        self.top = 0
        self.live: dict[int, dict[str, Any]] = {}
        self.pinged = {"d2h": False, "h2d": False}

    def step(self, d: str, kind: int, i: int, n: int) -> str:
        back = "h2d" if d == "d2h" else "d2h"
        if kind == PING:
            if self.pinged[d]:
                raise _Fail("malformed")
            self.pinged[d] = True
            return "ok"
        if kind == PONG:
            if not self.pinged[back]:
                raise _Fail("malformed")
            self.pinged[back] = False
            return "ok"
        x = self.live.get(i)
        if x is None:
            if d == "d2h" and kind in (HEAD, WS_OPEN):
                if i <= self.top:
                    raise _Fail("malformed")  # a reused or lower id
                self.top = i
                ws = kind == WS_OPEN
                if sum(e["ws"] == ws for e in self.live.values()) >= (MAX_WS if ws else MAX_HTTP):
                    return "reset:remote.busy"
                self.live[i] = {"ws": ws, "up": False, "down": False, "shut": set(), "head": False, "n": 0}
                return "ok"
            if i > self.top or d == "d2h" and kind in (HEAD, WS_OPEN):
                raise _Fail("malformed")  # a frame that opens nothing for an id above every opened one
            return "drop"
        if kind == RESET:
            del self.live[i]
            return "ok"
        if kind in (HEAD, WS_OPEN):
            if d == "d2h" or kind == WS_OPEN or x["head"]:
                raise _Fail("malformed")  # a second request head, a host WS_OPEN, a second response head
            x["head"] = True
        elif kind in (BODY, END):
            if d == "d2h":
                if x["ws"] or x["up"]:
                    raise _Fail("malformed")
                if kind == END:
                    x["up"] = True
                else:
                    x["n"] += n
                    if x["n"] > BODY_MAX:
                        del self.live[i]
                        return "reset:too_large"
            else:
                if not x["head"] or x["down"]:
                    raise _Fail("malformed")
                if kind == END:
                    x["down"] = True
        else:  # WS_TEXT, WS_BINARY, WS_CLOSE
            if not x["ws"] or d in x["shut"] or (d == "h2d" and not x["head"]):
                raise _Fail("malformed")
            if kind == WS_CLOSE:
                x["shut"].add(d)
        if (not x["ws"] and x["up"] and x["down"]) or (x["ws"] and (x["down"] or len(x["shut"]) == 2)):
            del self.live[i]
        return "ok"


class Registry:
    """The sessions of one workspace (by stream id) and the closed ones whose ``session_closed`` line is still owed."""

    def __init__(self) -> None:
        self.mu = threading.Lock()
        self.by_id: dict[str, Session] = {}
        self.ended: list[Session] = []
        self.rand: Callable[[int], bytes] = secrets.token_bytes  # tests set it: ephemeral key, then challenge

    def get(self, sid: str) -> Session | None:
        with self.mu:
            return self.by_id.get(sid)

    def all(self) -> list[Session]:
        with self.mu:
            return list(self.by_id.values())


_ATTR = "_remote_sessions"
_MU = threading.Lock()


def registry(service: Any) -> Registry:
    r = getattr(service, _ATTR, None)
    if r is None:
        with _MU:
            r = getattr(service, _ATTR, None)
            if r is None:
                r = Registry()
                setattr(service, _ATTR, r)
    return r  # type: ignore[no-any-return]


class Session:
    def __init__(
        self, service: Any, reg: Registry, stream_id: str, ident: str, kind: str, hub_id: str, now_ms: int
    ) -> None:
        self.service, self.reg, self.sid, self.ident, self.kind, self.hub_id = (
            service,
            reg,
            stream_id,
            ident,
            kind,
            hub_id,
        )
        self.mu = threading.Lock()
        self.state = "msg1"  # msg1 -> open | enroll -> live | pending -> closed
        self.started_ms = self.last_in_ms = now_ms
        self.tp: noise.Transport | None = None
        self.rs = b""
        self.challenge: bytes | None = None
        self.challenge_ms = 0
        self.out: collections.deque[bytes] = collections.deque()
        self.out_seq = 0
        self.tracker = Tracker()
        self.adapter: ad.Adapter | None = None
        self.person: str | None = None
        self.level: str | None = None
        self.name: str | None = None
        self.opened_ms: int | None = None
        self.reason: str | None = None
        self.logged = False

    # --- plumbing ---------------------------------------------------------------------------------------------------

    def _on_worker(self, fn: Callable[[], Any]) -> Any:
        return self.service.submit(fn).result(timeout=120)

    def _now(self) -> int:
        return int(self.service.clock() * 1000)

    def _fail(self, reason: str) -> None:
        gate.table(self.service).close(self.sid, reason)  # runs close() as the revoker, queues the close frame
        self.close(reason)  # also when the table no longer holds it

    def close(self, reason: str) -> None:
        """The revoker. Idempotent; takes no store lock and appends nothing."""
        with self.mu:
            if self.state == "closed":
                return
            was_live = self.state == "live"
            self.state, self.reason = "closed", reason
            if self.tp is not None:
                self.tp.wipe()
            self.challenge = None
            adapter = self.adapter
        if self.kind == "enroll":
            enrollment.drop(self.service, self.sid)
        if was_live and adapter is not None:
            adapter.push({"t": "closed", "stream_id": self.sid, "reason": reason})
        with self.reg.mu:
            self.reg.ended.append(self)

    def _emit(self, data: bytes, *, sealed: bool = True) -> bool:
        """Queue one frame for the device: ``data`` is a plaintext to seal, or (``sealed=False``) message 2."""
        cost = gate.FRAME_MIN_COST + ((len(data) + (16 if sealed else 0)) * 4 + 2) // 3
        if not gate.table(self.service).outbound_ok(self.sid, cost, self._now()):
            return False  # over the byte budget: the table closed the stream remote.busy
        with self.mu:
            if self.state == "closed":
                return False
            self.out.append(self.tp.seal(data) if sealed and self.tp is not None else data)
            full = len(self.out) > OUT_MAX
        if full:
            self._fail("remote.busy")
            return False
        return True

    def drain(self) -> list[tuple[int, bytes]]:
        with self.mu:
            out = []
            while self.out:
                self.out_seq += 1
                out.append((self.out_seq, self.out.popleft()))
            return out

    # --- the facts of the model, recomputed ---------------------------------------------------------------------------

    def _facts(self, now_ms: int, *, opening: bool = False) -> dict[str, Any]:
        """Under the store lock (worker thread). Raises ``_Fail`` with the reason the stream must end with."""
        store = self.service.store
        with store.locked():
            core = store.state.workspace
            rem = core.remote or {}
            if not model_remote.is_on(core, now_ms):
                raise _Fail("remote.off")
            entry = devices.lookup(rem, self.ident, now_ms, devices.owner_ids(core.members))
            if entry is None:
                raise _Fail("remote.device" if opening else "revoked")
            adapter = ad.current(self.service)
            if adapter is None or (not opening and adapter is not self.adapter):
                raise _Fail("remote.version" if opening else "closed")
            if entry["person"] != adapter.person:
                raise _Fail("remote.device")  # a device of owner B never signs as owner A
            cap = entry["level_cap"]
            policy_level = rem.get("level")
            lv = model_remote.LEVELS
            level = lv[min(lv.index(cap), lv.index(policy_level) if policy_level in lv else 0)]
            return {
                "entry": entry, "adapter": adapter,
                "identity": {
                    "workspace_id": self.service.workspace_id, "device_id": self.ident, "person": entry["person"],
                    "level": level,
                },
            }  # fmt: skip

    # --- the device's frames (pump thread) ---------------------------------------------------------------------------

    def feed(self, raw: bytes, now_ms: int) -> None:
        with self.mu:
            state = self.state
        try:
            if state == "msg1":
                self._msg1(raw, now_ms)
            elif state == "open":
                self._open(raw, now_ms)
            elif state == "live":
                self._app(raw, now_ms)
            elif state == "enroll":
                self._enroll(raw, now_ms)
            elif state != "closed":
                raise _Fail("malformed")  # a pending enrollment accepts nothing more
        except _Fail as f:
            self._fail(f.reason)
        except HubRefused as exc:
            self._fail(_reason(exc.code))
        except Exception:  # noqa: BLE001 - a bug in one stream must not stop the pump; the stream ends
            _log.warning("remote.session_error")
            self._fail("malformed")

    def _msg1(self, raw: bytes, now_ms: int) -> None:
        scalar = wrk.load(self.service)
        if scalar is None:
            raise _Fail("handshake")
        hs = noise.read_msg1(
            noise.prologue(self.kind, self.service.workspace_id, self.ident, self.sid, self.hub_id), scalar, raw
        )
        if self.kind == "session":
            self._pin(hs.rs, now_ms)
        e, challenge = self.reg.rand(32), self.reg.rand(32)
        msg2, tp = noise.write_msg2(hs, e, challenge)
        with self.mu:
            if self.state != "msg1":
                return
            self.tp, self.rs, self.challenge, self.challenge_ms = tp, hs.rs, challenge, now_ms
            self.state = "open" if self.kind == "session" else "enroll"
        self._emit(msg2, sealed=False)

    def _pin(self, static: bytes, now_ms: int) -> None:
        """A session: the device's static key must be the entry's ``transport_pub`` (a pin, not an authenticator)."""

        def look() -> bytes:
            store = self.service.store
            with store.locked():
                core = store.state.workspace
                entry = devices.lookup(core.remote or {}, self.ident, now_ms, devices.owner_ids(core.members))
                if entry is None:
                    raise _Fail("remote.device")
                return unb64u(entry["transport_pub"], 32)

        if not hmac.compare_digest(self._on_worker(look), static):
            raise _Fail("handshake")

    def _first(self, raw: bytes, now_ms: int) -> tuple[Any, bytes, bytes]:
        """The device's first transport message: opened, the challenge consumed (single use, 60 s)."""
        with self.mu:
            tp, ch, ch_ms, self.challenge = self.tp, self.challenge, self.challenge_ms, None
            if tp is None:
                raise _Fail("malformed")
            pt = tp.open(raw)
        if ch is None or now_ms - ch_ms > CHALLENGE_MS:
            raise _Fail("handshake")
        try:
            return loads_strict(pt), ch, tp.hh
        except JcsError:
            raise _Fail("malformed") from None

    def _open(self, raw: bytes, now_ms: int) -> None:
        msg, challenge, hh = self._first(raw, now_ms)
        api = check_open_msg(msg, HOST_API)
        self._on_worker(lambda: self._admit(msg, challenge, hh, api, now_ms))
        self.last_in_ms = now_ms
        self._emit(cj({"t": "opened", "api": api}))

    def _admit(self, msg: dict[str, Any], challenge: bytes, hh: bytes, api: int, now_ms: int) -> dict[str, Any]:
        """Worker thread, one store hold: the facts, the presence signature over the open bytes with the counter CAS
        shared with the presence batches, the ``remote.session_opened`` line, and the state change."""
        from . import append_host

        store = self.service.store
        with store.locked():
            facts = self._facts(now_ms, opening=True)
            entry, ident = facts["entry"], facts["identity"]
            with counters.lock():
                try:
                    count = enrollment.assertion_ok(
                        entry["presence_pub"], entry["presence_kind"],
                        enrollment.open_bytes(self.service.workspace_id, self.ident, hh, challenge),
                        msg["presence"], counters.held(self.service, self.ident),
                    )  # fmt: skip
                    if entry["presence_kind"] == "fido2":
                        counters.cas(self.service, self.ident, count)
                except HubRefused:
                    raise _Fail("presence") from None
            with self.mu:
                if self.state != "open":
                    raise _Fail("malformed")
                self.state, self.adapter = "live", facts["adapter"]
                self.person, self.level, self.name, self.opened_ms = (
                    ident["person"],
                    ident["level"],
                    entry["name"],
                    now_ms,
                )
                facts["adapter"].push({"t": "open", "stream_id": self.sid, "api": api, **ident})
            append_host(
                self.service, "remote.session_opened", stream_id=self.sid, device_id=self.ident,
                person=ident["person"], level=ident["level"],
            )  # fmt: skip
        return facts

    def _app(self, raw: bytes, now_ms: int) -> None:
        with self.mu:
            tp = self.tp
            if tp is None:
                return
            pt = tp.open(raw)
        kind, i = app_header(pt)
        with self.mu:
            verdict = self.tracker.step("d2h", kind, i, len(pt) - 5)
        if kind == PING:
            with self.mu:
                self.tracker.step("h2d", PONG, 0, 0)  # the host answers it itself: the pair is complete
            self._emit(frame(PONG, 0))
            return
        if kind == PONG:
            return
        if verdict == "drop":
            return  # an exchange that ended: the peer sent it before it saw the RESET; it does not keep a session alive
        self.last_in_ms = now_ms
        if verdict.startswith("reset:"):
            code = verdict[6:]
            self._emit(frame(RESET, i, code.encode()))
            if code == "too_large":  # the adapter has the head: it must stop the exchange too
                self._forward({"t": "frame", "stream_id": self.sid, "bytes": b64u(frame(RESET, i, b"too_large"))})
            return
        msg: dict[str, Any] = {"t": "frame", "stream_id": self.sid, "bytes": b64u(pt)}
        if kind in (HEAD, WS_OPEN):
            msg["identity"] = self._on_worker(lambda: self._recheck(now_ms))
        self._forward(msg)

    def _recheck(self, now_ms: int) -> dict[str, Any]:
        facts = self._facts(now_ms)
        return dict(facts["identity"])

    def _forward(self, msg: dict[str, Any]) -> None:
        adapter = self.adapter
        if adapter is None or not adapter.push(msg):
            raise _Fail("remote.busy" if adapter is not None and not adapter.ended else "closed")

    def _enroll(self, raw: bytes, now_ms: int) -> None:
        msg, challenge, hh = self._first(raw, now_ms)
        out = enrollment.verify(self.service, self.ident, self.sid, hh, self.rs, msg, challenge)
        with self.mu:
            if self.state != "enroll":
                return
            self.state = "pending"
        self._emit(cj({"t": "pending", "fingerprint": out["fp"]}))
        threading.Thread(
            target=self._wait_added, args=(out["pending"], out["expires_ms"]), name="remote-enroll-wait", daemon=True
        ).start()

    def _wait_added(self, pid: str, expires_ms: int) -> None:
        """Off the pump: wait for the owner's confirmation at the workspace, tell the device, close ``closed``."""
        try:
            did = enrollment.wait_added(pid, max(0.0, (expires_ms - self._now()) / 1000))
            if did is not None:
                self._emit(cj({"t": "added", "device_id": did}))
        except Exception:  # noqa: BLE001
            _log.warning("remote.enroll_wait_error")
        self._fail("closed")

    # --- the adapter's frames ---------------------------------------------------------------------------------------

    def send_app(self, pt: bytes) -> bool:
        """One app frame from the adapter to the device. ``False``: dropped (an exchange that already ended, or the
        stream is closed). A frame that breaks the rules closes the stream ``malformed``."""
        try:
            with self.mu:
                if self.state != "live":
                    return False
            kind, i = app_header(pt)
            with self.mu:
                verdict = self.tracker.step("h2d", kind, i, len(pt) - 5)
            if verdict == "drop":
                return False
            return self._emit(pt)
        except _Fail as f:
            self._fail(f.reason)
            return False


def start(service: Any, stream_id: str, ident: str, kind: str, hub_id: str | None, now_ms: int) -> bool:
    """Called by the gate inside the store lock for an open it takes: makes the session and registers it as the
    stream's revoker. ``False``: no session could be made (the link gave no hub id)."""
    if hub_id is None:
        return False
    reg = registry(service)
    s = Session(service, reg, stream_id, ident, kind, hub_id, now_ms)
    with reg.mu:
        reg.by_id[stream_id] = s
    return gate.table(service).attach(stream_id, s.close)


def feed(service: Any, stream_id: str, raw: bytes, now_ms: int) -> None:
    """One ``stream_data`` of an open stream that passed the seq and budget check (pump thread)."""
    s = registry(service).get(stream_id)
    if s is not None:
        s.feed(raw, now_ms)


def sweep(service: Any, now_ms: int) -> None:
    """Handshake 60 s, life 12 h, idle 2 h; and the end of the dashboard's lease ends the sessions that need it."""
    reg = registry(service)
    t = gate.table(service)
    has_adapter = ad.attached(service)
    for s in reg.all():
        with s.mu:
            state, started, last = s.state, s.started_ms, s.last_in_ms
        if state == "closed":
            continue
        if state in ("msg1", "open", "enroll") and now_ms - started > HANDSHAKE_MS:
            t.close(s.sid, "handshake")
        elif now_ms - started > LIFE_MS:
            t.close(s.sid, "expired")
        elif state == "live" and now_ms - last > IDLE_MS:
            t.close(s.sid, "idle")
        elif not has_adapter and state != "pending":
            t.close(s.sid, "closed")


def flush_out(service: Any, link: Any) -> int:
    """Send the frames the sessions queued for their devices, in order; forget closed sessions with nothing owed."""
    from orch.hubclient.tlslink.client import LinkError

    reg = registry(service)
    n = 0
    for s in reg.all():
        for seq, data in s.drain():
            try:
                link.send_stream("stream_data", {"stream_id": s.sid, "seq": seq, "bytes": b64u(data)})
                n += 1
            except LinkError:
                _log.warning("remote.data_frame_refused")
        with s.mu:
            gone = s.state == "closed" and not s.out
        if gone:
            with reg.mu:
                if s in reg.ended:
                    continue  # its closing line is still owed: log_ended forgets it
                reg.by_id.pop(s.sid, None)
    return n


def log_ended(service: Any) -> int:
    """After the store lock is released: append ``remote.session_closed`` for each opened session that ended."""
    from . import append_host

    reg = registry(service)
    with reg.mu:
        done, reg.ended = reg.ended, []
    n = 0
    for s in done:
        with reg.mu:
            if s.state == "closed" and not s.out:
                reg.by_id.pop(s.sid, None)
        if s.opened_ms is None or s.logged:
            continue
        s.logged = True
        service.submit(
            lambda s=s: append_host(
                service, "remote.session_closed", stream_id=s.sid, device_id=s.ident, reason=s.reason or "closed"
            )
        ).result(timeout=60)
        n += 1
    return n


def stream_facts(service: Any, stream_id: str, now_ms: int) -> dict[str, Any]:
    """What the signer is told about a stream (``remote.stream``; worker thread): computed from the model now, or
    ``None`` for a stream that is closed, unknown or no longer valid."""
    s = registry(service).get(stream_id)
    if s is None:
        return {}
    with s.mu:
        if s.state != "live" or s.tp is None:
            return {}
        hh = s.tp.hh
    try:
        facts = s._facts(now_ms)  # noqa: SLF001
    except _Fail:
        return {}
    e = facts["entry"]
    return {
        **facts["identity"], "hh": hh.hex(), "presence_kind": e["presence_kind"], "presence_pub": e["presence_pub"],
    }  # fmt: skip

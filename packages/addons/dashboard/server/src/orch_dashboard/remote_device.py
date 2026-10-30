"""The in-process stream adapter of remote access (R3, orch-core #1591; spec D89, design note r3-session-design.md 0-3,
contract hub/protocol.md 10.2c "Inside a session").

A remote device's HTTP and WebSocket travel inside a Noise stream that the HOST terminates. The host hands this process
the decrypted app frames over a host connection this process opened itself (`remote.attach`, cap `remote/1`), and takes
the answers back the same way. Nothing here is a socket to a listener: the adapter builds the ASGI scope itself and
awaits the app object in-process. No HTTP client is imported here (a test checks the imports).

Two halves:

* :class:`Terminator` (event loop, no threads): app frames <-> ASGI calls. One :class:`Stream` per host stream, one
  :class:`Exchange` per request or WebSocket. A request body is complete only at its END: the app is not called before
  (`stream_closed_before_request_end_never_dispatches_body`). Limits: HEAD <= 16 KiB, <= 8 open HTTP and <= 2 WebSocket
  exchanges (`remote.busy` RESET past them), a request without its END in 60 s is RESET `timeout`, a body over 1 MiB
  RESET `too_large`, <= 64 unsent frames per stream (a device that stops reading closes the stream `remote.busy`).
  A frame error is a stream error (`malformed`). Outside bearers (`authorization`, `cookie`, `x-setup-token`,
  `x-orch-*`) are answered 400 `remote.bearer`; `set-cookie` is never sent to the device.
* :class:`Attachment` (one thread per workspace): the `remote.attach` long poll on a host connection of its own, handing
  frames to the loop and taking the loop's answers.

**Identity (I1).** The scope's `RemoteIdentity` is built from the host's frame (`identity` on each HEAD / WS_OPEN) and
nothing else; the device's headers cannot name a person or a level. `REMOTE_STREAM` is set for the whole ASGI call of an
exchange, and the stream's one credential (`Auth.open_stream`) goes into `scope["state"]`, never into an answer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from orch.canon.jcs import JcsError, loads_strict
from orch.canon.jcs import dumps as cj
from orch.crypto.encoding import EncodingError, b64u, unb64u

from .errors import HostError, response
from .policy import REMOTE_DEVICE, STREAM_HOST, clean_path
from .remote_identity import REMOTE_STREAM, RemoteIdentity

log = logging.getLogger("orch_dashboard.remote_device")

HEAD, BODY, END, RESET, WS_OPEN, WS_TEXT, WS_BINARY, WS_CLOSE, PING, PONG = range(1, 11)
PAYLOAD_MAX = 65535 - 16 - 5  # one Noise transport plaintext minus the AEAD tag and the 5-byte header
HEAD_MAX = 16384
HEADERS_MAX = 64
REQUEST_BODY_MAX = 1 << 20
MAX_HTTP, MAX_WS = 8, 2
OUT_MAX = 64  # unsent frames per stream
REQUEST_END_S = 60.0
BUSY_S = 30.0  # a device that does not take frames for this long closes the stream `remote.busy`
WS_GRACE_S = 10.0  # after our WS_CLOSE the device answers with its own; else the exchange is reset
INBOX_MAX = 256
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")

FRAMING = frozenset(
    {
        "content-length", "transfer-encoding", "connection", "upgrade", "keep-alive", "te", "trailer",
        "proxy-connection", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
        "sec-websocket-accept",
    }
)  # fmt: skip
BEARER_NAMES = frozenset({"authorization", "cookie", "x-setup-token"})
BEARER_PREFIX = "x-orch-"
_TCHAR = frozenset("abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~")
_QUERY = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@%/?")


class Malformed(Exception):
    """A frame that breaks the contract: the stream is closed `malformed`."""


class Gone(Exception):
    """The stream is over: the ASGI call of an exchange that tries to send is ended."""


def frame(kind: int, ident: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + ident.to_bytes(4, "big") + payload


# -- heads -------------------------------------------------------------------------------------------------------------


def _value_ok(value: str) -> bool:
    return len(value) <= 4096 and all(0x20 <= ord(c) <= 0x7E for c in value) and value == value.strip(" ")


def _headers_ok(headers: Any, refused: frozenset[str]) -> bool:
    if type(headers) is not list or len(headers) > HEADERS_MAX:
        return False
    for pair in headers:
        if type(pair) is not list or len(pair) != 2 or not all(type(x) is str for x in pair):
            return False
        name, value = pair
        if not 1 <= len(name) <= 64 or not set(name) <= _TCHAR or not _value_ok(value):
            return False
        if name in FRAMING or name in refused:
            return False
    return True


def parse_request_head(payload: bytes, *, ws: bool) -> dict[str, Any]:
    """A request head or `WS_OPEN`: exact keys, canonical form, a plain path, a closed query alphabet, headers as the
    contract lists (no `host`, `origin` or framing names). Anything else is `Malformed`."""
    if len(payload) > HEAD_MAX:
        raise Malformed("head")
    try:
        head = loads_strict(payload)
    except (JcsError, ValueError):
        raise Malformed("head") from None
    keys = {"headers", "path", "query"} if ws else {"headers", "method", "path", "query"}
    if type(head) is not dict or set(head) != keys or cj(head) != payload:
        raise Malformed("head")
    path, query = head["path"], head["query"]
    if (
        type(path) is not str
        or not 1 <= len(path) <= 2048
        or clean_path(path) is None
        or any(not 0x21 <= ord(c) <= 0x7E for c in path)
        or type(query) is not str
        or len(query) > 4096
        or not set(query) <= _QUERY
        or not _headers_ok(head["headers"], frozenset({"host", "origin"}))
        or (not ws and head["method"] not in METHODS)
    ):
        raise Malformed("head")
    return head


def has_bearer(headers: list[list[str]]) -> bool:
    return any(name in BEARER_NAMES or name.startswith(BEARER_PREFIX) for name, _ in headers)


def response_headers(raw: Any) -> list[list[str]]:
    """The app's response headers as the contract's: lower case tokens, printable values, no framing names and never a
    `set-cookie` (a stream credential never leaves this process). What cannot be sent is dropped."""
    out: list[list[str]] = []
    for name, value in raw or []:
        try:
            n, v = bytes(name).decode("ascii").lower(), bytes(value).decode("latin-1").strip(" ")
        except (UnicodeDecodeError, TypeError, ValueError):
            continue
        if not 1 <= len(n) <= 64 or not set(n) <= _TCHAR or n in FRAMING or n == "set-cookie" or not _value_ok(v):
            continue
        out.append([n, v])
        if len(out) == HEADERS_MAX:
            break
    return out


def response_head(status: int, headers: Any) -> bytes:
    for cut in (HEADERS_MAX, 32, 16, 8, 0):
        body = cj({"headers": response_headers(headers)[:cut], "status": status})
        if len(body) <= HEAD_MAX:
            return body
    raise Malformed("head")


# -- the stream --------------------------------------------------------------------------------------------------------


@dataclass
class Exchange:
    id: int
    ws: bool
    head: dict[str, Any]
    identity: RemoteIdentity
    buf: bytearray = field(default_factory=bytearray)
    state: str = "collecting"  # collecting (a request waiting for its END) | running | done
    task: asyncio.Task | None = None
    timer: asyncio.TimerHandle | None = None
    inbox: asyncio.Queue | None = None  # a WebSocket: what the device sent
    refuse: tuple[int, str] | None = None  # answer this instead of calling the app (an outside bearer)
    gone: asyncio.Event = field(default_factory=asyncio.Event)  # the device or the stream ended this exchange
    device_closed: bool = False
    our_close: bool = False


@dataclass
class Stream:
    sid: str
    workspace_id: str
    person: str
    token: str
    exchanges: dict[int, Exchange] = field(default_factory=dict)
    unsent: int = 0
    room: asyncio.Event = field(default_factory=asyncio.Event)
    dead: bool = False

    def open_counts(self) -> tuple[int, int]:
        http = sum(1 for e in self.exchanges.values() if not e.ws)
        return http, len(self.exchanges) - http


class Terminator:
    """App frames <-> ASGI for one workspace's streams. Everything but :meth:`take_outbound` runs on the event loop."""

    def __init__(
        self, app, auth, loop: asyncio.AbstractEventLoop, *, busy_s: float = BUSY_S, request_s: float = REQUEST_END_S
    ):
        self.app, self.auth, self.loop = app, auth, loop
        self.busy_s, self.request_s = busy_s, request_s
        self.streams: dict[str, Stream] = {}
        self.mu = threading.Lock()  # guards `out` and `closes`, the two things the poll thread takes
        self.out: deque[tuple[str, bytes]] = deque()
        self.closes: deque[tuple[str, str]] = deque()
        self.active = 0  # open exchanges: the poll is short while any is open (a response may need to leave)
        self._tasks: set[asyncio.Task] = set()

    # -- from the host -------------------------------------------------------------------------------------------------
    def handle_many(self, messages: list[dict[str, Any]]) -> None:
        for msg in messages:
            try:
                self.handle(msg)
            except Exception:  # noqa: BLE001 - one bad message ends its stream, never the adapter
                log.warning("remote stream message not handled", exc_info=False)
                sid = msg.get("stream_id") if isinstance(msg, dict) else None
                st = self.streams.get(sid) if isinstance(sid, str) else None
                if st is not None:
                    self.fail(st, "malformed")

    def handle(self, msg: dict[str, Any]) -> None:
        kind, sid = msg.get("t"), msg.get("stream_id")
        if not isinstance(sid, str):
            return
        if kind == "open":
            self.on_open(sid, msg)
        elif kind == "closed":
            st = self.streams.get(sid)
            if st is not None:
                self.drop(st)
        elif kind == "frame":
            st = self.streams.get(sid)
            if st is None:
                return  # a frame of a stream that is gone (it closed in between)
            try:
                self.on_frame(st, msg)
            except Malformed:
                self.fail(st, "malformed")

    def on_open(self, sid: str, msg: dict[str, Any]) -> None:
        if sid in self.streams:
            return
        workspace_id, person = msg.get("workspace_id"), msg.get("person")
        if not isinstance(workspace_id, str) or not isinstance(person, str):
            self.queue_close(sid, "closed")
            return
        try:
            token = self.auth.open_stream(sid, workspace_id, person)
        except ValueError:
            self.queue_close(sid, "closed")
            return
        self.streams[sid] = Stream(sid, workspace_id, person, token)

    def identity_of(self, st: Stream, msg: dict[str, Any]) -> RemoteIdentity:
        raw = msg.get("identity")
        if type(raw) is not dict or set(raw) != {"workspace_id", "device_id", "person", "level"}:
            raise Malformed("identity")
        try:
            ident = RemoteIdentity(st.sid, raw["workspace_id"], raw["device_id"], raw["person"], raw["level"])
        except ValueError:
            raise Malformed("identity") from None
        if ident.person != st.person or ident.workspace_id != st.workspace_id:
            raise Malformed("identity")  # the host never changes a stream's person or workspace
        return ident

    def on_frame(self, st: Stream, msg: dict[str, Any]) -> None:
        try:
            pt = unb64u(msg.get("bytes"))
        except EncodingError:
            raise Malformed("bytes") from None
        if not 5 <= len(pt) <= PAYLOAD_MAX + 5:
            raise Malformed("size")
        kind, ident, payload = pt[0], int.from_bytes(pt[1:5], "big"), pt[5:]
        if kind in (PING, PONG):
            return
        ex = st.exchanges.get(ident)
        if kind in (HEAD, WS_OPEN):
            if ex is not None or ident == 0:
                raise Malformed("id")
            self.on_open_exchange(st, ident, kind == WS_OPEN, payload, msg)
        elif ex is None:
            return  # an exchange that ended: the peer sent it before it saw our RESET
        elif kind == BODY:
            if ex.ws or ex.state != "collecting":
                return
            if len(ex.buf) + len(payload) > REQUEST_BODY_MAX:
                self.reset(st, ex, "too_large")
                return
            ex.buf.extend(payload)
        elif kind == END:
            if not ex.ws and ex.state == "collecting":
                self.dispatch(st, ex)
        elif kind == RESET:
            self.end_exchange(st, ex)
        elif kind in (WS_TEXT, WS_BINARY):
            if ex.ws and ex.inbox is not None:
                self.to_inbox(
                    st,
                    ex,
                    {"type": "websocket.receive", "text": payload.decode("utf-8")}
                    if kind == WS_TEXT
                    else {"type": "websocket.receive", "bytes": payload},
                )
        elif kind == WS_CLOSE:
            if ex.ws and ex.inbox is not None:
                ex.device_closed = True
                code = int.from_bytes(payload, "big")
                self.to_inbox(st, ex, {"type": "websocket.disconnect", "code": code})
                self.maybe_finish(st, ex)
        else:
            raise Malformed("kind")

    def to_inbox(self, st: Stream, ex: Exchange, event: dict[str, Any]) -> None:
        assert ex.inbox is not None
        if ex.inbox.qsize() >= INBOX_MAX:
            self.fail(st, "remote.busy")  # the app does not keep up with the device
            return
        ex.inbox.put_nowait(event)

    def on_open_exchange(self, st: Stream, ident: int, ws: bool, payload: bytes, msg: dict[str, Any]) -> None:
        head = parse_request_head(payload, ws=ws)
        identity = self.identity_of(st, msg)
        http, socks = st.open_counts()
        if (ws and socks >= MAX_WS) or (not ws and http >= MAX_HTTP):
            self.emit_now(st, frame(RESET, ident, b"remote.busy"))
            return
        ex = Exchange(ident, ws, head, identity)
        if has_bearer(head["headers"]):
            ex.refuse = (400, "remote.bearer")
        st.exchanges[ident] = ex
        self.active += 1
        if ws:
            ex.inbox = asyncio.Queue()
            ex.inbox.put_nowait({"type": "websocket.connect"})
            ex.state = "running"
            ex.task = self.spawn(self.run_ws(st, ex))
        else:
            ex.timer = self.loop.call_later(self.request_s, self.request_timeout, st.sid, ident)

    def request_timeout(self, sid: str, ident: int) -> None:
        st = self.streams.get(sid)
        ex = st.exchanges.get(ident) if st is not None else None
        if st is not None and ex is not None and ex.state == "collecting":
            self.reset(st, ex, "timeout")

    def dispatch(self, st: Stream, ex: Exchange) -> None:
        """The request is complete (its END came): only now the app is called."""
        if ex.timer is not None:
            ex.timer.cancel()
            ex.timer = None
        ex.state = "running"
        ex.task = self.spawn(self.run_http(st, ex))

    def spawn(self, coro) -> asyncio.Task:
        task = self.loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- ending things -------------------------------------------------------------------------------------------------
    def end_exchange(self, st: Stream, ex: Exchange) -> None:
        """The exchange is over in both directions (or reset): cancel what runs, forget it."""
        if st.exchanges.pop(ex.id, None) is not None:
            self.active -= 1
        ex.state = "done"
        ex.gone.set()
        if ex.timer is not None:
            ex.timer.cancel()
        if ex.task is not None and not ex.task.done() and ex.task is not asyncio.current_task():
            ex.task.cancel()

    def reset(self, st: Stream, ex: Exchange, code: str) -> None:
        self.emit_now(st, frame(RESET, ex.id, code.encode()))
        self.end_exchange(st, ex)

    def maybe_finish(self, st: Stream, ex: Exchange) -> None:
        if ex.ws and ex.device_closed and ex.our_close:
            self.end_exchange(st, ex)

    def fail(self, st: Stream, reason: str) -> None:
        """A stream error: tell the host to close the stream, and forget it here."""
        self.queue_close(st.sid, reason)
        self.drop(st)

    def drop(self, st: Stream) -> None:
        """The stream is over: nothing runs for it, nothing it minted works (D89)."""
        if st.dead:
            return
        st.dead = True
        self.streams.pop(st.sid, None)
        for ex in list(st.exchanges.values()):
            self.end_exchange(st, ex)
        st.room.set()
        tickets = getattr(getattr(self.app, "state", None), "term_tickets", None)
        if tickets is not None:
            tickets.forget(st.token)
        task = self.loop.run_in_executor(None, self.auth.drop_stream, st.sid)
        self._tasks.add(task)  # type: ignore[arg-type]
        task.add_done_callback(self._tasks.discard)

    def reset_all(self) -> None:
        """The attachment was lost: the host closed every session of it. Nothing of them stays."""
        for st in list(self.streams.values()):
            self.drop(st)

    def queue_close(self, sid: str, reason: str) -> None:
        with self.mu:
            self.closes.append((sid, reason))

    # -- to the host ---------------------------------------------------------------------------------------------------
    def emit_now(self, st: Stream, pt: bytes) -> None:
        """A small control frame (RESET): never waits."""
        if not st.dead:
            st.unsent += 1
            with self.mu:
                self.out.append((st.sid, pt))

    async def emit(self, st: Stream, pt: bytes) -> None:
        while st.unsent >= OUT_MAX and not st.dead:
            st.room.clear()
            try:
                await asyncio.wait_for(st.room.wait(), self.busy_s)
            except TimeoutError:
                self.fail(st, "remote.busy")  # the device does not take what we send
                raise Gone from None
        if st.dead:
            raise Gone
        st.unsent += 1
        with self.mu:
            self.out.append((st.sid, pt))

    def take_outbound(
        self, max_send: int = 64, max_close: int = 16
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, int]]:
        """(thread-safe) What the poll sends next, and how many frames per stream it took (`sent`)."""
        send, close, counts = [], [], {}
        with self.mu:
            while self.out and len(send) < max_send:
                sid, pt = self.out.popleft()
                send.append({"stream_id": sid, "bytes": b64u(pt)})
                counts[sid] = counts.get(sid, 0) + 1
            while self.closes and len(close) < max_close:
                sid, reason = self.closes.popleft()
                close.append({"stream_id": sid, "reason": reason})
        return send, close, counts

    def sent(self, counts: dict[str, int]) -> None:
        for sid, n in counts.items():
            st = self.streams.get(sid)
            if st is not None:
                st.unsent = max(0, st.unsent - n)
                st.room.set()

    def pending(self) -> bool:
        with self.mu:
            return bool(self.out or self.closes)

    # -- the ASGI calls ------------------------------------------------------------------------------------------------
    def scope(self, st: Stream, ex: Exchange, *, ws: bool) -> dict[str, Any]:
        head = ex.head
        headers = [(n.encode("ascii"), v.encode("latin-1")) for n, v in head["headers"]]
        headers.append((b"host", STREAM_HOST.encode()))
        path = head["path"]
        scope: dict[str, Any] = {
            "type": "websocket" if ws else "http",
            "asgi": {"version": "3.0", "spec_version": "2.4" if ws else "2.3"},
            "scheme": "wss" if ws else "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": head["query"].encode("ascii"),
            "root_path": "",
            "headers": headers,
            "client": None,
            "server": (STREAM_HOST, 443),
            "orch_listener": REMOTE_DEVICE,
            "state": {"remote": ex.identity, "token": st.token},
        }
        if ws:
            scope["subprotocols"] = []
            scope["extensions"] = {"websocket.http.response": {}}
        else:
            scope["http_version"] = "1.1"
            scope["method"] = head["method"]
            if ex.buf:
                scope["headers"].append((b"content-length", str(len(ex.buf)).encode()))
        return scope

    async def run_http(self, st: Stream, ex: Exchange) -> None:
        REMOTE_STREAM.set(st.sid)
        started = finished = False
        delivered = False
        body = bytes(ex.buf)

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await ex.gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal started, finished
            if finished or ex.state == "done":
                return
            if message["type"] == "http.response.start":
                started = True
                await self.emit(st, frame(HEAD, ex.id, response_head(int(message["status"]), message.get("headers"))))
            elif message["type"] == "http.response.body" and started:
                chunk = bytes(message.get("body", b""))
                for i in range(0, len(chunk), PAYLOAD_MAX):
                    await self.emit(st, frame(BODY, ex.id, chunk[i : i + PAYLOAD_MAX]))
                if not message.get("more_body"):
                    finished = True
                    await self.emit(st, frame(END, ex.id))

        try:
            if ex.refuse is not None:
                answer = response(ex.refuse[1], ex.refuse[0])
                await answer(self.scope(st, ex, ws=False), receive, send)
            else:
                await self.app(self.scope(st, ex, ws=False), receive, send)
        except (asyncio.CancelledError, Gone):
            return
        except Exception:  # noqa: BLE001 - the answer is a static 500, the exception type only in the log
            log.warning("remote stream request failed")
            if not started and ex.state != "done":
                with contextlib.suppress(Gone, Malformed):
                    await self.emit(
                        st, frame(HEAD, ex.id, response_head(500, [(b"content-type", b"application/json")]))
                    )
                    await self.emit(st, frame(BODY, ex.id, b'{"ok":false,"error":{"code":"internal"}}'))
                    await self.emit(st, frame(END, ex.id))
                    finished = True
        finally:
            if ex.state != "done":
                if not finished and not st.dead:
                    self.emit_now(st, frame(RESET, ex.id, b"cancelled"))
                self.end_exchange(st, ex)

    async def run_ws(self, st: Stream, ex: Exchange) -> None:
        REMOTE_STREAM.set(st.sid)
        accepted = answered = False

        async def receive():
            assert ex.inbox is not None
            return await ex.inbox.get()

        async def send(message):
            nonlocal accepted, answered
            kind = message["type"]
            if ex.state == "done":
                return
            if kind == "websocket.accept" and not accepted and not answered:
                accepted = True
                await self.emit(st, frame(HEAD, ex.id, response_head(101, [])))
            elif kind in ("websocket.send",) and accepted and not ex.our_close:
                if message.get("text") is not None:
                    data, k = message["text"].encode("utf-8"), WS_TEXT
                else:
                    data, k = bytes(message.get("bytes") or b""), WS_BINARY
                if len(data) > PAYLOAD_MAX:
                    await self.close_ws(st, ex, 1009)
                    return
                await self.emit(st, frame(k, ex.id, data))
            elif kind == "websocket.close":
                if accepted:
                    await self.close_ws(st, ex, int(message.get("code") or 1000))
                elif not answered:
                    answered = True
                    await self.emit(st, frame(HEAD, ex.id, response_head(403, [])))
                    await self.emit(st, frame(END, ex.id))
            elif kind == "websocket.http.response.start" and not accepted and not answered:
                answered = True
                await self.emit(st, frame(HEAD, ex.id, response_head(int(message["status"]), message.get("headers"))))
            elif kind == "websocket.http.response.body" and answered:
                chunk = bytes(message.get("body", b""))
                for i in range(0, len(chunk), PAYLOAD_MAX):
                    await self.emit(st, frame(BODY, ex.id, chunk[i : i + PAYLOAD_MAX]))
                if not message.get("more_body"):
                    await self.emit(st, frame(END, ex.id))

        try:
            if ex.refuse is not None:
                answered = True
                await self.emit(
                    st, frame(HEAD, ex.id, response_head(ex.refuse[0], [(b"content-type", b"application/json")]))
                )
                await self.emit(
                    st,
                    frame(BODY, ex.id, b'{"ok":false,"error":{"code":"%s","retryable":false}}' % ex.refuse[1].encode()),
                )
                await self.emit(st, frame(END, ex.id))
            else:
                await self.app(self.scope(st, ex, ws=True), receive, send)
        except (asyncio.CancelledError, Gone):
            return
        except Exception:  # noqa: BLE001
            log.warning("remote stream socket failed")
        try:
            if ex.state != "done":
                if accepted and not ex.our_close:
                    await self.close_ws(st, ex, 1000)
                elif not accepted and not answered:
                    await self.emit(st, frame(HEAD, ex.id, response_head(403, [])))
                    await self.emit(st, frame(END, ex.id))
                if not accepted:
                    self.end_exchange(st, ex)  # a refused upgrade is an ordinary response: it ends with its END
        except Gone:
            return

    async def close_ws(self, st: Stream, ex: Exchange, code: int) -> None:
        if ex.our_close or ex.state == "done":
            return
        ex.our_close = True
        await self.emit(st, frame(WS_CLOSE, ex.id, code.to_bytes(2, "big")))
        if ex.device_closed:
            self.end_exchange(st, ex)
        else:
            self.loop.call_later(WS_GRACE_S, self.ws_grace, st.sid, ex.id)

    def ws_grace(self, sid: str, ident: int) -> None:
        st = self.streams.get(sid)
        ex = st.exchanges.get(ident) if st is not None else None
        if st is not None and ex is not None and ex.our_close and not ex.device_closed:
            self.reset(st, ex, "cancelled")


# -- the host connection -----------------------------------------------------------------------------------------------

ACTIVE_WAIT_MS = 100  # while an exchange is open the poll is short: a response (an event, a terminal's output) leaves
IDLE_WAIT_MS = 10_000
RETRY_S = {"remote.busy": 5.0, "host.unavailable": 2.0}
GIVE_UP_S = 60.0  # a refusal that will not change soon (not an owner, an old host): look again in a minute


class Attachment(threading.Thread):
    """One workspace's `remote.attach` long poll on a host connection of its own. A refusal or a lost connection ends
    the attachment (the host closes the sessions of a lapsed lease) and the manager starts another when it is time."""

    def __init__(
        self,
        ws: str,
        terminator: Terminator,
        link_factory,
        *,
        wait_ms: int = IDLE_WAIT_MS,
        active_ms: int = ACTIVE_WAIT_MS,
    ):
        super().__init__(name=f"remote-attach-{ws[:8]}", daemon=True)
        self.ws, self.terminator, self.link_factory = ws, terminator, link_factory
        self.wait_ms, self.active_ms = wait_ms, active_ms
        self.stop_event = threading.Event()
        self.retry_at = 0.0
        self.link: Any = None
        self.polls = 0

    def stop(self) -> None:
        self.stop_event.set()
        link = self.link
        if link is not None:
            with contextlib.suppress(Exception):
                link.close()

    def run(self) -> None:
        loop, term = self.terminator.loop, self.terminator
        attach_id = None
        try:
            self.link = link = self.link_factory(self.ws)
            while not self.stop_event.is_set():
                send, close, counts = term.take_outbound()
                wait = 0 if (send or close) else (self.active_ms if term.active else self.wait_ms)
                args: dict[str, Any] = {"send": send, "close": close, "wait_ms": wait}
                if attach_id is not None:
                    args["attach_id"] = attach_id
                got = link.call("remote.attach", args)
                self.polls += 1
                attach_id = got["attach_id"]
                if counts:
                    loop.call_soon_threadsafe(term.sent, counts)
                if got["frames"]:
                    loop.call_soon_threadsafe(term.handle_many, got["frames"])
        except HostError as exc:
            if not self.stop_event.is_set():
                code = exc.error.get("code", "")
                self.retry_at = time.monotonic() + RETRY_S.get(code, GIVE_UP_S if code != "invalid.input" else 1.0)
        except Exception:  # noqa: BLE001 - never a raise out of the thread
            if not self.stop_event.is_set():
                self.retry_at = time.monotonic() + 5.0
        finally:
            link = self.link
            if link is not None:
                with contextlib.suppress(Exception):
                    link.close()
            with contextlib.suppress(RuntimeError):  # the loop is closed at shutdown
                loop.call_soon_threadsafe(term.reset_all)


class RemoteDevices:
    """Starts and keeps one :class:`Attachment` per workspace of this computer (the registry's pins). Off by default
    (`create_app(remote_streams=True)` turns it on, `__main__` does)."""

    def __init__(self, app, gateway, *, link_factory=None, scan_s: float = 10.0):
        self.app, self.gateway = app, gateway
        self.link_factory = link_factory or self.default_link
        self.scan_s = scan_s
        self.terminators: dict[str, Terminator] = {}
        self.attachments: dict[str, Attachment] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def default_link(self, ws: str):
        link = self.gateway.new_link(ws)
        if not link.supports("remote.attach", "remote/1"):
            link.close()
            raise HostError(
                {"code": "op.unsupported", "message": "this host has no remote sessions", "retryable": False}, 1
            )
        return link

    def workspaces(self) -> list[str]:
        return sorted(self.gateway.people())

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.thread = threading.Thread(target=self.scan, name="remote-devices-scan", daemon=True)
        self.thread.start()

    def scan_once(self) -> None:
        assert self.loop is not None
        for ws in self.workspaces():
            current = self.attachments.get(ws)
            if current is not None and current.is_alive():
                continue
            if current is not None and time.monotonic() < current.retry_at:
                continue
            term = self.terminators.get(ws)
            if term is None:
                term = self.terminators[ws] = Terminator(self.app, self.app.state.auth, self.loop)
            att = self.attachments[ws] = Attachment(ws, term, self.link_factory)
            att.start()

    def scan(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.scan_once()
            except Exception:  # noqa: BLE001
                log.warning("remote stream scan failed")
            self.stop_event.wait(self.scan_s)

    def stop(self) -> None:
        self.stop_event.set()
        for att in list(self.attachments.values()):
            att.stop()
        for att in list(self.attachments.values()):
            att.join(timeout=2)

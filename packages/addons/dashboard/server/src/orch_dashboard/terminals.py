"""Terminals (M7a): the person's own shells in the dock (contract v1.6, sections 12.3 and 12.6).

Control goes through the host: `run.list`, `term.preview`, `term.open`, `run.stop` are typed routes with fixed ops and
closed input models, like the reads. Terminal bytes go directly between the browser's WebSocket `/term` and the runner's
term socket, through core's `TermClient` (the section 12.6.1 verification, `input_id`/`lease_gen`, one frame in flight,
throttle and `lost`).

**Attach is lazy (section 12.6.6).** `POST .../runs/<run>/attach` stores the intent and returns a browser ticket (128
random bits, kept only as a hash, single use, at most 30 s, 10 per minute per session); it calls nothing on the host.
Only when the WebSocket has presented that ticket does this server call `term.attach` on a host connection of its own,
verify the runner and present the host ticket at once.

**Hygiene.** Nothing here logs, stores, echoes or returns terminal output, input, tickets or frame contents, also not in
exceptions or validation errors: failures end the socket with a fixed reason and a close code, never a message built
from data.

**Who the terminal runs as.** In tier B (spec 13.5) terminals run as `orch-agent-<person>`, never as the person: the
host's preview names that user (`runs_as`) and `execution_line` carries the card line "This terminal runs as
orch-agent-<person>. It cannot read your keys. Never type secrets into it."
(`null` in tier A, where the page keeps its "run as you" line).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import queue
import re
import secrets
import socket
import struct
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.websockets import WebSocket, WebSocketState

from . import reads
from .adapters import runs as project
from .errors import HostError, fail, retry_after, valid_code
from .gateway import Gateway
from .hostlink import HostLink
from .policy import LOOPBACK

#: Tier B (spec 13.5): the host names the agent user the terminal runs as (`runs_as`); only this shape is shown.
AGENT_USER = re.compile(r"^orch-agent-[a-z_][a-z0-9_-]{0,31}$")
TIER_B_LINE = "This terminal runs as {user}. It cannot read your keys. Never type secrets into it."


def execution_line(runs_as: Any) -> str | None:
    """The terminal header's card line in tier B, else `None` (the page then keeps D-P2-31's "run as you" line)."""
    return TIER_B_LINE.format(user=runs_as) if isinstance(runs_as, str) and AGENT_USER.fullmatch(runs_as) else None


def preview_view(data: Any) -> dict[str, Any]:
    """The preview for the page: the adapter's view plus the tier B card line (and no `as_person` when it is set)."""
    out = project.preview(data)
    line = execution_line(data.get("runs_as")) if isinstance(data, dict) else None
    return {**out, "execution_line": line, "as_person": out["as_person"] and line is None}


RunId = Annotated[str, StringConstraints(pattern=r"^r_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
Idem = Annotated[str, StringConstraints(pattern=r"^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
Model = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:\[\]/-]{0,63}$")]
Repo = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Label = Annotated[str, StringConstraints(min_length=1, max_length=40)]

TICKET_TTL = 30.0  # the browser ticket (the dashboard server's choice, at most 30 s)
TICKET_RATE = 10  # browser tickets per minute per bearer session
FIRST_MESSAGE_S = 5.0
FIRST_MESSAGE_MAX = 4096
MAX_MESSAGE = 65536  # the WebSocket's max_size (also set on the server: uvicorn ws_max_size)
BEARER_RECHECK = 0.25
OUT_QUEUE = 512 * 1024  # decoded output bytes per WebSocket
CONTROL_LIMIT = 4096  # control frames are never dropped; a browser that lets this many pile up ends with overflow
RESIZE_EVERY = 0.25  # the writer's resizes, coalesced to at most 4 per second
REPLY_RATE, REPLY_BURST = 4.0, 8.0  # tag 1 (CPR/DA1 replies the page allowed)
WRITE_TIMEOUT = 10.0  # one input or resize frame to the runner socket
PENDING_INPUT = 1 << 20  # unacknowledged input bytes before this server stops reading the browser
MAX_STREAMS = 16  # open streams per session (SSE tabs and terminals together)
RUN_PAGES = 8

# Section 12.6.6, the close codes (the page decides retry by the close code alone).
CLOSE_TABLE = {
    1000: {"exited", "run.stopped", "cancelled"},
    4400: {"protocol", "invalid.input"},
    4401: {"session.ended", "ticket.refused"},
    4403: {
        "person.removed",
        "not_visible",
        "ticket_invalid",
        "op.not_allowed",
        "not_found",
        "runner.disabled",
        "runner.stale",
    },
    4409: {"writer_busy", "term.writer_busy"},
    4422: {"term.not_attachable"},
    4429: {"overflow", "rate.limited", "run.limit", "attach.limit", "quota.host"},
    4503: {
        "runner.lost",
        "runner.changed",
        "runner.restarting",
        "host.restarting",
        "runner.unavailable",
        "host.unavailable",
    },
}
CLOSE_OF = {name: code for code, names in CLOSE_TABLE.items() for name in names}
RETRYABLE = {"timeout", "host.busy", "lock.busy", "outcome.unknown", "workspace.unavailable"}


def close_code(reason: str, code: str | None = None, retryable: bool = False) -> int:
    """The close code for an `end`: by the catalog code behind `host.error`, else by the reason. Any other code is 4500,
    or 4503 when it is retryable."""
    key = code if reason == "host.error" and code else reason
    if key in CLOSE_OF:
        return CLOSE_OF[key]
    return 4503 if retryable or key in RETRYABLE else 4500


# -- the browser ticket ------------------------------------------------------------------------------------------------


@dataclass
class Intent:
    token: str
    ws: str
    run: str
    mode: str
    take: bool
    from_: int | None
    generation: str
    expires: float
    #: the listener the session (and so the ticket) belongs to; a ticket works on that listener's socket only
    listener: str = LOOPBACK


@dataclass
class Tickets:
    """Browser tickets: only the sha256 is kept; redeeming pops it (single use); expired ones are swept."""

    clock: Any = time.monotonic
    generation: str = field(default_factory=lambda: secrets.token_hex(16))
    items: dict[str, Intent] = field(default_factory=dict)
    minted: dict[str, deque] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def mint(
        self, token: str, ws: str, run: str, mode: str, take: bool, from_: int | None, listener: str = LOOPBACK
    ) -> tuple[str, float] | None:
        """A new ticket and its lifetime in seconds, or None when this session asked for 10 in the last minute."""
        with self.lock:
            now = self.clock()
            self.items = {h: i for h, i in self.items.items() if i.expires > now}
            # Sessions with no ticket in the last minute keep no rate state (review I1).
            self.minted = {k: d for k, d in self.minted.items() if d and d[-1] > now - 60}
            recent = self.minted.setdefault(_digest(token), deque())
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if len(recent) >= TICKET_RATE:
                return None
            recent.append(now)
            raw = secrets.token_urlsafe(16)  # 128 bits
            self.items[_digest(raw)] = Intent(
                token, ws, run, mode, take, from_, self.generation, now + TICKET_TTL, listener
            )
            return raw, TICKET_TTL

    def redeem(self, raw: str) -> Intent | None:
        with self.lock:
            intent = self.items.pop(_digest(raw), None)
            if intent is None or intent.expires <= self.clock() or intent.generation != self.generation:
                return None
            return intent

    def forget(self, token: str) -> None:
        """A logout drops that session's unused tickets."""
        with self.lock:
            self.items = {h: i for h, i in self.items.items() if i.token != token}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# -- routes ------------------------------------------------------------------------------------------------------------


class Cwd(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["workspace", "repo", "worktree", "workdir"]
    repo: Repo | None = None
    ticket: reads.Key | None = None


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    harness: Literal["shell", "claude-code", "codex"] = "shell"
    ticket: reads.Key | None = None
    mode: Literal["context"] | None = None
    model: Model | None = None
    cwd: Cwd | None = None
    label: Label | None = None

    def host_args(self) -> dict:
        args = self.model_dump(exclude_none=True, include={"harness", "ticket", "mode", "model", "label"})
        if self.cwd is not None:
            args["cwd"] = self.cwd.model_dump(exclude_none=True)
        return args


class OpenBody(PreviewBody):
    spec: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    cols: int | None = Field(default=None, ge=20, le=500)
    rows: int | None = Field(default=None, ge=5, le=200)
    idem: Idem


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AttachBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["read", "write"] = "read"
    take: bool = False
    from_: int | None = Field(default=None, ge=0, le=2**53 - 1, validation_alias="from")


def register(app: FastAPI, gateway: Gateway, tickets: Tickets) -> None:
    @app.get("/api/w/{ws}/runs")
    def run_list(ws: str = Depends(reads.workspace)):
        """The person's runs, live and kept (a full `run.list`: the page replaces its state with it after any
        reconnect or end of the live stream)."""
        out, page, runner = [], None, None
        for _ in range(RUN_PAGES):
            data = gateway.call(ws, "run.list", {"state": "all", **({"page": page} if page else {})})
            out += [project.run(r) for r in data["runs"] if isinstance(r, dict)]
            runner = data.get("runner")
            page = data.get("next_page")
            if not page:
                break
        return {"runs": out, "runner": project.runner(runner), "truncated": bool(page)}

    @app.post("/api/w/{ws}/terminals/preview")
    def preview(body: PreviewBody, ws: str = Depends(reads.workspace)):
        return preview_view(gateway.call(ws, "term.preview", body.host_args()))

    def terminals_listener(request: Request) -> str:
        """The listener of this request, if terminals are on there and the session belongs to it. A shell on the server
        is off on the front unless switched on (host spec 10.3): nothing is opened or minted otherwise."""
        listener = request.scope.get("orch_listener", LOOPBACK)
        policy = app.state.policies.get(listener)
        if policy is None or not policy.terminals:
            raise fail("not_found", "Terminals are not available on this connection.", exit=3)
        if app.state.auth.listener_of(request.state.token) != listener:
            raise fail("session.required", exit=3)
        return listener

    @app.post("/api/w/{ws}/terminals/open")
    def open_(body: OpenBody, request: Request, ws: str = Depends(reads.workspace)):
        terminals_listener(request)
        args = {**body.host_args(), "spec": body.spec}
        args.update({k: v for k, v in (("cols", body.cols), ("rows", body.rows)) if v is not None})
        data = gateway.enabled(ws).call("term.open", args, write=True, idem=body.idem)
        return {"run": project.run(data["run"])}

    @app.post("/api/w/{ws}/runs/{run}/stop")
    def stop(run: RunId, body: StopBody, ws: str = Depends(reads.workspace)):
        data = gateway.call(ws, "run.stop", {"run": run})
        cleanup = data.get("cleanup")
        return {
            "state": data.get("state") if data.get("state") in ("exited", "stopping") else None,
            "cleanup": cleanup if cleanup in ("complete", "incomplete", "unknown") else None,
        }

    @app.post("/api/w/{ws}/runs/{run}/attach")
    def attach(run: RunId, body: AttachBody, request: Request, ws: str = Depends(reads.workspace)):
        """Step 1 of the lazy attach: a browser ticket bound to this session, workspace, run and request. Nothing is
        asked of the host here."""
        listener = terminals_listener(request)
        minted = tickets.mint(request.state.token, ws, run, body.mode, body.take, body.from_, listener)
        if minted is None:
            raise fail("rate.limited")
        raw, ttl = minted
        return {"ticket": raw, "expires_in_ms": int(ttl * 1000)}


# -- the WebSocket -----------------------------------------------------------------------------------------------------


class Closed(Exception):
    """End the socket: an `end` message (unless `silent`), then the close code."""

    def __init__(
        self,
        reason: str,
        *,
        code: str | None = None,
        message: str | None = None,
        retry: int | None = None,
        close: int | None = None,
        silent: bool = False,
    ):
        super().__init__(reason)
        self.reason, self.code, self.message, self.retry, self.silent = reason, code, message, retry, silent
        self.close = close if close is not None else close_code(reason, code)

    def end(self) -> dict:
        out: dict[str, Any] = {"t": "end", "reason": self.reason}
        if self.code:
            out["code"] = self.code
        if self.message:
            out["message"] = self.message
        if self.close == 4429:
            out["retry_after_ms"] = self.retry if self.retry is not None else 1000
        return out


def host_closed(exc: HostError) -> Closed:
    """`term.attach` refused: `host.error` with the code; the host's message only when it is plain short text."""
    err = exc.error
    code = err.get("code") if valid_code(err.get("code")) else "internal"
    message = project.text(err.get("message"), 300)
    detail = err.get("detail") if isinstance(err.get("detail"), dict) else {}
    retry = next(
        (v for v in (err.get("retry_after_ms"), detail.get("retry_after_ms")) if retry_after(v) is not None), None
    )
    return Closed(
        "host.error",
        code=code,
        message=message if message and message != code else None,
        retry=retry,
        close=close_code("host.error", code, err.get("retryable") is True),
    )


class OutQueue:
    """Server to browser, in order. Output is bounded (512 KiB decoded): on overrun the oldest output is dropped and
    replaced by one `gap` with the runner's offsets. Control frames are never dropped."""

    def __init__(self, limit: int = OUT_QUEUE):
        self.items: deque = deque()
        self.bytes = 0
        self.controls = 0
        self.limit = limit
        self.ready = asyncio.Event()

    def put_out(self, off: int, data: bytes) -> None:
        self.items.append(["out", off, data])
        self.bytes += len(data)
        while self.bytes > self.limit:
            self._drop_oldest()
        self.ready.set()

    def put(self, frame: dict) -> None:
        self.items.append(["ctl", frame])
        self.controls += 1
        if self.controls > CONTROL_LIMIT:
            raise Closed("overflow")
        self.ready.set()

    def _drop_oldest(self) -> None:
        for i, item in enumerate(self.items):
            if item[0] == "out":
                del self.items[i]
                self.bytes -= len(item[2])
                start, end = item[1], item[1] + len(item[2])
                prev = self.items[i - 1] if i > 0 else None
                if prev is not None and prev[0] == "gap" and prev[2] == start:
                    prev[2] = end
                else:
                    self.items.insert(i, ["gap", start, end])
                return

    async def get(self):
        while not self.items:
            self.ready.clear()
            await self.ready.wait()
        item = self.items.popleft()
        if item[0] == "out":
            self.bytes -= len(item[2])
        elif item[0] == "ctl":
            self.controls -= 1
        return item


class SteadyWrites:
    """The TermClient's socket with a write timeout of its own (review M2, delta D-M1). Core's client sends input frame
    2.. from inside `recv` (after the previous frame's ack) and throttled resends from `pump`, all under whatever
    timeout `recv` last set (10..50 ms): too short for a 64 KiB frame through an 8 KiB AF_UNIX buffer while the runner
    is busy. Every `sendall` here runs under `WRITE_TIMEOUT`, and the read timeout is put back afterwards.
    (Core request: `TermClient.send` could do this itself.)"""

    def __init__(self, sock, stopped: threading.Event | None = None):
        self.sock = sock
        self.read_timeout = sock.gettimeout()
        self.stopped = stopped or threading.Event()

    def settimeout(self, value):
        self.read_timeout = value
        self.sock.settimeout(value)

    def gettimeout(self):
        return self.read_timeout

    def sendall(self, data):
        if self.stopped.is_set():  # nothing more reaches the runner once the attachment is over (delta2 D2-L1)
            raise ConnectionAbortedError("attachment stopped")
        self.sock.settimeout(WRITE_TIMEOUT)
        try:
            self.sock.sendall(data)
        finally:
            with suppress(OSError):
                self.sock.settimeout(self.read_timeout)

    def recv(self, n):
        return self.sock.recv(n)

    def fileno(self):
        return self.sock.fileno()

    def close(self):
        self.sock.close()


class Pump:
    """The thread that owns the `TermClient` after `connect`: it writes the browser's input and resizes, pumps
    throttled resends and reads the runner socket continuously, posting every frame to the event loop."""

    def __init__(self, client, loop: asyncio.AbstractEventLoop, post):
        self.client, self.loop, self.post = client, loop, post
        self.stopped = threading.Event()
        # Core's TermClient from orch-core #475 on has its own write deadline (`send_timeout`) and calls socket methods
        # this wrapper does not offer (setblocking, send): wrap only an older client.
        if client is not None and getattr(client, "sock", None) is not None and not hasattr(client, "send_timeout"):
            client.sock = SteadyWrites(client.sock, self.stopped)
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.pending = 0  # unacknowledged input bytes (read by the loop; written here)
        self.queued = 0  # input bytes put into the inbox and not yet taken by the pump (review L5)
        self.queued_lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, name="orch-dashboard-term", daemon=True)

    def start(self):
        self.thread.start()

    def put(self, item):
        """From the loop: input is counted until the pump takes it, so the browser pause sees it too."""
        if item[0] == "in":
            with self.queued_lock:
                self.queued += len(item[1])
        self.inbox.put(item)

    def stop(self):
        self.stopped.set()
        # A write already blocked on a slow runner ends now, not after WRITE_TIMEOUT; reads end too (delta2 D2-L1).
        sock = getattr(self.client, "sock", None)
        raw = sock.sock if isinstance(sock, SteadyWrites) else sock
        if isinstance(raw, socket.socket):
            with suppress(OSError):
                raw.shutdown(socket.SHUT_RDWR)

    def _deliver(self, item):
        if not self.stopped.is_set():
            with suppress(RuntimeError):  # the loop is closing
                self.loop.call_soon_threadsafe(self.post, item)

    def run(self):
        client = self.client
        try:
            while not self.stopped.is_set():
                while not self.stopped.is_set():  # stopped between queued items too (delta D-L3)
                    try:
                        kind, *rest = self.inbox.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "in":
                        with self.queued_lock:
                            self.queued -= len(rest[0])
                    if kind == "in":
                        client.send_input(rest[0])
                    elif kind == "resize":
                        client.resize(*rest)
                client.pump()
                self.pending = sum(len(chunk) for chunk in client.unacked.values())
                try:
                    frame = client.recv(0.05)
                except TimeoutError:
                    continue
                if frame is None:
                    self._deliver(("closed", client.ended, bool(client.lost)))
                    return
                self.pending = sum(len(chunk) for chunk in client.unacked.values())
                self._deliver(("frame", frame, bool(client.lost)))
                if frame.get("t") == "end":
                    return
        except Exception:  # never the frame or its bytes in a message or a log: the runner is gone for this socket
            self._deliver(("closed", "lost", bool(client.lost)))
        finally:
            client.close()


#: release tasks still running after their socket's handler ended (kept referenced until done)
_CLEANUPS: set[asyncio.Task] = set()


class TermSocket:
    """One browser WebSocket bridged to one attachment."""

    def __init__(self, websocket: WebSocket, gateway: Gateway, auth, tickets: Tickets, streams):
        self.websocket, self.gateway, self.auth, self.tickets, self.streams = websocket, gateway, auth, tickets, streams
        self.intent: Intent | None = None
        self.link: HostLink | None = None
        self.att: str | None = None
        self.pump: Pump | None = None
        self.out = OutQueue()
        self.writer = False
        self.read_only = True
        self.paused_until = 0.0
        self.bucket, self.bucket_at = REPLY_BURST, time.monotonic()
        self.resize_next: tuple[int, int] | None = None
        self.resize_at = 0.0
        self.resize_task: asyncio.Task | None = None
        self.final: Closed | None = None
        self.done = asyncio.Event()
        #: set when the socket is doomed while the bind runs: the bind thread presents no host ticket after it
        self.abort = threading.Event()
        #: a bind still running when the socket ends: `finish` closes the socket first, then waits for it and releases
        self.binding: asyncio.Task | None = None

    # -- the whole life of the socket
    async def serve(self) -> None:
        ws = self.websocket
        await ws.accept()
        counted = False
        tasks: list[asyncio.Task] = []
        try:
            raw = await self.first_ticket()
            intent = self.tickets.redeem(raw)
            listener = ws.scope.get("orch_listener", LOOPBACK)
            if (
                intent is None
                or not self.auth.valid(intent.token, touch=False)
                or intent.listener != listener
                or self.auth.listener_of(intent.token) != listener
            ):
                raise Closed("ticket.refused", silent=True, close=4401)
            self.intent = intent
            self.read_only = intent.mode == "read"
            if self.streams.count(intent.token) >= MAX_STREAMS:
                raise Closed("host.error", code="quota.host", close=4429)
            self.streams.add(intent.token)
            counted = True
            tasks.append(asyncio.create_task(self.watch_bearer()))
            # Any message before the attachment is bound, and a logout, end the socket at once (review L2, L3): the
            # bind is raced against both; a doomed bind presents no host ticket (`abort`, checked in its thread) and is
            # waited for in `finish`, after the socket has closed, so its capability is still cancelled.
            early = asyncio.create_task(self.refuse_early())
            ended = asyncio.create_task(self.done.wait())
            tasks += [early, ended]
            bind = asyncio.create_task(self.bind())
            await asyncio.wait({bind, early, ended}, return_when=asyncio.FIRST_COMPLETED)
            if not bind.done():
                self.abort.set()
                self.binding = bind
                if early.done() and not early.cancelled() and early.exception() is not None:
                    raise early.exception()
                return  # the bearer ended: `final` is set
            ready = bind.result()
            if self.done.is_set():
                return  # no `ready` after the session ended
            if early.done() and not early.cancelled() and early.exception() is not None:
                raise early.exception()
            early.cancel()
            with suppress(asyncio.CancelledError):
                await early
            self.writer = ready.get("writer") is True
            await ws.send_text(_json(_ready(ready)))
            tasks += [asyncio.create_task(self.from_browser()), asyncio.create_task(self.to_browser())]
            await self.done.wait()
        except Closed as exc:
            self.final = self.final or exc
        except Exception:
            self.final = self.final or Closed("internal", close=4500)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.resize_task:
                self.resize_task.cancel()
            await self.finish()
            if counted:
                self.streams.remove(self.intent.token)

    def end(self, closed: Closed) -> None:
        if self.final is None:
            self.final = closed
        self.done.set()

    async def finish(self) -> None:
        final = self.final or Closed("internal", close=4500)
        self.abort.set()
        if self.pump is not None:
            self.pump.stop()
        # The release must outlive a cancelled handler (the server may cancel it once the socket is closed): it runs as
        # its own task, started before the close handshake so `attach.cancel` never waits for a slow browser (delta
        # D-L3). A bind still running (its thread cannot be cancelled) is waited for first.
        cleanup = asyncio.create_task(self._cleanup())
        _CLEANUPS.add(cleanup)
        cleanup.add_done_callback(_CLEANUPS.discard)
        if self.websocket.application_state == WebSocketState.CONNECTED:
            with suppress(Exception):
                if not final.silent:
                    await self.websocket.send_text(_json(final.end()))
                await self.websocket.close(final.close)
        with suppress(asyncio.CancelledError):
            await asyncio.shield(cleanup)

    async def _cleanup(self) -> None:
        if self.binding is not None:
            await asyncio.gather(self.binding, return_exceptions=True)
            if self.pump is not None:
                self.pump.stop()
        att, link = self.att, self.link
        if att is not None or link is not None:
            await asyncio.to_thread(self._release, att, link)
        # The pump closes the runner socket in its `finally`: wait for it, bounded by one write (delta D-L3).
        thread = getattr(self.pump, "thread", None)
        if thread is not None:
            with suppress(RuntimeError):  # never started
                await asyncio.to_thread(thread.join, WRITE_TIMEOUT + 1)

    @staticmethod
    def _release(att, link):
        """`attach.cancel` (harmless for an attachment that already ended), then the host connection closes."""
        try:
            if att is not None and link is not None:
                with suppress(HostError, OSError, ValueError):
                    link.call("attach.cancel", {"att": att})
        finally:
            if link is not None:
                with suppress(OSError):
                    link.close()

    async def first_ticket(self) -> str:
        try:
            message = await asyncio.wait_for(self.websocket.receive(), FIRST_MESSAGE_S)
        except TimeoutError:
            raise Closed("protocol") from None
        if message.get("type") == "websocket.disconnect":
            raise Closed("cancelled", silent=True)
        text = message.get("text")
        if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > FIRST_MESSAGE_MAX:
            raise Closed("protocol")
        try:
            value = json.loads(text)
        except ValueError:
            raise Closed("protocol") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"ticket"}
            or not isinstance(value["ticket"], str)
            or not 1 <= len(value["ticket"]) <= 64
        ):
            raise Closed("protocol")
        return value["ticket"]

    async def watch_bearer(self) -> None:
        """Logout or expiry closes the socket within about a second: 4401, no `end` message."""
        while self.auth.valid(self.intent.token, touch=False):
            await asyncio.sleep(BEARER_RECHECK)
        self.final = Closed("session.ended", silent=True, close=4401)
        self.done.set()

    async def refuse_early(self) -> None:
        message = await self.websocket.receive()
        if message.get("type") == "websocket.disconnect":
            raise Closed("cancelled", silent=True)
        raise Closed("protocol")

    async def bind(self) -> dict:
        """`term.attach` on a host connection of this socket's own, the runner check (section 12.6.1), the host ticket
        presented at once; the runner's first frame."""
        intent = self.intent
        loop = asyncio.get_running_loop()

        def attach():
            from orch.runner.termclient import RunnerChanged, TermClient

            self.gateway.enabled(intent.ws)  # the dashboard addon is still on, through the shared connection
            self.link = self.gateway.new_link(intent.ws)
            args: dict[str, Any] = {"run": intent.run, "mode": intent.mode, "take": intent.take}
            if intent.from_ is not None:
                args["from"] = intent.from_
            answer = self.link.call("term.attach", args)
            self.att = answer.get("att") if isinstance(answer, dict) else None
            # Doomed meanwhile (logout, an early message, the socket closed): the host ticket is never presented.
            if self.abort.is_set() or not self.auth.valid(intent.token, touch=False):
                # Released here, in the thread (delta D-I1): even a loop torn down meanwhile cannot skip the cancel.
                att, link, self.att, self.link = self.att, self.link, None, None
                self._release(att, link)
                raise Closed("cancelled", silent=True)
            client = TermClient(self.gateway.run, answer)
            try:
                first = client.connect()
            except RunnerChanged:
                client.close()
                raise Closed("runner.changed") from None
            except BaseException:
                client.close()  # the ticket may have gone out: never leave the runner socket open (review L4)
                raise
            if first.get("t") == "end":
                client.close()
                reason = first.get("reason")
                raise Closed(reason if isinstance(reason, str) and reason in CLOSE_OF else "runner.lost")
            if first.get("t") != "ready":
                client.close()
                raise Closed("runner.lost")
            return client, first

        try:
            client, ready = await asyncio.to_thread(attach)
        except HostError as exc:
            raise host_closed(exc) from None
        except (OSError, KeyError, TypeError, ValueError):
            raise Closed("runner.lost") from None
        if self.abort.is_set():
            client.close()
            raise Closed("cancelled", silent=True)
        self.pump = Pump(client, loop, self.post)
        self.pump.start()
        return ready

    # -- runner to browser
    def post(self, item) -> None:
        """On the loop: one item from the pump."""
        try:
            kind, value, lost = item
            if kind == "closed":
                self.out.put({"t": "_end", "reason": "runner.lost", "lost": lost})
                return
            frame = value
            t = frame.get("t")
            if t == "out":
                try:
                    data = base64.b64decode(frame["b64"], validate=True)
                except ValueError:
                    self.out.put({"t": "_end", "reason": "runner.lost", "lost": lost})
                    return
                if not 0 <= frame["off"] < 2**63:  # 8-byte offset prefix (review I4): a runner fault, not a stall
                    self.out.put({"t": "_end", "reason": "runner.lost", "lost": lost})
                    return
                self.out.put_out(frame["off"], data)
            elif t == "gap":
                self.out.put({"t": "gap", "from": frame["from"], "to": frame["to"]})
            elif t == "resize":
                self.out.put({"t": "resize", "cols": frame["cols"], "rows": frame["rows"]})
            elif t == "writer":
                self.out.put({"t": "writer", "writer": frame["writer"] is True})
            elif t == "exit":
                code, sig = frame.get("code"), frame.get("signal")
                self.out.put(
                    {
                        "t": "exit",
                        "code": code if type(code) is int else None,
                        "signal": sig if isinstance(sig, str) and len(sig) <= 16 and sig.isalnum() else None,
                    }
                )
            elif t == "throttle":
                self.out.put({"t": "input_paused", "retry_ms": frame["retry_ms"]})
            elif t == "end":
                reason = frame["reason"]
                self.out.put({"t": "_end", "reason": reason if reason in CLOSE_OF else "runner.lost", "lost": lost})
        except Closed as exc:
            self.end(exc)

    async def to_browser(self) -> None:
        ws = self.websocket
        while True:
            item = await self.out.get()
            if item[0] == "out":
                await ws.send_bytes(struct.pack(">Q", item[1]) + item[2])
            elif item[0] == "gap":
                await ws.send_text(_json({"t": "gap", "from": item[1], "to": item[2]}))
            else:
                frame = item[1]
                if frame["t"] == "_end":
                    if frame["lost"]:
                        await ws.send_text(_json({"t": "input_lost"}))
                    self.end(Closed(frame["reason"]))
                    return
                if frame["t"] == "writer":
                    self.writer = frame["writer"]
                elif frame["t"] == "input_paused":
                    self.paused_until = time.monotonic() + frame["retry_ms"] / 1000
                await ws.send_text(_json(frame))

    # -- browser to runner
    async def from_browser(self) -> None:
        ws = self.websocket
        while True:
            wait = self.paused_until - time.monotonic()
            if wait > 0:  # throttled: stop reading the browser until the runner wants input again
                await asyncio.sleep(wait)
            while self.input_full():
                await asyncio.sleep(0.02)
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                self.end(Closed("cancelled", silent=True))
                return
            data, text = message.get("bytes"), message.get("text")
            if data is not None:
                self.on_binary(data)
            elif text is not None:
                self.on_text(text)
            else:
                self.end(Closed("protocol"))
                return
            if self.done.is_set():
                return

    def input_full(self) -> bool:
        """Input not yet written (queued for the pump) plus not yet acknowledged, against the 1 MiB bound."""
        return self.pump is not None and self.pump.pending + self.pump.queued > PENDING_INPUT

    def on_binary(self, data: bytes) -> None:
        if not data or len(data) > MAX_MESSAGE:
            return self.end(Closed("protocol"))
        tag, body = data[0], data[1:]
        if tag not in (0, 1):
            return self.end(Closed("protocol"))
        if self.read_only or not self.writer or not body:
            return  # a read attachment's input is dropped at the sink
        if tag == 1:
            now = time.monotonic()
            self.bucket = min(REPLY_BURST, self.bucket + (now - self.bucket_at) * REPLY_RATE)
            self.bucket_at = now
            if self.bucket < 1:
                return  # over the reply rate: dropped
            self.bucket -= 1
        self.pump.put(("in", bytes(body)))

    def on_text(self, text: str) -> None:
        if len(text) > FIRST_MESSAGE_MAX:
            return self.end(Closed("protocol"))
        try:
            value = json.loads(text)
        except ValueError:
            return self.end(Closed("protocol"))
        if (
            not isinstance(value, dict)
            or set(value) != {"t", "cols", "rows"}
            or value["t"] != "resize"
            or type(value["cols"]) is not int
            or type(value["rows"]) is not int
        ):
            return self.end(Closed("protocol"))
        cols, rows = value["cols"], value["rows"]
        if self.read_only or not self.writer or not (20 <= cols <= 500 and 5 <= rows <= 200):
            return  # only the writer's resize applies; sizes outside the range are refused
        self.resize_next = (cols, rows)
        if self.resize_task is None or self.resize_task.done():
            self.resize_task = asyncio.create_task(self.flush_resize())

    async def flush_resize(self) -> None:
        wait = self.resize_at + RESIZE_EVERY - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        size, self.resize_next = self.resize_next, None
        if size is not None and self.pump is not None:
            self.resize_at = time.monotonic()
            self.pump.put(("resize", *size))


def _ready(frame: dict) -> dict:
    """The browser's `ready`: never the lease generation, the socket, the runner's pid or generation."""
    state = frame.get("state")
    return {
        "t": "ready",
        "start": frame.get("start") if type(frame.get("start")) is int else 0,
        "offset": frame.get("offset") if type(frame.get("offset")) is int else 0,
        "cols": frame.get("cols") if type(frame.get("cols")) is int else 80,
        "rows": frame.get("rows") if type(frame.get("rows")) is int else 24,
        "writer": frame.get("writer") is True,
        "state": state if state in ("running", "exited") else "running",
    }


def _json(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"))


class Streams:
    """Open terminal sockets per session (counted with the SSE tabs toward 16 per session)."""

    def __init__(self, live):
        self.live = live
        self.open: dict[str, int] = {}

    def count(self, token: str) -> int:
        return self.open.get(token, 0) + self.live.tabs_of(token)

    def add(self, token: str) -> None:
        self.open[token] = self.open.get(token, 0) + 1

    def remove(self, token: str) -> None:
        left = self.open.get(token, 0) - 1
        if left > 0:
            self.open[token] = left
        else:
            self.open.pop(token, None)


def register_socket(app: FastAPI, gateway: Gateway, auth, tickets: Tickets, streams: Streams) -> None:
    @app.websocket("/term")
    async def term(websocket: WebSocket):
        await TermSocket(websocket, gateway, auth, tickets, streams).serve()

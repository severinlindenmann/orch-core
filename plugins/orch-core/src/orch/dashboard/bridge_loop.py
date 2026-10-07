"""The remote bridge's host loop: a background task in the dashboard's lifespan (one event loop with the pages and
the factory runner) that takes sealed requests from the relay's mailbox through the transport child, decides each with
the host library and answers it by running the dashboard app in memory.

One request, in order:

1. Host.check(envelope, mailbox id): shape, tag, device, replay, quota, framing, sequence, time, stream owner, record.
2. Host.authorize(accepted): the route's scope (route_hook below), then the run. `fresh` is set only by the library
   after a verified assertion; it is never read from a request.
3. bridge_dispatch.dispatch(app, request, RemoteOrigin from the DECISION: device, scope, label, fresh): the
   dispatcher asks Host.still_authorized(run) immediately before the app runs, before the first event and before
   every body event; the remote gate decides the route again, with the request as received.
4. A page: the whole response is collected, stored with Host.finish() BEFORE it is sent, and sent in sealed chunks of
   at most 256 KiB. finish() False: the stored refusal is sent instead (Host.end_run). A body over 64 KiB is stored as its head
   only (a replay is answered already_done with its status).
5. A stream (the STREAM flag): chunk 0 carries the status, then frames coalesced to the latest one, at most one per
   `frame_s` (4 per second), a keepalive after `keepalive_s` of silence, and a LAST chunk. still_authorized() is asked
   before EVERY chunk is sealed; on False the stream ends with the record's stored refusal. Cancel, revocation,
   scope change, the kill switch and shutdown close streams (the dispatcher is closed, so the app's route ends).

Per device at most MAX_INFLIGHT pages and MAX_STREAMS streams run at once; one more is refused `busy`, sealed and
stored. Mailbox errors back off from 1 s to a 10 s cap; host_taken, lease_lost, unauthorized, pending, not_owner and
no_space stop the loop with a message (the local dashboard keeps running). A heartbeat with counts only goes out every
`heartbeat_s`; a clean stop sends a goodbye and releases the lease.

This loop calls the Host's methods on the event loop's own thread; the Remote tab calls Host.revoke and Host.set_scope
from the dashboard's worker threads. Every Host method that reads or changes its in-memory state takes the host lock
(then, inside it, the registry lock), so the two never interleave. This loop's own stream table (`_streams`) is
touched only on the loop's thread: close_streams hands a call from another thread over with call_soon_threadsafe.
ponytail: the Host's file writes (fsync) block the loop for a moment per request; move them to one worker thread if
that ever shows in page latency.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import unquote

from orch.dashboard.bridge_dispatch import Body, BridgeRequest, Limits, Refused, Start, dispatch
from orch.dashboard.reach import RemoteOrigin, Scope
from orch.remote import presence
from orch.remote.bridge_host.envelope import F_STREAM, MAX_CHUNK, OVERHEAD, b64u, canonical_json, unb64u
from orch.remote.bridge_host.host_check import Requirement
from orch.remote.bridge_host.replay_store import MAX_REPLAY_BODY
from orch.remote.transport import Child, TransportError

log = logging.getLogger("orch.remote")

POLL_WAIT = 25
HEARTBEAT_S = 10.0
KEEPALIVE_S = 20.0  # the mailbox drops a stream after 60 s without a frame; the device after 60 s without a chunk
FRAME_S = 0.25  # at most 4 frames per second per stream
BACKOFF_START, BACKOFF_CAP = 1.0, 10.0
RESPOND_TRIES = 7  # 1+2+4+8+10+10 s of backoff: inside the mailbox's 60 s
MAX_INFLIGHT = 8  # pages per device at once
MAX_STREAMS = 4  # streams per device at once
_RID = re.compile(r"[0-9a-f]{32}")
RETRY = frozenset({"rate_limited", "network", "server"})
FATAL = {
    "host_taken": "remote: another orch host is serving this workspace through the relay. Stop that one, or start "
                  "this one again with --take-over.",
    "lease_lost": "remote: another host took this workspace over; the remote link is stopped. The local dashboard "
                  "keeps running.",
    "unauthorized": "remote: the relay refused this device (revoked or signed out). Approve it again, then restart.",
    "pending": "remote: this device is not approved on the relay yet. Approve it in your browser, then restart.",
    "not_owner": "remote: this device does not own the workspace's space on the relay, and only the owner can host "
                 "it.",
    "no_space": "remote: the workspace's space on the relay no longer exists.",
    "transport_exited": "remote: the transport (the relay addon's tool) stopped; see its message above. The local "
                        "dashboard keeps running.",
    "loop_crashed": "remote: the remote link failed and is stopped; the local dashboard keeps running. Restart to "
                    "try again.",
    "transport_failed": "remote: the transport (the relay addon's tool) did not start; see its message above. The "
                        "local dashboard keeps running.",
}
_ERRORS = {"bad_request": (400, b"This request cannot be read."), "too_large": (413, b"This is too large."),
           "timeout": (504, b"This took too long."), "error": (500, b"Something went wrong on the computer.")}


def _form(meta, data, query: str):
    """The parameters the gate would read for this request: the query and, for an urlencoded body, the form. None
    when the body is not an urlencoded form (as in the gate: multipart, no content type), is over the peek limit, or
    holds a ";" or a non-ASCII byte (the route's form parser reads those differently from a plain split), so the
    gate's strictest tag applies and no subject is built."""
    from urllib.parse import parse_qs, parse_qsl

    from orch.dashboard.remote_gate import MAX_PEEK
    headers = meta.get("headers")
    ctype = next((str(v) for k, v in (headers.items() if isinstance(headers, dict) else ())
                  if isinstance(k, str) and k.lower() == "content-type"), "")
    if ctype.lower().split(";")[0].strip() != "application/x-www-form-urlencoded":
        return None
    if not isinstance(data, bytes) or len(data) > MAX_PEEK or b";" in data or not data.isascii():
        return None
    params: dict = {}
    for k, v in parse_qsl(query.replace(";", "&"), keep_blank_values=True):
        params.setdefault(k, []).append(v)
    for k, v in parse_qs(data.decode("ascii"), keep_blank_values=True).items():
        params.setdefault(k, []).extend(v)
    return params


# The routes that type into or start something on the host and so need the typing lease. Every other Type route
# keeps what it had (Type alone, or a fresh assertion where the gate says so).
LEASE_ROUTES = frozenset({("POST", "/terminals/new"), ("POST", "/terminals/{name}/keys"),
                          ("POST", "/terminals/{name}/size"), ("POST", "/terminals/{name}/end"),
                          ("POST", "/t/{ref}/agent/start"), ("POST", "/quick/{qid}/agent/start")})


def route_hook(routes, ws=None):
    """The host library's route hook: what the remote gate's own table needs for the route this request matches.
    A route that needs a fresh assertion (a permission to allow, the Start of a factory epic, the epic's verdict, a
    change under a running factory epic) gets Requirement(scope, "fresh", subject): the subject is built from the
    workspace's LIVE state and the request's own hashes, so the person approves exactly what will be written and a
    stale request gets no challenge (R13). Without `ws`, or for a fresh route with no subject builder, no assertion
    is asked for and the gate refuses it (no origin is fresh). The gate decides every route again on the request
    as received, so this can only refuse more than the gate, never less. A Type route that is not fresh and is one of
    LEASE_ROUTES (terminal keys, size, end, new, Start agent) asks the library for the typing lease instead: a
    platform-authenticator assertion bound to the device, valid 15 minutes from the unlock (a run inside it does not
    extend it), given only to input sent on a stream the device itself opened. Other Type routes are as they were."""
    from orch.dashboard.factory_remote import subject
    from orch.dashboard.remote_gate import factory_need, match_route, tag_for

    def hook(meta, data):
        method, target = meta.get("method"), meta.get("path")
        if not isinstance(method, str) or not isinstance(target, str) or not target.startswith("/"):
            return None
        raw, _, query = target.partition("?")
        path = unquote(raw, errors="strict")
        scope = {"type": "http", "method": method, "path": path, "raw_path": raw.encode("ascii"),
                 "query_string": query.encode("ascii"), "root_path": "", "headers": []}
        params = _form(meta, data, query) if ws is not None else {}
        if ws is None:
            params = {}
        tag = tag_for(routes, scope, params)
        if tag is None or tag.scope is None:
            return None
        route = match_route(routes, scope)
        lease = tag.scope is Scope.TYPE and not tag.fresh and route is not None \
            and (method, route.path) in LEASE_ROUTES
        plain = Requirement(tag.scope.name.lower(), "lease" if lease else "none")
        if ws is None:
            return plain
        route_path = route.path if route is not None else None
        pp = route.matches(scope)[1].get("path_params", {}) if route is not None else {}
        needed, kind = tag.scope, None
        if route_path == "/permits/{rid}/grant":
            kind = "permission"
        n = factory_need(ws, method, route_path, pp, tag)
        if n is not None:
            needed, kind = n
        if kind is None:
            return plain
        body = data if isinstance(data, bytes) else b""
        return Requirement(needed.name.lower(), "fresh", subject(ws, kind, route_path, pp, _form(meta, data, ""), method, target, body))
    return hook


def origin_for(run) -> RemoteOrigin:
    """The origin the dashboard sees for an authorised run: device, scope and `fresh` from the host's decision only."""
    return RemoteOrigin(run.device, Scope[run.scope.upper()], str(getattr(run.entry, "label", "") or ""),
                        run.fresh is True)


class RemoteLink:
    """The BridgeLink the Remote tab reads (orch.remote.bridge_link): the link's state, and the kill switch."""

    def __init__(self, now=time.time):
        self._now = now
        self._status = {"state": "off", "since": None, "last_error": None, "host_online": False}
        self.on_disconnect = None

    def set(self, state: str, error: str | None = None, *, online: bool | None = None) -> None:
        if state != self._status["state"] or error != self._status["last_error"]:
            self._status.update(state=state, since=self._now(), last_error=error)
        if online is not None:
            self._status["host_online"] = online

    def status(self):
        return dict(self._status)

    def disconnect(self) -> None:
        if self.on_disconnect is not None:
            self.on_disconnect()


@dataclass
class _Stream:
    device: str
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    code: str | None = None  # end with this refusal (stopped, revoked, scope_changed)
    cancelled: bool = False  # the device cancelled it: a plain LAST chunk
    task: asyncio.Task | None = None


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


async def _wait(event: asyncio.Event, timeout: float | None) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    event.clear()


class HostLoop:
    def __init__(self, app, host, child_argv, ws, *, say=_say, child=None, poll_wait: int = POLL_WAIT,
                 heartbeat_s: float = HEARTBEAT_S, keepalive_s: float = KEEPALIVE_S, frame_s: float = FRAME_S,
                 limits: Limits = Limits(), beat=presence.heartbeat):
        self.app, self.host, self.ws, self.say = app, host, ws, say
        self.child = child or Child(child_argv, ws.root)
        self.poll_wait, self.heartbeat_s, self.keepalive_s, self.frame_s = poll_wait, heartbeat_s, keepalive_s, frame_s
        self.limits, self.beat = limits, beat
        self.link = RemoteLink()
        self.link.on_disconnect = self.disconnect
        self._tasks: set[asyncio.Task] = set()
        self._streams: dict[str, _Stream] = {}
        self._busy: dict[tuple[str, bool], int] = {}
        self._stopping = False
        self._done = asyncio.Event()
        self._beat_task: asyncio.Task | None = None
        self._ender: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sleep = asyncio.sleep  # the backoff's wait (tests record it)

    # -- life ------------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        """The background task: start the child, then poll until stopped or a fatal error."""
        self._loop = asyncio.get_running_loop()
        self.link.set("connecting")
        try:
            try:
                await self.child.start()
            except TransportError:
                await self._end("error", "transport_failed", goodbye=False)
                return
            self._beat_task = asyncio.create_task(self._heartbeats())
            code = await self._polling()
            if code:
                await self._end("error", code, goodbye=False)
        except Exception as e:  # noqa: BLE001 - a bug here must not leave the link reading "online"
            log.error("remote: the host loop failed (%s)", type(e).__name__)
            await self._end("error", "loop_crashed", goodbye=False)

    async def stop(self) -> None:
        """A clean stop (the dashboard shuts down): streams end `stopped`, a goodbye, the lease released."""
        await self._end("stopped", None)

    def disconnect(self) -> None:
        """The kill switch: nothing more runs (Host.stop at once), streams end `stopped`, and the link stops talking
        to the relay; the local dashboard keeps running. Safe from any thread."""
        try:
            self.host.stop()
        except Exception:  # noqa: BLE001 - the loop's own stop below calls it again
            log.warning("remote: the kill switch could not mark the host stopped at once")
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._schedule_end, "stopped", None, True,
                                            "remote: disconnected; the local dashboard keeps running")

    def close_streams(self, rids, code: str) -> None:
        """End these streams with refusal `code` (what Host.revoke and Host.set_scope return, with `revoked` or
        `scope_changed`). Safe from any thread: the Remote tab's routes run in the threadpool."""
        rids = list(rids)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is not None and running is not self._loop:
            if not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._close_streams, rids, code)
            return
        self._close_streams(rids, code)

    def _close_streams(self, rids, code: str) -> None:
        for rid in rids:
            st = self._streams.get(rid)
            if st is not None and st.code is None:
                st.code = code
                st.wake.set()

    def _schedule_end(self, state, code, goodbye=True, message=None) -> None:
        if not self._stopping and self._ender is None:
            self._ender = asyncio.ensure_future(self._end(state, code, goodbye=goodbye, message=message))

    async def _end(self, state: str, code: str | None, *, goodbye: bool = True, message: str | None = None) -> None:
        if self._stopping:
            await self._done.wait()
            return
        self._stopping = True
        try:
            text = message or FATAL.get(code or "")
            if text:
                self.say(text)
            try:
                self.host.stop()
            except Exception:  # noqa: BLE001
                log.warning("remote: could not mark the host stopped")
            self.close_streams(list(self._streams), "stopped")
            streams = [s.task for s in self._streams.values() if s.task is not None]
            if streams:
                await asyncio.wait(streams, timeout=5)
            me = asyncio.current_task()
            rest = [t for t in self._tasks if t is not me]
            for t in rest:
                t.cancel()
            await asyncio.gather(*rest, return_exceptions=True)
            if self._beat_task is not None and self._beat_task is not me:
                self._beat_task.cancel()
                await asyncio.gather(self._beat_task, return_exceptions=True)
            if goodbye and self.child.alive:
                for op in ("goodbye", "release"):
                    with contextlib.suppress(TransportError):
                        await self.child.call(op, timeout=5)
            await self.child.close()
        finally:
            self.link.set(state, code, online=False)
            self._done.set()

    # -- polling ---------------------------------------------------------------------------------------------------

    async def _polling(self) -> str | None:
        delay = 0.0
        while not self._stopping:
            try:
                ans = await self.child.call("poll", wait=self.poll_wait, timeout=self.poll_wait + 15)
            except TransportError as e:
                if self._stopping:
                    return None
                if e.code in FATAL:
                    return e.code
                if e.code == "exited":
                    return "transport_exited"
                delay = min(max(BACKOFF_START, delay * 2), BACKOFF_CAP)
                self.link.set("reconnecting", e.code, online=False)
                log.info("remote: poll failed (%s); next try in %.0f s", e.code, delay)
                await self._sleep(delay)
                continue
            delay = 0.0
            self.link.set("online", online=True)
            reqs = ans.get("requests")
            for r in reqs if isinstance(reqs, list) else []:
                self._spawn(self._handle(r))
        return None

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("remote: a request failed on the host (%s)", type(task.exception()).__name__)

    async def _heartbeats(self) -> None:
        while not self._stopping:
            try:
                beat = await asyncio.to_thread(self.beat, self.ws)
                await self.child.call("heartbeat", **beat)
            except TransportError as e:
                if e.code in FATAL:
                    self._schedule_end("error", e.code, False)
                    return
            except Exception as e:  # noqa: BLE001 - a beat that cannot be read is skipped, never invented
                log.info("remote: heartbeat skipped (%s)", type(e).__name__)
            await asyncio.sleep(self.heartbeat_s)

    # -- one request -----------------------------------------------------------------------------------------------

    async def _handle(self, r) -> None:
        rid, body = (r.get("rid"), r.get("body")) if isinstance(r, dict) else (None, None)
        if not isinstance(rid, str) or not _RID.fullmatch(rid) or not isinstance(body, str):
            return
        try:
            env = unb64u(body)
        except ValueError:
            return
        v = self.host.check(env, rid)
        if v.result == "accept":
            decided = self.host.authorize(v)  # its refusals carry no header: they answer the accepted request
            v = decided if decided.result == "drop" or decided.header is not None else \
                dataclasses.replace(decided, header=v.header, device=v.device)
        await self._answer(v)

    async def _answer(self, v) -> None:
        if v.result == "drop" or v.header is None:
            log.debug("remote: dropped (%s)", v.why)
            return
        rid, stream = v.header.rid.hex(), bool(v.header.flags & F_STREAM)
        if v.result == "refuse":
            await self._send(rid, [self.host.seal_refusal(v, stream=stream)])
        elif v.result == "replay":
            await self._send(rid, self._chunks(v.header, v.outcome or {}, v.body, stream=stream))
        elif v.result == "run":
            await self._run(v, stream)
        elif v.result == "cancel":
            st = self._streams.get(v.fields.get("stream"))
            if st is not None:
                st.cancelled = True
                st.wake.set()
            await self._send(rid, [self.host.seal_chunk(v.header, 0, {"cancelled": v.fields.get("stream")},
                                                        last=True)])
        else:  # pairing answers (pair_pending, pair_status, credential_begin, credential_finish)
            await self._send(rid, [self.host.seal_chunk(v.header, 0, dict(v.fields), last=True, stream=stream)])

    async def _refuse(self, run, stream: bool, code: str | None = None) -> None:
        """End `run` with the record's stored refusal (Host.end_run stores one first when there is none)."""
        v = self.host.end_run(run, code)
        await self._send(run.rid, [self.host.seal_refusal(v, stream=stream)])

    async def _run(self, run, stream: bool) -> None:
        key = (run.device, stream)
        if self._busy.get(key, 0) >= (MAX_STREAMS if stream else MAX_INFLIGHT):
            await self._refuse(run, stream, "busy")
            return
        self._busy[key] = self._busy.get(key, 0) + 1
        try:
            origin = origin_for(run)
            m = run.meta or {}
            req = BridgeRequest(m.get("method"), m.get("path"), m.get("headers", {}), run.data)
            await (self._stream(run, req, origin) if stream else self._page(run, req, origin))
        finally:
            self._busy[key] -= 1

    def _events(self, run, req, origin):
        return dispatch(self.app, req, origin, still_authorized=lambda: self.host.still_authorized(run),
                        limits=self.limits, on_error=lambda e: log.warning("remote: a route failed (%s)",
                                                                           type(e).__name__))

    @staticmethod
    def _head(start, reason) -> dict:
        if start is None:
            status = _ERRORS.get(reason, _ERRORS["error"])[0]
            return {"status": status, "headers": [["content-type", "text/plain; charset=utf-8"]]}
        return {"status": start.status, "headers": [[k, v] for k, v in start.headers]}

    def _store(self, run, head: dict, body: bytes) -> bool:
        """Store the outcome before anything of it is sent; False when the run may not be answered with it. A body
        too large to store is not stored, and is answered only while still_authorized() holds."""
        try:
            stored = self.host.finish(run.answer_rids, head, body)
        except ValueError:  # a head too large to store
            return self.host.still_authorized(run)
        return stored and (len(body) <= MAX_REPLAY_BODY or self.host.still_authorized(run))

    async def _page(self, run, req, origin) -> None:
        start, parts, reason = None, [], None
        async with contextlib.aclosing(self._events(run, req, origin)) as events:
            async for e in events:
                if isinstance(e, Start):
                    start = e
                elif isinstance(e, Body):
                    parts.append(e.chunk)
                else:
                    reason = e.reason if isinstance(e, Refused) else None
                    break
        if reason == "not_authorized":
            await self._refuse(run, False)
            return
        head = self._head(start if reason is None else None, reason)
        body = b"".join(parts) if reason is None else _ERRORS.get(reason, _ERRORS["error"])[1]
        if not self._store(run, head, body):
            await self._refuse(run, False)
            return
        await self._send(run.rid, self._chunks(run.header, head, body, stream=False))

    async def _stream(self, run, req, origin) -> None:
        rid, header, loop = run.rid, run.header, asyncio.get_running_loop()
        st = _Stream(run.device, task=asyncio.current_task())
        self._streams[rid] = st
        got = {"start": None, "frame": None, "end": None}

        async def produce():
            async with contextlib.aclosing(self._events(run, req, origin)) as events:
                async for e in events:
                    if isinstance(e, Start):
                        got["start"] = e
                    elif isinstance(e, Body):
                        got["frame"] = e.chunk  # coalesced: only the latest frame waits to be sent
                    else:
                        got["end"] = e.reason if isinstance(e, Refused) else "end"
                    st.wake.set()
                    if got["end"]:
                        return
            got["end"] = got["end"] or "error"
            st.wake.set()

        async def post(idx, meta, data=b"", last=False) -> bool:
            return await self._post(rid, idx, last, self.host.seal_chunk(header, idx, meta, data, last=last,
                                                                          stream=True))

        def allowed() -> bool:  # before EVERY chunk of a stream is sealed
            return st.code is None and self.host.still_authorized(run)

        prod = asyncio.ensure_future(produce())
        idx, head, sent_at, frame_at = 0, None, loop.time(), 0.0
        try:
            while True:
                if st.cancelled:
                    await post(idx, {}, last=True)
                    return
                if st.code is not None:
                    await self._refuse_frame(run, st.code)
                    return
                now = loop.time()
                if head is None:
                    if got["start"] is None and got["end"] is None:
                        await _wait(st.wake, None)
                        continue
                    if got["start"] is None:  # ended before it started
                        if got["end"] == "not_authorized":
                            await self._refuse_frame(run)
                            return
                        h, body = self._head(None, got["end"]), _ERRORS.get(got["end"], _ERRORS["error"])[1]
                        if not self._store(run, h, body):
                            await self._refuse_frame(run)
                            return
                        await post(0, h, body, last=True)
                        return
                    if not allowed():
                        await self._refuse_frame(run, st.code)
                        return
                    head = self._head(got["start"], None)
                    if not await post(0, head):
                        return
                    idx, sent_at = 1, loop.time()
                    continue
                if got["frame"] is not None:
                    left = frame_at + self.frame_s - now
                    if left > 0:
                        await _wait(st.wake, left)
                        continue
                    if not allowed():
                        await self._refuse_frame(run, st.code)
                        return
                    data, got["frame"] = got["frame"], None
                    if not await post(idx, {}, data):
                        return
                    idx, sent_at = idx + 1, loop.time()
                    frame_at = sent_at
                    continue
                if got["end"] is not None:
                    if got["end"] == "not_authorized" or not allowed() or not self._store(run, head, b""):
                        await self._refuse_frame(run, st.code)
                        return
                    await post(idx, {}, last=True)
                    return
                left = sent_at + self.keepalive_s - now
                if left > 0:
                    await _wait(st.wake, left)
                    continue
                if not allowed():
                    await self._refuse_frame(run, st.code)
                    return
                if not await post(idx, {"keepalive": True}):
                    return
                idx, sent_at = idx + 1, loop.time()
        finally:
            prod.cancel()
            await asyncio.gather(prod, return_exceptions=True)
            self.host.close_stream(rid)
            self._streams.pop(rid, None)

    async def _refuse_frame(self, run, code: str | None = None) -> None:
        """A stream's final chunk: the stored refusal (LAST | REFUSAL | STREAM)."""
        await self._refuse(run, True, code)

    # -- answers ---------------------------------------------------------------------------------------------------

    def _chunks(self, header, head: dict, body: bytes, *, stream: bool) -> list[bytes]:
        """A response as sealed chunks of at most 256 KiB: chunk 0 carries the head, the rest `{}`."""
        out, idx, meta, rest = [], 0, head, body
        while True:
            room = MAX_CHUNK - OVERHEAD - 4 - len(canonical_json(meta))
            piece, rest = rest[:room], rest[room:]
            out.append(self.host.seal_chunk(header, idx, meta, piece, last=not rest, stream=stream))
            if not rest:
                return out
            idx, meta = idx + 1, {}

    async def _send(self, rid: str, envs: list[bytes]) -> None:
        for idx, env in enumerate(envs):
            if not await self._post(rid, idx, idx == len(envs) - 1, env):
                return

    async def _post(self, rid: str, idx: int, last: bool, env: bytes) -> bool:
        """One chunk to the mailbox through the child; retried with backoff on the retryable codes."""
        delay = 0.0
        for _ in range(RESPOND_TRIES):
            try:
                await self.child.call("respond", rid=rid, idx=idx, last=last, body=b64u(env))
                return True
            except TransportError as e:
                if e.code in FATAL:
                    self._schedule_end("error", e.code, False)
                    return False
                if e.code not in RETRY:
                    log.info("remote: a response chunk was not taken (%s)", e.code)
                    return False
                delay = min(max(BACKOFF_START, delay * 2), BACKOFF_CAP)
                await self._sleep(delay)
        return False

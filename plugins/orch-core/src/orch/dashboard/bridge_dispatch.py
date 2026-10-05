"""In-memory dispatcher for the remote bridge: run one request against the dashboard's ASGI app, in this process and
this event loop, and hand the response back as events as the app produces them.

No crypto, no mailbox, no network and no thread lives here. The caller (the later `--remote` host) has already
verified who the device is and what it may do, and passes that as a `RemoteOrigin`; this module only builds the
request the dashboard sees. Call it only with an origin built from a verified, authorised decision.

What the dashboard sees is decided here, never by the caller's headers: a fixed allow-list of request headers is
copied over, Host and Origin are the dashboard's own, the dashboard's own token cookie is injected from
`app.state.token` (and stripped from anything sent back), the client address is not loopback, and the scope carries
the remote marker, so the outermost RemoteGate applies its scope table exactly as for any bridged request.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from typing import AsyncIterator, Callable
from urllib.parse import unquote

from orch.dashboard.auth import COOKIE
from orch.dashboard.reach import SCOPE_KEY, RemoteOrigin

# The only request headers a caller may set. Anything else (cookie, origin, host, forwarding headers, encodings) is
# dropped; content-length is computed here from the body.
ALLOWED_HEADERS = frozenset({"accept", "accept-language", "content-type", "if-none-match", "if-modified-since",
                             "range"})
LOCAL_HOST = "127.0.0.1"
MAX_PATH = 8192
MAX_HEADER_VALUE = 4096
_METHOD = re.compile(r"[A-Z]{3,7}")
_BAD_VALUE = re.compile(r"[\x00-\x1f\x7f]")
_NO_RESPONSE_HEADERS = frozenset({"set-cookie", "set-cookie2"})  # a response never sets a cookie for a device


@dataclass(frozen=True)
class BridgeRequest:
    method: str
    path: str  # the request target as received, path and query: "/t/L-0001?x=1" (percent-encoded, ASCII)
    headers: dict
    body: bytes = b""


@dataclass(frozen=True)
class Limits:
    max_request_bytes: int = 1 << 20
    max_response_bytes: int = 16 << 20
    max_seconds: float = 120.0  # one whole response, a stream included; the device reconnects after it
    max_chunk: int = 32 << 10  # a larger body piece from the app is split into events of at most this size
    grace_seconds: float = 2.0  # how long the app gets to finish after a disconnect before it is cancelled


@dataclass(frozen=True)
class Start:
    status: int
    headers: tuple


@dataclass(frozen=True)
class Body:
    chunk: bytes


@dataclass(frozen=True)
class End:
    pass


@dataclass(frozen=True)
class Refused:
    reason: str  # bad_request | not_authorized | too_large | timeout | error


ResponseEvent = Start | Body | End | Refused


def _build(app, request: BridgeRequest, origin, limits: Limits):
    """(scope, body) for the dashboard, or the reason this request is refused before anything runs."""
    token = getattr(getattr(app, "state", None), "token", None)
    if type(origin) is not RemoteOrigin or not isinstance(token, str) or not token or _BAD_VALUE.search(token) \
            or ";" in token or " " in token:
        return "error"
    if not isinstance(request, BridgeRequest) or not isinstance(request.body, bytes):
        return "bad_request"
    if len(request.body) > limits.max_request_bytes:
        return "too_large"
    method, target = request.method, request.path
    if not isinstance(method, str) or not _METHOD.fullmatch(method) or not isinstance(target, str) \
            or not isinstance(request.headers, dict):
        return "bad_request"
    if not target.startswith("/") or len(target) > MAX_PATH or not target.isascii() or "#" in target \
            or _BAD_VALUE.search(target) or " " in target:
        return "bad_request"
    raw, _, query = target.partition("?")
    try:
        path = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return "bad_request"
    headers = [(b"host", LOCAL_HOST.encode()), (b"origin", f"http://{LOCAL_HOST}".encode()),
               (b"cookie", f"{COOKIE}={token}".encode()), (b"content-length", str(len(request.body)).encode())]
    seen = set()
    for key, value in request.headers.items():
        name = key.lower() if isinstance(key, str) else ""
        if name not in ALLOWED_HEADERS:
            continue
        if not isinstance(value, str) or len(value) > MAX_HEADER_VALUE or _BAD_VALUE.search(value) \
                or not value.isascii() or name in seen:
            return "bad_request"
        seen.add(name)
        headers.append((name.encode(), value.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": raw.encode("ascii"),
             "query_string": query.encode("ascii"), "root_path": "", "headers": headers,
             "client": ("remote", 0), "server": (LOCAL_HOST, 0), SCOPE_KEY: origin}
    return scope, request.body


async def _authorized(still_authorized) -> bool:
    try:
        answer = still_authorized()
        if inspect.isawaitable(answer):
            answer = await answer
        return answer is True
    except Exception:  # noqa: BLE001 - fail closed
        return False


async def _stop(task: asyncio.Task, closing: asyncio.Event, queue: asyncio.Queue, grace: float) -> None:
    """Tell the app its client is gone, give it `grace` seconds to finish by itself, then cancel it; never leaves it
    running."""
    closing.set()
    try:
        while True:  # unblock a send waiting on a full queue
            queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    try:
        if not task.done():
            await asyncio.wait({task}, timeout=grace)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved: nothing is logged as never-awaited


async def dispatch(app, request: BridgeRequest, origin: RemoteOrigin, *, still_authorized: Callable,
                   limits: Limits = Limits()) -> AsyncIterator[ResponseEvent]:
    """Run `request` against the dashboard `app` as the paired device `origin` and yield its response as it is
    produced: Start, Body..., End; or a Refused (the last event) when it could not run or was cut off.

    `still_authorized` (a callable, sync or async, True to go on) is asked right before the app runs, before the
    response's first event and before every body event, so a revoked device stops at once, mid-stream included;
    anything but True, an error included, refuses. Leaving the iterator by any route (break, error, cancel,
    aclose) disconnects the app and waits for it, so a streaming route ends and no task is left behind."""
    built = _build(app, request, origin, limits)
    if isinstance(built, str):
        yield Refused(built)
        return
    scope, body = built
    if not await _authorized(still_authorized):
        yield Refused("not_authorized")
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=16)
    closing = asyncio.Event()  # the client is gone (or leaving): the app's receive() says http.disconnect
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await closing.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if closing.is_set():
            raise OSError("client disconnected")  # what a closed connection looks like to a response
        kind = message.get("type")
        if kind == "http.response.start":
            headers = tuple((k.decode("latin-1"), v.decode("latin-1")) for k, v in message.get("headers", ())
                            if k.decode("latin-1").lower() not in _NO_RESPONSE_HEADERS)
            await queue.put(("start", int(message["status"]), headers))
        elif kind == "http.response.body":
            data = message.get("body", b"")
            for i in range(0, len(data), limits.max_chunk):
                await queue.put(("body", data[i:i + limits.max_chunk]))
            if not message.get("more_body", False):
                await queue.put(("end",))

    async def run():
        try:
            await app(scope, receive, send)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - reported to the caller as a refusal
            if not closing.is_set():
                await queue.put(("failed", exc))
            return
        if not closing.is_set():
            await queue.put(("returned",))

    task = asyncio.ensure_future(run())
    try:
        deadline = loop.time() + limits.max_seconds
        total, started = 0, False
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                yield Refused("timeout")
                return
            tag = item[0]
            if tag == "start" and not started:
                if not await _authorized(still_authorized):
                    yield Refused("not_authorized")
                    return
                started = True
                yield Start(item[1], item[2])
            elif tag == "body" and started:
                total += len(item[1])
                if total > limits.max_response_bytes:
                    yield Refused("too_large")
                    return
                if not await _authorized(still_authorized):
                    yield Refused("not_authorized")
                    return
                yield Body(item[1])
            elif tag == "end" and started:
                yield End()
                return
            else:  # failed, returned without finishing, or a message out of order
                yield Refused("error")
                return
    finally:
        await _stop(task, closing, queue, limits.grace_seconds)

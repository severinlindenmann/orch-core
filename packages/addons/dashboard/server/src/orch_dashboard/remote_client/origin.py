"""One 127.0.0.1 origin of the remote client (R5 design sections 1 and 4).

An :class:`Origin` is a plain ASGI app bound to ONE port. The guard in front of every request is the same for the home
page and for a workspace session:

* ``Host`` exactly ``127.0.0.1:<port>``; ``Origin`` exactly ``http://127.0.0.1:<port>`` on every non-GET request and
  every
  WebSocket (and on a GET when present); ``Sec-Fetch-Site`` (when present) ``same-origin`` or ``none``;
* ``OPTIONS`` is 405 and no response ever carries an ``access-control-*`` header (no CORS at all); a request that comes
  from a service worker is refused and the page's CSP forbids workers;
* a per-launch bearer on every ``/api`` request (``/auth/session`` turns a single-use launch code into one);
* the path must be plain (no escape, no dot segment), checked on the raw bytes too.

A **session** origin turns ``/api/...`` and WebSockets into app frames of its session; the UI itself is served only
from the installed build (:class:`~orch_dashboard.remote_client.static.StaticSite`). A **home** origin serves the
client's own screens and ``/api/remote-client/...``. The browser's ``Authorization`` and ``X-Orch-*`` headers are
consumed here: the session's header allow-list (``orch.remoteclient.frames``) means they are never forwarded."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from orch.remoteclient import ClientError, WsRefused

from .static import StaticSite

__all__ = ["Origin", "csp", "json_response"]

BODY_MAX = 1 << 20
WS_MESSAGE_MAX = 65514
LAUNCH_TTL_S = 60.0
TOKENS_MAX = 4
Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]
#: ``(method, path, body) -> (status, json)`` for the home origin; runs in a worker thread
HomeApi = Callable[[str, str, dict[str, Any]], tuple[int, dict[str, Any]]]


def csp(port: int) -> str:
    return (
        f"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' ws://127.0.0.1:{port}; "
        "img-src 'self' data:; worker-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    )


def _security_headers(port: int) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-security-policy", csp(port).encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
        (b"cache-control", b"no-store"),
    ]


def json_response(status: int, doc: dict[str, Any]) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    body = json.dumps(doc, separators=(",", ":")).encode()
    return status, [(b"content-type", b"application/json")], body


def error_doc(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": False}}


class Origin:
    def __init__(
        self,
        *,
        port: int,
        kind: str,
        static: StaticSite,
        session: Any = None,
        home_api: HomeApi | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        assert kind in ("home", "session")
        self.port, self.kind, self.static = port, kind, static
        self.host = f"127.0.0.1:{port}"
        self.origin = f"http://{self.host}"
        self.session = session  # an orch.remoteclient Session (session origins)
        self.home_api = home_api
        self._clock = clock
        self._codes: dict[str, float] = {}
        self._tokens: list[str] = []
        self.last_seen = clock()
        self.on_redeem: Callable[[], None] | None = None  # the launcher removes its redirect file

    # -- credentials --

    def launch_code(self) -> str:
        """A single-use code (valid 60 s) for ``POST /auth/session``; it goes in the page address, never in a log."""
        now = self._clock()
        self._codes = {c: t for c, t in self._codes.items() if t > now}
        code = secrets.token_urlsafe(32)
        self._codes[code] = now + LAUNCH_TTL_S
        return code

    def _redeem(self, code: object) -> str | None:
        if type(code) is not str:
            return None
        found = None
        for c in list(self._codes):
            if hmac.compare_digest(c, code):
                found = c
        if found is None:
            return None
        expires = self._codes.pop(found)
        if self.on_redeem is not None:
            self.on_redeem()
        if expires <= self._clock():
            return None
        token = secrets.token_urlsafe(32)
        self._tokens = [*self._tokens[-(TOKENS_MAX - 1) :], token]
        return token

    def _bearer_ok(self, headers: dict[str, str]) -> bool:
        auth = headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return False
        given = auth[7:]
        ok = False
        for t in self._tokens:
            ok |= hmac.compare_digest(t, given)
        return ok

    # -- the guard --

    def _headers(self, scope: dict[str, Any]) -> tuple[dict[str, str], bool]:
        """Lower-cased headers; the flag is False when a name occurs twice (Host, Origin, ... are single-valued)."""
        out: dict[str, str] = {}
        single = True
        for k, v in scope.get("headers", []):
            n = k.decode("latin-1").lower()
            if n in out:
                single = False
            out[n] = v.decode("latin-1")
        return out, single

    def _refusal(self, scope: dict[str, Any], h: dict[str, str], single: bool, websocket: bool) -> str | None:
        """A reason (for tests; the browser sees "Not allowed") or None."""
        if not single or h.get("host") != self.host:
            return "host"
        method = scope.get("method", "GET")
        origin = h.get("origin")
        if websocket or method not in ("GET", "HEAD"):
            if origin != self.origin:
                return "origin"
        elif origin is not None and origin != self.origin:
            return "origin"
        # The browser is sent here by the launcher's redirect file (file://), which makes the page load a cross-site
        # NAVIGATION. That one request only receives the static page (no secret, no data, the code is in the fragment
        # and never sent); every other request must be same-origin.
        navigation = (
            method in ("GET", "HEAD")
            and h.get("sec-fetch-mode") == "navigate"
            and h.get("sec-fetch-dest") == "document"
        )
        if h.get("sec-fetch-site") not in (None, "same-origin", "none") and not (
            navigation and not scope.get("path", "/").startswith(("/api", "/auth"))
        ):
            return "fetch-site"
        if "service-worker" in h:
            return "service-worker"
        try:
            raw = scope.get("raw_path")
            raw_s = raw.decode("ascii") if raw is not None else scope["path"]
        except UnicodeDecodeError:
            return "path"
        path = scope["path"]
        if raw_s != path or not path.startswith("/") or "\\" in path or any(c in path for c in ";%"):
            return "path"
        if path != "/" and any(s in ("", ".", "..") for s in path[1:].split("/")):
            return "path"
        return None

    # -- ASGI --

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        self.last_seen = self._clock()
        if scope["type"] == "http":
            await self._http(scope, receive, self._wrap(send))
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        # lifespan: not used (lifespan="off")

    def _wrap(self, send: Send) -> Send:
        extra = _security_headers(self.port)

        async def out(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                names = {k for k, _ in extra}
                # nothing from a workspace's answer may speak for this origin: no CORS, no CSP, no cookie
                kept = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in names
                    and not k.lower().startswith(b"access-control-")
                    and k.lower() != b"set-cookie"
                ]
                message = {**message, "headers": [*kept, *extra]}
            await send(message)

        return out

    async def _reply(self, send: Send, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _json(self, send: Send, status: int, doc: dict[str, Any]) -> None:
        s, h, b = json_response(status, doc)
        await self._reply(send, s, h, b)

    async def _read_body(self, receive: Receive) -> bytes | None:
        out = bytearray()
        while True:
            m = await receive()
            if m["type"] == "http.disconnect":
                return None
            out += m.get("body", b"")
            if len(out) > BODY_MAX:
                return None
            if not m.get("more_body"):
                return bytes(out)

    async def _http(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        h, single = self._headers(scope)
        method, path = scope["method"], scope["path"]
        if method == "OPTIONS":  # a CORS preflight is answered with nothing to agree to
            return await self._reply(send, 405, [(b"allow", b"GET, HEAD, POST")], b"")
        why = self._refusal(scope, h, single, False)
        if why is not None:
            return await self._json(send, 403, error_doc("remote_client.refused", "Not allowed."))
        if path == "/auth/session" and method == "POST":
            return await self._launch(receive, send)
        if path.startswith("/auth/"):
            return await self._json(send, 404, error_doc("not_found", "Not found."))
        if path == "/api" or path.startswith("/api/"):
            if not self._bearer_ok(h):
                return await self._json(send, 401, error_doc("remote_client.bearer", "Not allowed."))
            if self.kind == "home":
                return await self._home(scope, receive, send, method, path)
            return await self._forward(scope, receive, send, h, method, path)
        if method not in ("GET", "HEAD"):
            return await self._json(send, 405, error_doc("method", "Not allowed."))
        await self._static(send, h, method, path)

    async def _launch(self, receive: Receive, send: Send) -> None:
        body = await self._read_body(receive)
        try:
            code = json.loads(body or b"").get("code") if body is not None and len(body) < 1024 else None
        except (ValueError, AttributeError):
            code = None
        token = self._redeem(code)
        if token is None:
            return await self._json(
                send, 401, error_doc("remote_client.launch", "Open this page again from the client.")
            )
        await self._json(send, 200, {"token": token})

    # -- the UI, from the installed build only --

    async def _static(self, send: Send, h: dict[str, str], method: str, path: str) -> None:
        f = self.static.lookup(self.kind, path)
        if f is None and "text/html" in h.get("accept", "") and "." not in path.rsplit("/", 1)[-1]:
            f = self.static.page(self.kind)  # the single-page app's own routes
        if f is None:
            return await self._json(send, 404, error_doc("not_found", "Not found."))
        await self._reply(send, 200, [(b"content-type", f.content_type.encode())], b"" if method == "HEAD" else f.body)

    # -- the home origin --

    async def _home(self, scope: dict[str, Any], receive: Receive, send: Send, method: str, path: str) -> None:
        assert self.home_api is not None
        body = await self._read_body(receive)
        if body is None:
            return await self._json(send, 413, error_doc("too_large", "Too much data."))
        try:
            doc = json.loads(body) if body else {}
        except ValueError:
            doc = None
        if type(doc) is not dict:
            return await self._json(send, 400, error_doc("malformed", "That did not look right."))
        status, out = await asyncio.to_thread(self.home_api, method, path, doc)
        await self._json(send, status, out)

    # -- a session origin: /api/... into the stream --

    async def _forward(
        self, scope: dict[str, Any], receive: Receive, send: Send, h: dict[str, str], method: str, path: str
    ) -> None:
        session = self.session
        if session is None or session.closed is not None:
            return await self._json(send, 410, error_doc("expired", "The session has ended. Open it again."))
        body = await self._read_body(receive)
        if body is None:
            return await self._json(send, 413, error_doc("too_large", "Too much data was sent."))
        query = scope.get("query_string", b"").decode("latin-1")
        try:
            resp = await asyncio.to_thread(session.request, method, path, query, list(h.items()), body)
        except ClientError as exc:
            return await self._json(send, 502 if exc.code != "too_large" else 413, error_doc(exc.code, exc.message))
        skip = {"content-length", "transfer-encoding", "connection"}
        headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in resp.headers if k not in skip]
        await send({"type": "http.response.start", "status": resp.status, "headers": headers})
        chunks = resp.chunks()

        def pull() -> bytes | None:
            try:
                return next(chunks)
            except (StopIteration, ClientError):
                return None

        try:
            while True:
                chunk = await asyncio.to_thread(pull)
                if chunk is None:
                    break
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
        except BaseException:
            resp.cancel()
            raise

    # -- WebSocket (session origins only) --

    async def _websocket(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        h, single = self._headers(scope)
        msg = await receive()
        if msg["type"] != "websocket.connect":
            return
        session = self.session
        path = scope["path"]
        if (
            self.kind != "session"
            or session is None
            or session.closed is not None
            or self._refusal({**scope, "method": "GET"}, h, single, True) is not None
        ):
            return await send({"type": "websocket.close", "code": 1008})
        query = scope.get("query_string", b"").decode("latin-1")
        try:
            ws = await asyncio.to_thread(session.websocket, path, query, list(h.items()))
        except (WsRefused, ClientError):
            return await send({"type": "websocket.close", "code": 1008})
        await send({"type": "websocket.accept"})
        done = asyncio.Event()

        async def up() -> None:
            try:
                while True:
                    m = await receive()
                    if m["type"] == "websocket.disconnect":
                        break
                    data = m.get("text")
                    if data is not None:
                        if len(data.encode()) > WS_MESSAGE_MAX:
                            await send({"type": "websocket.close", "code": 1009})
                            break
                        await asyncio.to_thread(ws.send_text, data)
                    elif m.get("bytes") is not None:
                        if len(m["bytes"]) > WS_MESSAGE_MAX:
                            await send({"type": "websocket.close", "code": 1009})
                            break
                        await asyncio.to_thread(ws.send_binary, m["bytes"])
            except ClientError:
                pass
            finally:
                done.set()

        async def down() -> None:
            try:
                while not done.is_set():
                    got = await asyncio.to_thread(ws.recv, 1.0)
                    if got is None:
                        continue
                    kind, val = got
                    if kind == "text":
                        await send({"type": "websocket.send", "text": val})
                    elif kind == "binary":
                        await send({"type": "websocket.send", "bytes": val})
                    else:
                        await send({"type": "websocket.close", "code": int(val) if 1000 <= int(val) < 5000 else 1011})
                        break
            finally:
                done.set()

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        await done.wait()
        for t in tasks:
            t.cancel()
        await asyncio.to_thread(ws.close, 1000)

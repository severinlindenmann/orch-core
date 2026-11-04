"""R3 T4 kit: drive the real stream adapter (`remote_device.Terminator`) with app frames, as the host would hand them,
and read back the frames it would send. The host's side is faked at the frame boundary (no socket anywhere)."""

# ruff: noqa: F811

import asyncio
import json
import time
from collections import defaultdict

from orch.canon.jcs import dumps as cj
from orch.crypto.encoding import b64u, unb64u

from orch_dashboard import remote_device as rd
from orch_dashboard.remote_device import BODY, END, HEAD, WS_OPEN, frame

STREAM = "5e" * 16
OTHER = "6f" * 16
DEVICE = "rd_" + "a1" * 16
PERSON = "p_" + "0f" * 16


def head(method, path, query="", headers=()):
    return cj({"headers": [list(h) for h in headers], "method": method, "path": path, "query": query})


def ws_head(path, query="", headers=()):
    return cj({"headers": [list(h) for h in headers], "path": path, "query": query})


class Wire:
    """One Terminator on the running loop: `send` feeds host frames, `pump` collects what it would send."""

    def __init__(self, app, auth, ws, **kw):
        self.app, self.auth, self.ws = app, auth, ws
        self.term = rd.Terminator(app, auth, asyncio.get_running_loop(), **kw)
        self.frames = defaultdict(list)  # (stream, id) -> [(kind, payload)]
        self.closes = []

    def open(self, sid=STREAM, person=PERSON, workspace=None):
        self.term.handle(
            {
                "t": "open",
                "stream_id": sid,
                "api": 1,
                "workspace_id": workspace or self.ws,
                "device_id": DEVICE,
                "person": person,
                "level": "work",
            }
        )

    def identity(self, level="work", person=PERSON, workspace=None):
        return {"workspace_id": workspace or self.ws, "device_id": DEVICE, "person": person, "level": level}

    def send(self, kind, ident, payload=b"", sid=STREAM, level="work", identity=None):
        msg = {"t": "frame", "stream_id": sid, "bytes": b64u(frame(kind, ident, payload))}
        if kind in (HEAD, WS_OPEN):
            msg["identity"] = identity or self.identity(level)
        self.term.handle(msg)

    def request(self, ident, method, path, query="", headers=(), body=b"", sid=STREAM, level="work", end=True):
        self.send(HEAD, ident, head(method, path, query, headers), sid, level)
        if body:
            self.send(BODY, ident, body, sid)
        if end:
            self.send(END, ident, b"", sid)

    def closed(self, sid=STREAM):
        self.term.handle({"t": "closed", "stream_id": sid, "reason": "revoked"})

    def pump(self):
        """What the poll thread would take: record it, and give the room back."""
        send, close, counts = self.term.take_outbound(1000, 1000)
        for item in send:
            pt = unb64u(item["bytes"])
            self.frames[(item["stream_id"], int.from_bytes(pt[1:5], "big"))].append((pt[0], pt[5:]))
        self.closes += [(c["stream_id"], c["reason"]) for c in close]
        self.term.sent(counts)

    async def until(self, pred, timeout=5.0):
        end = time.monotonic() + timeout
        while True:
            self.pump()
            if pred():
                return
            assert time.monotonic() < end, f"condition not reached: {dict(self.frames)} {self.closes}"
            await asyncio.sleep(0.01)

    def kinds(self, ident, sid=STREAM):
        return [k for k, _ in self.frames[(sid, ident)]]

    def response(self, ident, sid=STREAM):
        """(status, headers, body) of a complete response, or None."""
        items = self.frames[(sid, ident)]
        if not items or items[0][0] != HEAD or items[-1][0] != END:
            return None
        h = json.loads(items[0][1])
        return h["status"], dict(h["headers"]), b"".join(p for k, p in items if k == BODY)

    async def settle(self):
        """Let the tasks and the executor jobs of the adapter finish."""
        for _ in range(5):
            await asyncio.sleep(0.02)
            self.pump()


def spy_app(seen, body=b"ok", status=200, headers=()):
    """An ASGI app that records its scope and answers a fixed body (or waits, when `body` is None)."""

    async def app(scope, receive, send):
        seen.append(scope)
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.accept"})
            while True:
                event = await receive()
                if event["type"] == "websocket.disconnect":
                    return
                await send({"type": "websocket.send", "text": event.get("text") or "bin"})
        got = b""
        while True:
            event = await receive()
            got += event.get("body", b"")
            if not event.get("more_body"):
                break
        scope["received"] = got
        await send(
            {"type": "http.response.start", "status": status, "headers": [(b"content-type", b"text/plain"), *headers]}
        )
        await send({"type": "http.response.body", "body": body})

    return app


class App:
    """The minimum `app` the Terminator needs: a callable with `.state` (the real app has one)."""

    def __init__(self, inner, auth):
        self.inner = inner
        self.state = type("S", (), {"auth": auth})()

    async def __call__(self, scope, receive, send):
        await self.inner(scope, receive, send)

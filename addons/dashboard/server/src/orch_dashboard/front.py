"""The front socket's side of the remote dashboard (#1198 R1-4; host spec 0.8.6 item 10.2).

A Unix socket only the front (Caddy, its own system user) may use. Two checks, in this order:
1. `PeerChecked`: every accepted connection's peer UID must be the configured front UID; any other is closed before a
   byte is read. This answers the open point of EDGE.md: on a loopback port every local process can claim any client
   address; on this socket only the front can.
2. `Front`: the one ASGI wrapper of the front server. It marks every request as the remote listener's
   (`scope["orch_listener"]`, which `security.Security` maps to the frozen remote policy: a header never does) and reads
   the single header `X-Orch-Client` that the front sets. Nothing else reads that header, and the loopback server never
   passes through here, so a local process cannot claim an address, a kind of session or a listener by sending it."""

import socket

from .auth import client_address
from .errors import response
from .hostlink import peer_uid

CLIENT_HEADER = b"x-orch-client"
LISTENER = "remote"


class PeerChecked(socket.socket):
    """A listening socket whose `accept` drops every connection from another UID than ``front_uid``. Fail closed: a
    peer whose UID cannot be read is dropped too. (asyncio's event loop must be the selector one, which calls
    `accept` here; uvloop would accept in C and skip the check, so the front server runs `loop="asyncio"`.)"""

    def __init__(self, front_uid: int, family=socket.AF_UNIX):
        super().__init__(family, socket.SOCK_STREAM)
        self.front_uid = front_uid

    def accept(self):
        conn, addr = super().accept()
        try:
            allowed = peer_uid(conn) == self.front_uid
        except OSError:
            allowed = False
        if not allowed:
            conn.close()
            raise ConnectionAbortedError("peer is not the front")  # asyncio treats this as a false alarm and goes on
        return conn, addr


class Front:
    """Wrap the dashboard app for the front server (and only for it)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if (
            kind == "lifespan"
        ):  # the loopback server owns the app's lifespan; this one is a second server on the same app
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if kind not in ("http", "websocket"):
            return
        values = [v for k, v in scope["headers"] if k.lower() == CLIENT_HEADER]
        try:
            client = client_address(values[0].decode("ascii")) if len(values) == 1 else None
        except UnicodeDecodeError:
            client = None
        if client is None:
            # The front always sets exactly one address, replacing any the browser sent. Anything else did not come
            # through the front's configuration: refuse.
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await response("invalid.input", 400)(scope, receive, send)
            return
        scope = {
            **scope,
            "headers": [(k, v) for k, v in scope["headers"] if k.lower() != CLIENT_HEADER],
            "orch_listener": LISTENER,
            "state": {**scope.get("state", {}), "client": client},
        }
        await self.app(scope, receive, send)

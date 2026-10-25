"""The front socket's side of the remote dashboard (#1198 R1-4; host spec 0.8.6 item 10.2).

A Unix socket only the front (Caddy, its own system user) may use, in root's directory `/run/orch/front/<uid>`
(group `orch-front`, 0750; see `__main__.front_socket_path`). Two checks, in this order:
1. `PeerChecked`: every accepted connection's peer UID must be the configured front UID; any other is closed before a
   byte is read. This answers the open point of EDGE.md: on a loopback port every local process can claim any client
   address; on this socket only the front can.
2. `Front`: the one ASGI wrapper of the front server. It marks every request as the remote listener's
   (`scope["orch_listener"]`, which `security.Security` maps to the frozen remote policy: a header never does) and reads
   the single header `X-Orch-Client` that the front sets. Nothing else reads that header, and the loopback server never
   passes through here, so a local process cannot claim an address, a kind of session or a listener by sending it."""

import socket
import sys

from .auth import client_address
from .errors import response
from .hostlink import peer_uid
from .policy import REMOTE

CLIENT_HEADER = b"x-orch-client"
PROC_TCP = "/proc/net/tcp"  # IPv4 sockets with their owner UID; the loopback listener is AF_INET only


class PeerChecked(socket.socket):
    """A listening socket whose `accept` drops every connection from another UID than ``front_uid``. Fail closed: a
    peer whose UID cannot be read is dropped too. (asyncio's event loop must be the selector one, which calls
    `accept` here; uvloop would accept in C and skip the check, so every server on such a socket runs `loop="asyncio"`.)
    """

    def __init__(self, front_uid: int, family=socket.AF_UNIX):
        super().__init__(family, socket.SOCK_STREAM)
        self.front_uid = front_uid

    def peer_allowed(self, conn) -> bool:
        return peer_uid(conn) == self.front_uid

    def accept(self):
        conn, addr = super().accept()
        try:
            allowed = self.peer_allowed(conn)
        except (OSError, ValueError):
            allowed = False
        if not allowed:
            conn.close()
            raise ConnectionAbortedError("peer is not allowed")  # asyncio treats this as a false alarm and goes on
        return conn, addr


def tcp_owner_uid(conn, proc_tcp=PROC_TCP) -> int:
    """The UID that owns the other end of an accepted loopback TCP connection (Linux, L8 of #1198 H1).

    `SO_PEERCRED` exists only for Unix sockets. For TCP the kernel lists every socket with its owner UID in
    `/proc/net/tcp`: the row whose local address is our peer address and whose remote address is our local address is
    the client's socket, and its `uid` column is the user that created it. (Both ends are on 127.0.0.1, so the pair of
    ports is unique among live connections.) Raises OSError if the row is not found: the caller fails closed. Limit: the
    owner is the socket's creator, so a client that passes the descriptor to another UID's process is judged as the
    creator; only the person's own processes can hand one over, and they could connect themselves."""
    (lhost, lport), (rhost, rport) = conn.getsockname()[:2], conn.getpeername()[:2]
    want_local, want_remote = _hex_addr(rhost, rport), _hex_addr(lhost, lport)
    with open(proc_tcp) as table:
        next(table, None)  # header
        for line in table:
            fields = line.split()
            if len(fields) > 7 and fields[1] == want_local and fields[2] == want_remote:
                return int(fields[7])
    raise OSError("no socket entry for the peer")


def _hex_addr(host, port) -> str:
    """`/proc/net/tcp` writes an IPv4 address as the host-order hex of its four bytes, then the port in hex."""
    packed = socket.inet_aton(host)
    return f"{int.from_bytes(packed, sys.byteorder):08X}:{port:04X}"


class LoopbackPeerChecked(PeerChecked):
    """The dashboard's loopback TCP listener: on Linux every accepted connection must come from the person's UID, so
    an agent user (`orch-agent-*`) or any other local user cannot reach even the login page. An SSH tunnel's forwarding
    runs as the person and passes. Elsewhere (macOS has no way to ask the kernel who owns a TCP peer) nothing is
    dropped and the bearer stays the only gate. ``platform`` and ``proc_tcp`` are for tests."""

    def __init__(self, person_uid: int, platform=None, proc_tcp=PROC_TCP):
        super().__init__(person_uid, socket.AF_INET)
        self.enforced = (platform or sys.platform).startswith("linux")
        self.proc_tcp = proc_tcp

    def peer_allowed(self, conn) -> bool:
        return not self.enforced or tcp_owner_uid(conn, self.proc_tcp) == self.front_uid


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
            "orch_listener": REMOTE,
            "state": {**scope.get("state", {}), "client": client},
        }
        await self.app(scope, receive, send)

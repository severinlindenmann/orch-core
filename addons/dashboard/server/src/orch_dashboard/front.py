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
import struct
import sys

from .auth import client_address
from .errors import response
from .hostlink import peer_uid
from .policy import REMOTE

CLIENT_HEADER = b"x-orch-client"
NETLINK_SOCK_DIAG = 4  # NETLINK_INET_DIAG
SOCK_DIAG_BY_FAMILY = 20
NLMSG_ERROR = 2
NLM_F_REQUEST = 1
TCP_ESTABLISHED = 1
_NLHDR = struct.Struct("=IHHII")  # len, type, flags, seq, pid
_DIAG_REQ = struct.Struct("=BBBBI")  # family, protocol, ext, pad, states
_DIAG_ID = struct.Struct("!HH4s12s4s12sI2I")  # sport, dport, src, dst, if, cookie (src/dst: IPv4 + 12 pad bytes)
_DIAG_MSG = struct.Struct(
    "=BBBB"
)  # family, state, timer, retrans (then the id, then expires, rqueue, wqueue, uid, inode)
_UID_AT = _NLHDR.size + _DIAG_MSG.size + _DIAG_ID.size + 12


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


def open_netlink():
    sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_SOCK_DIAG)
    sock.settimeout(1.0)
    return sock


def tcp_owner_uid(conn, netlink=open_netlink) -> int:
    """The UID that owns the other end of an accepted loopback TCP connection (Linux, L8 of #1198 H1).

    `SO_PEERCRED` exists only for Unix sockets. For TCP we ask the kernel's sock_diag (NETLINK_INET_DIAG) for the
    client's socket by its exact 4-tuple (our peer address as source, our address as destination): one hash lookup,
    never a dump of all sockets (a table an agent can fill up would make every accept slow). The reply carries the
    socket's owner `idiag_uid` and its state. Only an ESTABLISHED socket counts: orphaned and TIME_WAIT entries are
    reported with uid 0, which a root person would take for themselves. Any error (no tcp_diag in the kernel, a short
    or odd reply, no such socket) raises OSError, and the caller drops the connection. Stdlib only. Limit: the owner
    is the socket's creator, so a client that passes the descriptor to another UID's process is judged as the creator;
    only the person's own processes can hand one over, and they could connect themselves."""
    (lhost, lport), (rhost, rport) = conn.getsockname()[:2], conn.getpeername()[:2]
    seq = 1
    ident = _DIAG_ID.pack(
        rport, lport, socket.inet_aton(rhost), bytes(12), socket.inet_aton(lhost), bytes(12), 0, 0xFFFFFFFF, 0xFFFFFFFF
    )
    body = _DIAG_REQ.pack(socket.AF_INET, socket.IPPROTO_TCP, 0, 0, 1 << TCP_ESTABLISHED) + ident
    request = _NLHDR.pack(_NLHDR.size + len(body), SOCK_DIAG_BY_FAMILY, NLM_F_REQUEST, seq, 0) + body
    sock = netlink()
    try:
        sock.send(request)
        reply = sock.recv(4096)
    finally:
        sock.close()
    if len(reply) < _UID_AT + 4:  # short, or an NLMSG_ERROR (which is 36 bytes)
        raise OSError("sock_diag gave no socket entry for the peer")
    length, kind, _flags, rseq, _pid = _NLHDR.unpack_from(reply)
    if kind != SOCK_DIAG_BY_FAMILY or rseq != seq or length < _UID_AT + 4 or length > len(reply):
        raise OSError("sock_diag gave an unexpected reply")
    family, state, _timer, _retrans = _DIAG_MSG.unpack_from(reply, _NLHDR.size)
    if family != socket.AF_INET or state != TCP_ESTABLISHED:
        raise OSError("the peer socket is not an established one")
    return struct.unpack_from("=I", reply, _UID_AT)[0]


class LoopbackPeerChecked(PeerChecked):
    """The dashboard's loopback TCP listener: on Linux every accepted connection must come from the person's UID, so
    an agent user (`orch-agent-*`) or any other local user cannot reach even the login page. An SSH tunnel's forwarding
    runs as the person and passes. Elsewhere (macOS has no way to ask the kernel who owns a TCP peer) nothing is
    dropped and the bearer stays the only gate. ``platform`` and ``netlink`` are for tests."""

    def __init__(self, person_uid: int, platform=None, netlink=open_netlink):
        super().__init__(person_uid, socket.AF_INET)
        self.enforced = (platform or sys.platform).startswith("linux")
        self.netlink = netlink

    def peer_allowed(self, conn) -> bool:
        return not self.enforced or tcp_owner_uid(conn, self.netlink) == self.front_uid


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

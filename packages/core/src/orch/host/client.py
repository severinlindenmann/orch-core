"""Pinned Unix client connection and handshake."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from . import frames
from ._socket import peer_credentials, recv
from ._unix import UnsafePath
from .listener import check_socket_path

#: the ops this client calls above contract version 1 (§0.4: an op left out of ``hello.ops`` is assumed at 1)
CLIENT_OPS: dict[str, int] = {"task.done": 2}


class Refused(ConnectionError):
    """The host answered the hello with a ``fatal`` frame (for example ``peer.denied``): it will not talk to us."""

    def __init__(self, code: str) -> None:
        super().__init__(f"the host refused the connection: {code}")
        self.code = code


class PinMismatch(UnsafePath):
    """The process behind the socket is not the one the trusted registry or pin names (a hard refusal, never a
    retry): ``host.untrusted``."""


@dataclass(frozen=True)
class Pin:
    """Trusted registry entry, NOT values read from workspace config.json.

    Person clients obtain this from their own registry. Agent clients obtain it
    from the root-owned setup-helper pin file; H2a/H3c supply its storage format.
    workspace_id is None for a control/worker endpoint.
    """

    host_uid: int
    workspace_id: str | None
    socket_path: Path

    def check_welcome(self, welcome: dict[str, Any]) -> None:
        frames.validate_envelope(welcome, sender="host")
        workspace = welcome.get("workspace")
        actual = workspace.get("id") if isinstance(workspace, dict) else None
        if welcome.get("t") != "welcome" or welcome.get("host_uid") != self.host_uid or actual != self.workspace_id:
            raise PinMismatch("Server welcome does not match the trusted pin")


def connect_pinned(pin: Pin, *, timeout: float = 2) -> socket.socket:
    """Return a connected socket only after checking UID; sends no bytes.

    Send hello next, then call pin.check_welcome BEFORE sending any request or
    secret. The workspace claim necessarily arrives after hello, never before it.
    """
    check_socket_path(pin.socket_path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(pin.socket_path))
        if peer_credentials(sock).uid != pin.host_uid:
            raise PinMismatch("Server UID does not match the trusted pin")
        return sock
    except BaseException:
        sock.close()
        raise


def handshake_pinned(pin: Pin, hello: dict[str, Any], *, timeout: float = 2) -> tuple[socket.socket, dict[str, Any]]:
    """Verify the complete pin before returning a socket usable for requests.

    Only a validated, credential-free hello is sent before the workspace check.
    The deadline covers the whole welcome, including a trickled length prefix.
    """
    frames.validate_envelope(hello, sender="client")
    if hello["t"] != "hello":
        raise frames.FrameError("handshake.invalid")
    sock = connect_pinned(pin, timeout=timeout)
    try:
        deadline = monotonic() + timeout
        sock.sendall(frames.encode(hello))
        decoder = frames.Decoder()
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Welcome deadline exceeded")
            sock.settimeout(remaining)
            data = recv(sock)
            if not data:
                decoder.finish()
                raise ConnectionError("Server closed before welcome")
            received = decoder.feed(data)
            if received:
                welcome = received[0]
                if isinstance(welcome, frames.FrameError):
                    raise welcome
                if len(received) != 1 or decoder.pending:
                    raise frames.FrameError(message="Unexpected frame before any request")
                if welcome.get("t") == "fatal" and isinstance(welcome.get("error"), dict):
                    raise Refused(str(welcome["error"].get("code")))
                pin.check_welcome(welcome)
                if welcome["proto"] not in hello["proto"]:
                    raise frames.FrameError("host.version_mismatch")
                sock.settimeout(timeout)
                return sock, welcome
    except BaseException:
        sock.close()
        raise


class Connection:
    """A blocking client of one workspace socket: the pinned handshake, then one request at a time.

    the CLI signer (H5b) and the thin CLI (H4b, :mod:`orch.hostclient`) share it. ``call`` sends one ``req`` and returns
    its ``result``, raising the host's error as :class:`~orch.host.errors.HostError`. ``auth`` carries the grant and the
    session (never in ``args``), ``dry_run`` goes in ``opts``, and ``timeout`` is the seconds this one
    call waits for its answer on the socket (a ``wait`` outlasts the default). Frames that are not the answer
    (``evt`` items of a subscription) are skipped: nothing in the CLI subscribes, ``wait`` is one request the host
    parks on its bus. After a timeout or a broken connection the object is unusable: open another.
    """

    def __init__(self, pin: Pin, *, client: str = "orch", timeout: float = 120) -> None:
        hello = {"t": "hello", "proto": [1], "client": client, "caps": [], "ops": dict(CLIENT_OPS)}
        self.sock, self.welcome = handshake_pinned(pin, hello)
        self.timeout = timeout
        self.sock.settimeout(timeout)
        self._decoder = frames.Decoder()
        self._frames: list[Any] = []
        self._n = 0

    def call(
        self,
        op: str,
        args: dict[str, Any],
        *,
        idem: str | None = None,
        files: dict[str, Any] | None = None,
        auth: dict[str, str] | None = None,
        dry_run: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """The ``result`` of the request. Raises ``HostError`` for the host's refusal; ``TimeoutError``,
        ``ConnectionError`` or ``FrameError`` when no answer arrives."""
        from .errors import HostError

        self._n += 1
        rid = f"c{self._n}"
        frame: dict[str, Any] = {"t": "req", "id": rid, "op": op, "args": args}
        if auth:
            frame["auth"] = auth
        opts = {k: v for k, v in (("idem", idem), ("dry_run", True if dry_run else None)) if v}
        if opts:
            frame["opts"] = opts
        if files:
            frame["files"] = files
        self.sock.settimeout(self.timeout if timeout is None else timeout)
        self.sock.sendall(frames.encode(frame))
        while True:
            while not self._frames:
                data = recv(self.sock)
                if not data:
                    raise ConnectionError("the host closed the connection")
                self._frames.extend(self._decoder.feed(data))
            got = self._frames.pop(0)
            if isinstance(got, frames.FrameError):
                raise got
            if got.get("t") == "fatal":
                raise HostError.stored(got.get("error") or {"code": "internal"})
            if got.get("t") != "res" or got.get("id") != rid:
                continue
            if got.get("ok"):
                return got["result"]
            raise HostError.stored(got.get("error") or {"code": "internal"})

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

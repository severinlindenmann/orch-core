"""A small blocking client for the control socket (the person's own host, same UID): one request at a time.

H4b builds the full thin CLI (``orch.hostclient``); this is what ``orch host list|register|unregister|restart|outcome``
and the tests use.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import frames
from ._socket import recv
from .client import Pin, handshake_pinned

__all__ = ["ControlClient", "new_idem"]


def new_idem() -> str:
    from orch.identity import new_ulid

    return "a_" + new_ulid()


class ControlClient:
    def __init__(self, path: Path, *, host_uid: int | None = None, timeout: float = 5.0, client: str = "orch") -> None:
        pin = Pin(os.getuid() if host_uid is None else host_uid, None, Path(path))
        hello = {"t": "hello", "proto": [1], "client": client, "caps": ["control/1", "outcome/1"]}
        self.sock, self.welcome = handshake_pinned(pin, hello, timeout=2)
        self.timeout = timeout
        self.decoder = frames.Decoder()
        self.pending: list[Any] = []
        self.counter = 0

    def __enter__(self) -> ControlClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.sock.close()

    def send(self, op: str, args: dict[str, Any] | None = None, *, idem: str | None = None, **extra: Any) -> str:
        self.counter += 1
        request_id = f"c{self.counter}"
        frame: dict[str, Any] = {"t": "req", "id": request_id, "op": op, "args": args or {}, **extra}
        if idem is not None:
            frame["opts"] = {**frame.get("opts", {}), "idem": idem}
        self.sock.sendall(frames.encode(frame))
        return request_id

    def read(self, timeout: float | None = None) -> dict[str, Any]:
        self.sock.settimeout(self.timeout if timeout is None else timeout)
        while not self.pending:
            data = recv(self.sock)
            if not data:
                raise ConnectionError("the host closed the control connection")
            self.pending.extend(self.decoder.feed(data))
        frame = self.pending.pop(0)
        if isinstance(frame, frames.FrameError):
            raise frame
        return frame

    def call(self, op: str, args: dict[str, Any] | None = None, *, idem: str | None = None, **extra: Any) -> dict:
        request_id = self.send(op, args, idem=idem, **extra)
        while True:
            frame = self.read()
            if frame.get("id") == request_id or frame.get("t") == "fatal":
                return frame

"""The dashboard's side of the click channel (orch-v2-host §0.8.6 item 5). Standard library and the frame codec only:
it never imports custody code and never sees a key."""

from __future__ import annotations

import socket
import subprocess
from typing import Any

from orch.host import frames

from .protocol import OPS, ClickError

__all__ = ["ClickClient"]


class ClickClient:
    def __init__(self, sock: socket.socket, proc: subprocess.Popen[bytes] | None = None) -> None:
        self.sock, self.proc = sock, proc
        self._decoder = frames.Decoder()
        self._frames: list[Any] = []
        self._n = 0

    def call(self, op: str, args: dict[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
        if op not in OPS:
            raise ValueError(op)
        self._n += 1
        rid = f"k{self._n}"
        self.sock.settimeout(timeout)
        self.sock.sendall(frames.encode({"t": "req", "id": rid, "op": op, "args": args}))
        while True:
            while not self._frames:
                data = self.sock.recv(65536)
                if not data:
                    raise ConnectionError("the click signer closed the channel")
                self._frames.extend(self._decoder.feed(data))
            got = self._frames.pop(0)
            if isinstance(got, frames.FrameError):
                raise got
            if got.get("id") != rid:
                continue
            if got.get("ok"):
                return got["result"]
            raise ClickError.of(got.get("error") or {})

    def close(self, *, wait: float = 2.0) -> None:
        """Close the channel (the signer exits on EOF), wait, then kill that child only."""
        try:
            self.sock.close()
        finally:
            if self.proc is not None:
                try:
                    self.proc.wait(wait)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(wait)

    def __enter__(self) -> ClickClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

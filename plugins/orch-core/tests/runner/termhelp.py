"""Helpers for the term socket tests: a fake supervisor (no processes), a framed client, capability builders."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from orch.host import frames
from orch.runner.ring import Ring
from orch.runner.supervise import SpawnError
from orch.runner.term import TermListener

RUN = "r_TERMTESTRUN0000000000000A"
GEN = "a" * 32


class FakeLive:
    def __init__(self, ring_size: int = 1 << 20) -> None:
        self.ring = Ring(ring_size)
        self.exit: dict[str, Any] | None = None
        self.pty_closed = False
        self.cols, self.rows = 120, 40


class FakeSup:
    """What the term socket needs from the supervisor, without a PTY."""

    def __init__(self, ring_size: int = 1 << 20) -> None:
        self.live: dict[str, FakeLive] = {RUN: FakeLive(ring_size)}
        self.on_output = None
        self.written: list[bytes] = []
        self.sizes: list[tuple[int, int]] = []
        self.nudges = 0
        #: at most this many bytes per write (a partial write); 0 in ``stall`` means "queue full"
        self.max_write: int | None = None
        self.stall = 0
        self.before_write = None

    def live_of(self, run: str) -> FakeLive | None:
        return self.live.get(run)

    def attach_info(self, run: str) -> dict[str, Any] | None:
        live = self.live.get(run)
        if live is None:
            return None
        return {"start": live.ring.start, "offset": live.ring.end, "cols": live.cols, "rows": live.rows,
                "state": "running" if live.exit is None else "exited"}  # fmt: skip

    def write_input(self, run: str, data: bytes) -> int:
        live = self.live.get(run)
        if live is None or live.exit is not None:
            raise SpawnError("not_found", "gone")
        if self.before_write is not None:
            self.before_write()
        if self.stall > 0:
            self.stall -= 1
            return 0
        n = len(data) if self.max_write is None else min(len(data), self.max_write)
        self.written.append(data[:n])
        return n

    def resize(self, run: str, cols: int, rows: int) -> None:
        live = self.live[run]
        live.cols, live.rows = cols, rows
        self.sizes.append((cols, rows))

    def nudge(self, run: str) -> None:
        self.nudges += 1

    def emit(self, data: bytes, run: str = RUN) -> None:
        self.live[run].ring.write(data)
        if self.on_output is not None:
            self.on_output(run)

    def exit_run(self, code: int = 0, run: str = RUN) -> None:
        live = self.live[run]
        live.exit = {"code": code, "signal": None}
        live.pty_closed = True
        if self.on_output is not None:
            self.on_output(run)

    @property
    def typed(self) -> bytes:
        return b"".join(self.written)


class Rig:
    """A listener on a short socket path with a fake supervisor and a host epoch."""

    def __init__(self, ring_size: int = 1 << 20) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="tt", dir="/tmp"))
        self.sup = FakeSup(ring_size)
        self.listener = TermListener(self.dir / "term" / str(os.getuid()) / "term.sock", os.getuid(), gen=GEN,
                                     sup=self.sup)  # fmt: skip
        self.listener.open()
        self.listener.host_ready(1)
        self.clients: list[Client] = []
        self.changes: list[str] = []
        self.listener.on_change = self.changes.append

    def allow(
        self,
        att: str = "att1",
        *,
        mode: str = "read",
        take: bool = False,
        person: str = "p_alice",
        run: str = RUN,
        start_from: int | None = None,
        epoch: int | None = None,
    ) -> tuple[str, str]:
        ticket = secrets.token_urlsafe(32)
        nonce = secrets.token_hex(16)
        exp = (datetime.now(UTC) + timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.listener.allow(
            {"att": att, "ticket_hash": hashlib.sha256(ticket.encode()).hexdigest(), "workspace_id": "w_x",
             "person": person, "run": run, "session": None, "mode": mode, "take": take, "lease_gen": 0,
             "runner_gen": GEN, "from": start_from, "nonce": nonce, "expires_at": exp},
            epoch=epoch,
        )  # fmt: skip
        return ticket, nonce

    def attach(
        self,
        att: str = "att1",
        *,
        mode: str = "read",
        take: bool = False,
        person: str = "p_alice",
        start_from: int | None = None,
        read_ready: bool = True,
    ) -> Client:
        ticket, nonce = self.allow(att, mode=mode, take=take, person=person, start_from=start_from)
        c = Client(self.listener.path)
        self.clients.append(c)
        hello = c.recv()
        assert hello["t"] == "hello"
        c.send({"t": "attach", "ticket": ticket, "nonce": nonce})
        if read_ready:
            c.ready = c.recv()
        return c

    def close(self) -> None:
        for c in self.clients:
            c.close()
        self.listener.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class Client:
    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.decoder = frames.Decoder()
        self.buf: list[Any] = []
        self.ready: dict[str, Any] = {}

    def send(self, frame: dict[str, Any]) -> None:
        try:
            self.sock.sendall(frames.encode(frame))
        except OSError:  # the runner already closed: that is what several tests expect
            pass

    def recv(self, timeout: float = 5.0) -> dict[str, Any] | None:
        """The next frame, or ``None`` at end of stream."""
        deadline = time.monotonic() + timeout
        while not self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError("no frame in time")
            self.sock.settimeout(left)
            try:
                data = self.sock.recv(1 << 20)
            except OSError:
                return None
            if not data:
                return None
            self.buf.extend(self.decoder.feed(data))
        return self.buf.pop(0)

    def until(self, kind: str, timeout: float = 5.0) -> dict[str, Any]:
        """Frames up to and including the first of type ``kind``; asserts it arrives."""
        deadline = time.monotonic() + timeout
        while True:
            frame = self.recv(max(0.05, deadline - time.monotonic()))
            assert frame is not None, f"stream ended before {kind}"
            if frame.get("t") == kind:
                return frame

    def drain(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Every frame until the stream ends."""
        got = []
        deadline = time.monotonic() + timeout
        while True:
            frame = self.recv(max(0.05, deadline - time.monotonic()))
            if frame is None:
                return got
            got.append(frame)

    def collect(self, quiet: float = 0.3) -> list[dict[str, Any]]:
        """Frames until nothing arrives for ``quiet`` seconds or the stream ends."""
        got = []
        while True:
            try:
                frame = self.recv(quiet)
            except AssertionError:
                return got
            if frame is None:
                return got
            got.append(frame)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def out_bytes(items: list[dict[str, Any]]) -> bytes:
    return b"".join(base64.b64decode(f["b64"]) for f in items if f.get("t") == "out")


def wait_until(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def stream_closed(c: Client, timeout: float = 3.0) -> list[dict[str, Any]]:
    return c.drain(timeout)


__all__ = ["RUN", "Client", "Rig", "FakeSup", "b64", "out_bytes", "stream_closed", "wait_until", "threading"]

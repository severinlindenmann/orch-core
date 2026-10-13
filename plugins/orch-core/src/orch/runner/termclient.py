"""A small client of the runner's term socket (§12.6.1, §12.6.3), for the dashboard server's stream and for tests.

:meth:`TermClient.connect` does the verification of §12.6.1 before it sends a ticket: the socket path is computed from
the client's own run directory and ``runner.uid`` (the answer's ``socket`` must equal it), the directory
``<run>/term/<uid>/`` is owned by the runner UID with no write for group or other, the peer UID and PID after
``connect`` are the runner's, and the first frame is ``{t:"hello", gen, pid}`` with the answer's ``gen``. Any mismatch
raises :class:`RunnerChanged` (the page says "the runner changed; try again") before a ticket leaves this process.

The client owns ``input_id`` and ``lease_gen`` (§12.6.3): input is split into frames of at most 64 KiB, numbered per
attachment, and ``lease_gen`` follows the ``ready`` and ``writer`` frames. A ``throttle`` makes :meth:`pump` resend the
same frame with the same ``input_id`` after ``retry_ms``; input that was never acknowledged when the stream ends is
reported by :attr:`lost` (it is never replayed on a new attachment). Terminal bytes are never logged here.
"""

from __future__ import annotations

import base64
import os
import socket
import stat
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from orch.host import frames
from orch.host._socket import peer_credentials

__all__ = ["CHUNK", "RunnerChanged", "TermClient"]

CHUNK = 64 * 1024


class RunnerChanged(Exception):
    """The runner is not the one the host named (§12.6.1): nothing was sent."""


class TermClient:
    def __init__(self, run_dir: Path | str, answer: dict[str, Any]) -> None:
        """``answer``: the ``result.data`` of ``term.attach``."""
        self.run_dir = Path(run_dir)
        self.answer = answer
        self.sock: socket.socket | None = None
        self.decoder = frames.Decoder()
        self.buf: list[Any] = []
        self.ready: dict[str, Any] = {}
        self.writer = False
        self.lease_gen = int(answer.get("lease_gen") or 0)
        self.next_id = 1
        #: sent and not yet acknowledged, by input_id (insertion order)
        self.unacked: OrderedDict[int, bytes] = OrderedDict()
        self._resend: list[tuple[float, int]] = []
        self.ended: str | None = None

    # -- connect and verify
    def connect(self, timeout: float = 5.0) -> dict[str, Any]:
        runner = self.answer["runner"]
        uid = int(runner["uid"])
        expected = self.run_dir / "term" / str(uid) / "term.sock"
        if str(self.answer.get("socket")) != str(expected):
            raise RunnerChanged("the socket path is not the one this server computes")
        try:
            info = os.lstat(expected.parent)
        except OSError:
            raise RunnerChanged("the runner's terminal directory is gone") from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022:
            raise RunnerChanged("the runner's terminal directory is not owned by the runner or is writable")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(str(expected))
            creds = peer_credentials(sock)
            if creds.uid != uid or creds.pid != int(runner["pid"]):
                raise RunnerChanged("the peer of the terminal socket is not the runner")
            self.sock = sock
            hello = self.recv(timeout)
            if (
                not hello
                or hello.get("t") != "hello"
                or hello.get("gen") != runner["gen"]
                or (hello.get("pid") != runner["pid"])
            ):
                raise RunnerChanged("the runner's hello does not match")
        except RunnerChanged:
            sock.close()
            self.sock = None
            raise
        except OSError:
            sock.close()
            self.sock = None
            raise RunnerChanged("the runner's terminal socket cannot be reached") from None
        self.send({"t": "attach", "ticket": self.answer["ticket"], "nonce": self.answer["nonce"]})
        first = self.recv(timeout)
        if first is None:
            raise RunnerChanged("the terminal socket closed")
        if first.get("t") == "ready":
            self.ready = first
            self.handle(first)
        else:
            self.handle(first)
        return first

    # -- frames
    def send(self, frame: dict[str, Any]) -> None:
        assert self.sock is not None
        self.sock.sendall(frames.encode(frame))

    def recv(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The next frame (state updated), ``None`` at the end of the stream; ``TimeoutError`` when none arrives."""
        assert self.sock is not None
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.buf:
            if deadline is not None and deadline - time.monotonic() <= 0:
                raise TimeoutError("no frame")
            self.sock.settimeout(None if deadline is None else max(0.01, deadline - time.monotonic()))
            try:
                data = self.sock.recv(1 << 20)
            except TimeoutError:
                raise
            except OSError:
                return None
            if not data:
                return None
            self.buf.extend(self.decoder.feed(data))
        frame = self.buf.pop(0)
        if isinstance(frame, frames.FrameError):
            return None
        self.handle(frame)
        return frame

    def handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("t")
        if kind == "ready":
            self.writer, self.lease_gen = bool(frame["writer"]), int(frame["lease_gen"])
        elif kind == "writer":
            self.writer = bool(frame["writer"])
            self.lease_gen = int(frame["lease_gen"])
        elif kind == "ack":
            self.unacked.pop(int(frame["input_id"]), None)
        elif kind == "throttle":
            at = time.monotonic() + int(frame["retry_ms"]) / 1000
            self._resend = [(at, iid) for iid in self.unacked]  # the unacked frames, same ids, in order
        elif kind == "end":
            self.ended = str(frame.get("reason"))

    # -- input and resize (only with the write lease)
    def send_input(self, data: bytes) -> list[int]:
        """Number, split and send ``data``; the ids to wait for (``ack``). Nothing is sent without the lease."""
        if not self.writer:
            return []
        ids = []
        for at in range(0, len(data), CHUNK):
            chunk = data[at : at + CHUNK]
            iid = self.next_id
            self.next_id += 1
            self.unacked[iid] = chunk
            self._send_in(iid, chunk)
            ids.append(iid)
        return ids

    def _send_in(self, iid: int, chunk: bytes) -> None:
        self.send({"t": "in", "input_id": iid, "lease_gen": self.lease_gen, "b64": base64.b64encode(chunk).decode()})

    def pump(self) -> None:
        """Resend what a ``throttle`` paused, once its time has come (call it between reads)."""
        now = time.monotonic()
        due = [iid for at, iid in self._resend if at <= now and iid in self.unacked]
        self._resend = [(at, iid) for at, iid in self._resend if at > now and iid in self.unacked]
        for iid in due:
            self._send_in(iid, self.unacked[iid])

    def resize(self, cols: int, rows: int) -> None:
        if self.writer:
            self.send({"t": "resize", "lease_gen": self.lease_gen, "cols": cols, "rows": rows})

    @property
    def lost(self) -> list[int]:
        """Input ids that were never acknowledged: report ``input_lost``; never replay them on a new attachment."""
        return list(self.unacked)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

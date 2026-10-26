"""A small client of the runner's term socket (§12.6.1, §12.6.3), for the dashboard server's stream and for tests.

:meth:`TermClient.connect` does the verification of §12.6.1 before it sends a ticket: the socket path is computed from
the client's own run directory (tier B: the
agent's, from the root-owned pin, §13.3) and ``runner.uid`` (the answer's ``socket`` must equal it), the directory
``<run>/term/<uid>/`` is owned by the runner UID with no write for group or other, the peer UID and PID after
``connect`` are the runner's, and the first frame is ``{t:"hello", gen, pid}`` with the answer's ``gen``. Any mismatch
raises :class:`RunnerChanged` (the page says "the runner changed; try again") before a ticket leaves this process.

The client owns ``input_id`` and ``lease_gen`` (§12.6.3): input is split into frames of at most 64 KiB, numbered per
attachment, and ``lease_gen`` follows the ``ready`` and ``writer`` frames. **One input frame is in flight at a time**:
the next is sent only after the ``ack`` of the one before, so frames reach the PTY in order and none can be skipped. A
``throttle`` makes :meth:`pump` resend the same frame with the same ``input_id`` after ``retry_ms``; input that was
never acknowledged when the stream ends is reported by :attr:`lost` (it is never replayed on a new attachment). A
runner frame of the wrong shape ends the stream with ``protocol`` (its content is never put into an exception); a
stream that closes without an ``end`` frame ends with ``closed``. ``send``/``resize`` after the end raise
:class:`TermEnded`. Terminal bytes are never logged here.

**Writes have their own deadline.** :meth:`send` writes non-blocking with its own bound (``send_timeout``, default
5 s for the whole frame), independent of :meth:`recv`'s timeout: an ``ack``
read by ``recv(0.05)`` puts the next input frame on the wire from inside ``recv``, and a slow runner must not end a
multi-frame paste. A runner that takes no byte for that long ends the stream with ``send_timeout`` (the socket is
closed: a frame cut short can never be completed); a write error inside ``recv`` ends nothing by itself (the runner's
``end`` frame, if any, is still read) and ``recv`` returns the frame it read.
"""

from __future__ import annotations

import base64
import contextlib
import os
import selectors
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
#: :meth:`TermClient.send`'s write deadline (seconds), independent of ``recv``'s timeout
SEND_TIMEOUT = 5.0


class RunnerChanged(Exception):
    """The runner is not the one the host named (§12.6.1): nothing was sent."""


class TermEnded(ConnectionError):
    """``send``/``resize`` on a stream that already ended (``ended`` says why: an ``end`` reason, ``protocol`` or
    ``closed``)."""


class TermClient:
    def __init__(
        self,
        run_dir: Path | str,
        answer: dict[str, Any],
        *,
        send_timeout: float = SEND_TIMEOUT,
        pins: Any = None,
        host_uid: int | None = None,
    ) -> None:
        """``answer``: the ``result.data`` of ``term.attach``; ``send_timeout``: the write deadline of :meth:`send`.
        ``pins`` (an :class:`orch.host.pins.AgentPins`) and ``host_uid`` (this process's UID) are for tests."""
        self.pins = pins
        self.host_uid = os.getuid() if host_uid is None else host_uid
        self.run_dir = Path(run_dir)
        self.send_timeout = send_timeout
        self.answer = answer
        self.sock: socket.socket | None = None
        self.decoder = frames.Decoder()
        self.buf: list[Any] = []
        self.ready: dict[str, Any] = {}
        self.writer = False
        self.lease_gen = int(answer.get("lease_gen") or 0)
        self.next_id = 1
        #: not yet acknowledged, by input_id (in order; only the first is on the wire)
        self.unacked: OrderedDict[int, bytes] = OrderedDict()
        #: the input id on the wire (sent, neither acknowledged nor throttled), else ``None``
        self.inflight: int | None = None
        #: when a throttled frame is due again (``None``: nothing is waiting for a resend)
        self._resend_at: float | None = None
        self.ended: str | None = None

    # -- connect and verify
    def connect(self, timeout: float = 5.0) -> dict[str, Any]:
        runner = self.answer["runner"]
        uid = int(runner["uid"])
        base, tier_b = self._term_base(uid)
        expected = base / "term" / str(uid) / "term.sock"
        if str(self.answer.get("socket")) != str(expected):
            raise RunnerChanged("the socket path is not the one this server computes")
        try:
            info = os.lstat(expected.parent)
        except OSError:
            raise RunnerChanged("the runner's terminal directory is gone") from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022:
            raise RunnerChanged("the runner's terminal directory is not owned by the runner or is writable")
        if tier_b and stat.S_IMODE(info.st_mode) & 0o067:  # the person's ACL mask is ``--x`` and nothing else shows
            raise RunnerChanged("the runner's terminal directory is open to more than the person")
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
        try:
            self.send({"t": "attach", "ticket": self.answer["ticket"], "nonce": self.answer["nonce"]})
        except TermEnded:
            raise RunnerChanged("the terminal socket closed") from None
        first = self.recv(timeout)
        if first is None:
            raise RunnerChanged("the terminal socket closed")
        if first.get("t") == "ready":
            self.ready = first
        return first

    def _term_base(self, uid: int) -> tuple[Path, bool]:
        """Where the runner ``uid``'s ``term/`` directory is, and whether this is tier B. Tier A: this client's own run
        directory. Tier B (spec 13.3, 13.5): the runner's UID has a root-owned agent pin naming THIS process's UID as
        its host, and the directory is ``/run/orch/agents/<its user name>``, named by the system's user database, never
        by the host's answer. A pinned UID that is not this host's agent, or a name that is not ``orch-agent-*``,
        fails closed."""
        from orch.host.pins import AgentPins

        doc = (self.pins if self.pins is not None else AgentPins()).get(uid)
        if doc is None:
            return self.run_dir, False
        if doc.get("class") != "agent" or doc.get("host_uid") != self.host_uid or uid == self.host_uid:
            raise RunnerChanged("the runner is a pinned user that is not this host's agent")
        import pwd

        from orch.setup.osusers import AGENT_PREFIX, AGENT_RUN_ROOT

        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            raise RunnerChanged("the runner's user is unknown") from None
        if not name.startswith(AGENT_PREFIX):
            raise RunnerChanged("the runner's user is not an agent user")
        return Path(AGENT_RUN_ROOT) / name, True

    # -- frames
    def send(self, frame: dict[str, Any]) -> None:
        """Write one frame within ``send_timeout`` (non-blocking, whatever timeout ``recv`` left on the socket)."""
        sock = self.sock
        if sock is None:
            raise TermEnded(f"the terminal stream ended ({self.ended or 'closed'})")
        data = memoryview(frames.encode(frame))
        deadline = time.monotonic() + self.send_timeout
        previous = sock.gettimeout()
        sock.setblocking(False)
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(sock, selectors.EVENT_WRITE)
                while data:
                    try:
                        data = data[sock.send(data) :]
                        continue
                    except (BlockingIOError, InterruptedError):
                        pass
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not sel.select(remaining):
                        if deadline - time.monotonic() > 0:
                            continue  # woken early (a signal): the deadline has not passed
                        self._end_locally("send_timeout")
                        raise TermEnded("the runner took no input in time (send_timeout)")
        except OSError as e:  # the runner already shut the stream (e.g. the attachment was ended): its end frame, if
            # any, is still readable, so the socket stays open for recv
            raise TermEnded(f"the terminal stream ended ({self.ended or 'closed'})") from e
        finally:
            if self.sock is sock:
                sock.settimeout(previous)

    def _end_locally(self, reason: str) -> None:
        """End the stream on this side (``reason``) and close the socket: nothing more can be read or written."""
        self.ended = self.ended or reason
        self.buf.clear()
        sock, self.sock = self.sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def recv(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The next frame (state updated), ``None`` at the end of the stream; ``TimeoutError`` when none arrives. A
        stream that closes without an ``end`` frame (the runner shut it, e.g. after a revocation cut an output frame
        short, which cannot be completed without more of the revoked output) ends with ``closed``."""
        if self.sock is None:
            self.ended = self.ended or "closed"
            return None
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
                self.ended = self.ended or "closed"
                return None
            if not data:
                self.ended = self.ended or "closed"
                return None
            self.buf.extend(self.decoder.feed(data))
        frame = self.buf.pop(0)
        if isinstance(frame, frames.FrameError) or not _well_formed(frame):
            self._protocol()
            return None
        self.handle(frame)
        return frame

    def _protocol(self) -> None:
        """The runner sent something this client does not understand: the stream ends with ``protocol``."""
        self._end_locally("protocol")

    def handle(self, frame: dict[str, Any]) -> None:
        """Update the state from one well-formed frame (:func:`_well_formed`)."""
        kind = frame.get("t")
        if kind in ("ready", "writer"):
            self.writer, self.lease_gen = frame["writer"], frame["lease_gen"]
        elif kind == "ack":
            iid = frame["input_id"]
            self.unacked.pop(iid, None)
            if iid == self.inflight:
                self.inflight = None
                with contextlib.suppress(TermEnded):  # inside recv: the end is in ``ended``, the frame is returned
                    self._send_next()
        elif kind == "throttle":
            if self.inflight is not None:  # the same frame again, same id, once its time has come
                self._resend_at = time.monotonic() + frame["retry_ms"] / 1000
                self.inflight = None
        elif kind == "end":
            self.ended = frame["reason"]

    # -- input and resize (only with the write lease)
    def send_input(self, data: bytes) -> list[int]:
        """Number and split ``data`` and queue it; the ids to wait for (``ack``). Frames go out one at a time (the
        next after the previous one's ``ack``). Nothing is queued without the lease."""
        if not self.writer:
            return []
        ids = []
        for at in range(0, len(data), CHUNK):
            iid = self.next_id
            self.next_id += 1
            self.unacked[iid] = data[at : at + CHUNK]
            ids.append(iid)
        self._send_next()
        return ids

    def _send_next(self) -> None:
        """Put the oldest unacknowledged frame on the wire when none is in flight and no resend is pending."""
        if self.inflight is not None or self._resend_at is not None or not self.unacked or self.sock is None:
            return
        iid, chunk = next(iter(self.unacked.items()))
        self.inflight = iid
        self.send({"t": "in", "input_id": iid, "lease_gen": self.lease_gen, "b64": base64.b64encode(chunk).decode()})

    def pump(self) -> None:
        """Resend the frame a ``throttle`` paused, once its time has come (call it between reads)."""
        if self._resend_at is not None and time.monotonic() >= self._resend_at:
            self._resend_at = None
            self._send_next()

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


def _int(value: Any, low: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= low


#: the fields this client reads, per frame type, and their checks; other types are passed through unread
_SHAPES: dict[str, dict[str, Any]] = {
    "hello": {"gen": lambda v: isinstance(v, str), "pid": _int},
    "ready": {"writer": lambda v: isinstance(v, bool), "lease_gen": _int},
    "writer": {"writer": lambda v: isinstance(v, bool), "lease_gen": _int},
    "ack": {"input_id": lambda v: _int(v, 1)},
    "throttle": {"retry_ms": _int},
    "end": {"reason": lambda v: isinstance(v, str) and 0 < len(v) <= 64},
    "out": {"off": _int, "b64": lambda v: isinstance(v, str)},
    "gap": {"from": _int, "to": _int},
    "resize": {"cols": _int, "rows": _int},
}


def _well_formed(frame: Any) -> bool:
    if not isinstance(frame, dict) or not isinstance(frame.get("t"), str):
        return False
    checks = _SHAPES.get(frame["t"], {})
    return all(check(frame.get(key)) for key, check in checks.items())

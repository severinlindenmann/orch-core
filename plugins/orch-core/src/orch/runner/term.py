"""The runner's terminal socket (D-P2-25): ``<run>/term-<uid>/term.sock``, where the dashboard server attaches.

The host decides every attach and registers it here with ``attach.allow {att, ticket_hash, run, person, mode,
lease_gen, expires}`` over its command stream; the dashboard server then connects to this socket and sends the ticket
once, as its first line (``{"ticket": "…"}``, at most 1 KiB, within 5 s). The runner consumes it atomically: a ticket
works once, only until it expires (at most 10 s after the allow arrived, so an unused ticket never holds anything),
and only while the host connection that issued it is live. ``attach.end`` closes an attachment; losing the host ends
every attachment (``host.restarting``) and refuses new tickets until the next ``runner.hello`` is answered (a new
authorization epoch, D-P2-39).

The directory is the runner UID's own (0700, never a symlink), the socket 0600, and every connection's peer UID must be
the runner's UID. The byte path itself (output frames, input ids, the write lease) is H11b.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import socket
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch.host._socket import peer_credentials

__all__ = ["TermListener", "TicketError"]

LOG = logging.getLogger(__name__)
TICKET_WAIT = 5.0
ALLOW_SECONDS = 10.0
ATTEMPTS_PER_MINUTE = 10
LINE_MAX = 1024


class TicketError(Exception):
    pass


@dataclass
class Allow:
    att: str
    ticket_hash: str
    run: str
    person: str
    mode: str
    lease_gen: int
    expires: float
    epoch: int


@dataclass
class Attachment:
    att: str
    run: str
    person: str
    mode: str
    lease_gen: int
    conn: socket.socket


class TermListener:
    def __init__(self, path: Path, uid: int, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.uid = uid
        self.clock = clock
        self.lock = threading.Lock()
        self.allows: dict[str, Allow] = {}  # by ticket hash
        self.attachments: dict[str, Attachment] = {}
        self.accepting = False
        self.epoch = 0
        self.attempts: deque[float] = deque()
        self.sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closing = False

    # -- the socket
    def open(self) -> None:
        directory = self.path.parent
        with contextlib.suppress(FileExistsError):
            os.mkdir(directory, 0o700)
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.uid or stat.S_IMODE(info.st_mode) & 0o077:
            raise OSError(errno.EPERM, f"{directory}: expected a 0700 directory of UID {self.uid}")
        try:
            old = os.lstat(self.path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(old.st_mode) or old.st_uid != self.uid:
                raise OSError(errno.EEXIST, f"{self.path}: not our socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect(str(self.path))
                except OSError:
                    os.unlink(self.path)
                else:
                    raise OSError(errno.EADDRINUSE, f"{self.path} is already served")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            sock.bind(str(self.path))
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)
        sock.listen(16)
        self.sock = sock
        self._thread = threading.Thread(target=self._accept_loop, name="orch-runner-term", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closing = True
        self.host_lost("closed")
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
            self.sock = None
        with contextlib.suppress(OSError):
            os.unlink(self.path)
        if self._thread is not None:
            self._thread.join(timeout=2)

    # -- the host's side
    def host_ready(self, epoch: int) -> None:
        with self.lock:
            self.epoch = epoch
            self.accepting = True

    def host_lost(self, reason: str = "host.restarting") -> None:
        with self.lock:
            self.accepting = False
            self.allows.clear()
            ended = list(self.attachments.values())
            self.attachments.clear()
        for att in ended:
            _end(att.conn, reason)

    def allow(self, args: dict[str, Any]) -> None:
        now = self.clock()
        try:
            item = Allow(
                str(args["att"]), str(args["ticket_hash"]), str(args["run"]), str(args["person"]), str(args["mode"]),
                int(args.get("lease_gen") or 0), min(float(args["expires"]), now + ALLOW_SECONDS), self.epoch,
            )  # fmt: skip
        except (KeyError, TypeError, ValueError):
            raise TicketError("invalid attach.allow") from None
        if item.mode not in ("read", "write") or len(item.ticket_hash) != 64:
            raise TicketError("invalid attach.allow")
        with self.lock:
            if not self.accepting:
                raise TicketError("no host epoch")
            self._expire(now)
            self.allows[item.ticket_hash] = item

    def end(self, att: str, reason: str) -> bool:
        with self.lock:
            found = self.attachments.pop(att, None)
            for key in [k for k, a in self.allows.items() if a.att == att]:
                del self.allows[key]
        if found is not None:
            _end(found.conn, reason)
        return found is not None

    def _expire(self, now: float) -> None:
        for key in [k for k, a in self.allows.items() if a.expires <= now]:
            del self.allows[key]

    def consume(self, ticket: str) -> Allow | None:
        """The allow this ticket names, removed (single use); ``None`` for an unknown, expired or stale one."""
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        now = self.clock()
        with self.lock:
            self._expire(now)
            item = self.allows.pop(digest, None)
            if item is None or not self.accepting or item.epoch != self.epoch:
                return None
            return item

    def _rate_ok(self) -> bool:
        now = self.clock()
        with self.lock:
            while self.attempts and self.attempts[0] < now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= ATTEMPTS_PER_MINUTE:
                return False
            self.attempts.append(now)
            return True

    # -- connections
    def _accept_loop(self) -> None:
        while not self._closing and self.sock is not None:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                if self._closing:
                    return
                time.sleep(0.05)
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        keep = False
        try:
            if peer_credentials(conn).uid != self.uid:
                return
            if not self._rate_ok():
                _reply(conn, {"ok": False, "error": "rate.limited"})
                return
            line = _read_line(conn, TICKET_WAIT)
            try:
                doc = json.loads(line) if line is not None else None
            except ValueError:
                doc = None
            ticket = doc.get("ticket") if isinstance(doc, dict) else None
            item = self.consume(ticket) if isinstance(ticket, str) and 0 < len(ticket) <= 256 else None
            if item is None:
                _reply(conn, {"ok": False, "error": "term.ticket"})
                return
            att = Attachment(item.att, item.run, item.person, item.mode, item.lease_gen, conn)
            with self.lock:
                if not self.accepting or item.epoch != self.epoch:
                    _reply(conn, {"ok": False, "error": "term.ticket"})
                    return
                self.attachments[item.att] = att
            keep = True
            _reply(conn, {"ok": True, "att": item.att, "run": item.run, "mode": item.mode})
            conn.settimeout(None)
            while True:  # H11b streams here; until then the attachment stays open until either side ends it
                try:
                    data = conn.recv(4096)
                except OSError:
                    data = b""
                if not data:
                    break
        except OSError:
            pass
        finally:
            if keep:
                with self.lock:
                    current = self.attachments.get(att.att)
                    if current is att:
                        del self.attachments[att.att]
            with contextlib.suppress(OSError):
                conn.close()


def _read_line(conn: socket.socket, wait: float) -> bytes | None:
    deadline = time.monotonic() + wait
    buf = b""
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0 or len(buf) > LINE_MAX:
            return None
        conn.settimeout(left)
        try:
            part = conn.recv(LINE_MAX + 1 - len(buf))
        except (TimeoutError, OSError):
            return None
        if not part:
            return None
        buf += part
    line = buf.split(b"\n", 1)[0]
    return line if len(line) <= LINE_MAX else None


def _reply(conn: socket.socket, doc: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        conn.settimeout(2)
        conn.sendall(json.dumps(doc).encode() + b"\n")


def _end(conn: socket.socket, reason: str) -> None:
    _reply(conn, {"end": reason})
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)

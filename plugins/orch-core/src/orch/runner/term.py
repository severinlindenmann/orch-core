"""The runner's term socket (§12.6.1, §12.6.2): ``<run>/term/<uid>/term.sock``, where the dashboard server attaches.

The host decides every attach and registers it here with ``attach.allow`` over its command stream (§12.6.2); the
runner never sees a ticket, only its SHA-256. On every new connection (peer UID = the runner's UID) the runner sends
first ``{t:"hello", gen, pid}`` (the dashboard server checks both against what the host told it, §12.6.1 step 4); the
server answers with ``{t:"attach", ticket, nonce}`` within 5 s. Frames use §0.2's codec (4-byte length).

The capability is consumed atomically and only once: the hash must match a pending record whose ``runner_gen``,
``nonce`` and expiry hold (10 s unused at most, whatever the host's expiry says), in the current host epoch. A bad
ticket closes the connection with ``{t:"end", reason:"ticket_invalid"}`` and nothing else. Only presentations of a hash
the runner **has seen** (consumed or expired) count toward the lockout (10 per minute, then 60 s of refusals); an
unknown hash closes without counting, so random tickets cannot lock terminals out.

On success the runner answers ``{t:"ready", start, offset, cols, rows, writer, lease_gen, state}``. The write lease
(§12.6.2): ``take:false`` is refused with ``{t:"end", reason:"writer_busy"}`` when the lease is held or its generation
moved; ``take:true`` takes it; either way the generation increments and the old holder gets ``{t:"writer",
writer:false, lease_gen}``. Closing the holder's connection releases the lease. ``attach.end`` closes an attachment
with ``{t:"end", reason}``; losing the host ends every attachment (``host.restarting``) and refuses capabilities until
the next ``runner.hello`` is answered (a new authorization epoch). The byte path (``out``, ``in``, ``ack``) is H11b.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import socket
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from orch.host import frames
from orch.host._socket import peer_credentials

__all__ = ["TermListener", "TicketError"]

LOG = logging.getLogger(__name__)
ATTACH_WAIT = 5.0
ALLOW_SECONDS = 10.0
LOCKOUT_COUNT = 10
LOCKOUT_SECONDS = 60.0
SEEN_KEEP = 1024


class TicketError(Exception):
    pass


@dataclass
class Allow:
    att: str
    ticket_hash: str
    workspace_id: str
    person: str
    run: str
    session: str | None
    mode: str
    take: bool
    lease_gen: int
    runner_gen: str
    start_from: int | None
    nonce: str
    expires: float
    epoch: int


@dataclass
class Attachment:
    att: str
    run: str
    person: str
    mode: str
    conn: socket.socket
    writer: bool = False


def _parse_time(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("expires_at")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class TermListener:
    def __init__(
        self,
        path: Path,
        uid: int,
        *,
        gen: str = "0" * 32,
        info: Callable[[str], dict[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.uid = uid
        self.gen = gen
        self.info = info or (lambda run: {"start": 0, "offset": 0, "cols": 120, "rows": 40, "state": "running"})
        self.clock = clock
        self.lock = threading.Lock()
        self.allows: dict[str, Allow] = {}  # by ticket hash
        self.attachments: dict[str, Attachment] = {}
        self.leases: dict[str, tuple[str | None, int]] = {}  # run -> (holder att, lease generation)
        self.accepting = False
        self.epoch = 0
        self.seen: dict[str, float] = {}  # hashes of consumed or expired capabilities
        self.strikes: list[float] = []
        self.locked_until = 0.0
        self.sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closing = False

    # -- the socket
    def open(self) -> None:
        directory = self.path.parent
        with contextlib.suppress(FileExistsError):
            os.mkdir(directory.parent, 0o711)  # <run>/term: only traversed
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
        self.host_lost("runner.restarting")
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
            for item in self.allows.values():
                self._remember(item.ticket_hash)
            self.allows.clear()
            ended = list(self.attachments.values())
            self.attachments.clear()
            self.leases.clear()
        for att in ended:
            _end(att.conn, reason)

    def allow(self, args: dict[str, Any]) -> None:
        now = self.clock()
        try:
            item = Allow(
                str(args["att"]), str(args["ticket_hash"]), str(args["workspace_id"]), str(args["person"]),
                str(args["run"]), args.get("session"), str(args["mode"]), bool(args.get("take")),
                int(args.get("lease_gen") or 0), str(args["runner_gen"]), args.get("from"), str(args["nonce"]),
                min(_parse_time(args["expires_at"]), now + ALLOW_SECONDS), self.epoch,
            )  # fmt: skip
        except (KeyError, TypeError, ValueError):
            raise TicketError("invalid attach.allow") from None
        if item.mode not in ("read", "write") or len(item.ticket_hash) != 64 or len(item.nonce) != 32:
            raise TicketError("invalid attach.allow")
        if item.runner_gen != self.gen:
            raise TicketError("attach.allow for another runner generation")
        with self.lock:
            if not self.accepting:
                raise TicketError("no host epoch")
            self._expire(now)
            self.allows[item.ticket_hash] = item

    def end(self, att: str, reason: str) -> bool:
        with self.lock:
            found = self.attachments.pop(att, None)
            for key in [k for k, a in self.allows.items() if a.att == att]:
                self._remember(key)
                del self.allows[key]
            others = self._release(found) if found is not None else []
        if found is not None:
            _end(found.conn, reason)
        for conn, frame in others:
            _send(conn, frame)
        return found is not None

    def _remember(self, digest: str) -> None:
        self.seen[digest] = self.clock()
        if len(self.seen) > SEEN_KEEP:
            for key in sorted(self.seen, key=self.seen.__getitem__)[: len(self.seen) - SEEN_KEEP]:
                del self.seen[key]

    def _expire(self, now: float) -> None:
        for key in [k for k, a in self.allows.items() if a.expires <= now]:
            self._remember(key)
            del self.allows[key]

    def _strike(self, now: float) -> None:
        self.strikes = [t for t in self.strikes if t > now - 60] + [now]
        if len(self.strikes) >= LOCKOUT_COUNT:
            self.locked_until = now + LOCKOUT_SECONDS
            self.strikes = []

    def consume(self, ticket: str, nonce: str) -> Allow | None:
        """The capability this ticket names, removed (single use); ``None`` when it is bad in any way."""
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        now = self.clock()
        with self.lock:
            self._expire(now)
            if now < self.locked_until:
                return None
            item = self.allows.get(digest)
            if item is None:
                if digest in self.seen:  # a replayed or expired capability counts; an unknown hash never does
                    self._strike(now)
                return None
            del self.allows[digest]
            self._remember(digest)
            if not self.accepting or item.epoch != self.epoch or item.runner_gen != self.gen or item.nonce != nonce:
                return None
            return item

    # -- the lease (runner side, §12.6.2)
    def _take(self, item: Allow, att: Attachment) -> tuple[bool, list[tuple[socket.socket, dict[str, Any]]]]:
        holder, gen = self.leases.get(item.run, (None, 0))
        if not item.take and (holder is not None or gen != item.lease_gen):
            return False, []
        gen += 1
        notes = []
        old = self.attachments.get(holder) if holder else None
        if old is not None:
            old.writer = False
            notes.append((old.conn, {"t": "writer", "writer": False, "lease_gen": gen}))
        self.leases[item.run] = (att.att, gen)
        att.writer = True
        return True, notes

    def _release(self, att: Attachment) -> list[tuple[socket.socket, dict[str, Any]]]:
        holder, gen = self.leases.get(att.run, (None, 0))
        if holder != att.att:
            return []
        gen += 1
        self.leases[att.run] = (None, gen)
        return [
            (a.conn, {"t": "writer", "writer": False, "lease_gen": gen})
            for a in self.attachments.values()
            if a.run == att.run and a.att != att.att
        ]

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
        att: Attachment | None = None
        try:
            if peer_credentials(conn).uid != self.uid:
                return
            _send(conn, {"t": "hello", "gen": self.gen, "pid": os.getpid()})
            frame = _read_frame(conn, ATTACH_WAIT)
            ok = isinstance(frame, dict) and frame.get("t") == "attach"
            ticket, nonce = (frame.get("ticket"), frame.get("nonce")) if ok else (None, None)
            item = None
            if isinstance(ticket, str) and isinstance(nonce, str) and 0 < len(ticket) <= 64:
                item = self.consume(ticket, nonce)
            info = self.info(item.run) if item is not None else None
            if item is None or info is None:
                _end(conn, "ticket_invalid")
                return
            new = Attachment(item.att, item.run, item.person, item.mode, conn)
            with self.lock:
                if not self.accepting or item.epoch != self.epoch:
                    _end(conn, "ticket_invalid")
                    return
                notes: list[tuple[socket.socket, dict[str, Any]]] = []
                if item.mode == "write":
                    taken, notes = self._take(item, new)
                    if not taken:
                        _end(conn, "writer_busy")
                        return
                self.attachments[item.att] = att = new
                lease_gen = self.leases.get(item.run, (None, 0))[1]
            for other, note in notes:
                _send(other, note)
            _send(conn, {"t": "ready", "start": info["start"], "offset": info["offset"], "cols": info["cols"],
                         "rows": info["rows"], "writer": new.writer, "lease_gen": lease_gen,
                         "state": info["state"]})  # fmt: skip
            conn.settimeout(None)
            while True:  # H11b streams here; until then the attachment stays open until either side ends it
                try:
                    data = conn.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    break
        except OSError:
            pass
        finally:
            if att is not None:
                with self.lock:
                    others: list[tuple[socket.socket, dict[str, Any]]] = []
                    if self.attachments.get(att.att) is att:
                        del self.attachments[att.att]
                        others = self._release(att)
                for other, note in others:
                    _send(other, note)
            with contextlib.suppress(OSError):
                conn.close()


def _read_frame(conn: socket.socket, wait: float) -> Any:
    deadline = time.monotonic() + wait
    decoder = frames.Decoder()
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        conn.settimeout(left)
        try:
            part = conn.recv(4096)
        except (TimeoutError, OSError):
            return None
        if not part:
            return None
        got = decoder.feed(part)
        if got:
            return None if isinstance(got[0], frames.FrameError) else got[0]


def _send(conn: socket.socket, frame: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        conn.settimeout(2)
        conn.sendall(frames.encode(frame))


def _end(conn: socket.socket, reason: str) -> None:
    _send(conn, {"t": "end", "reason": reason})
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)

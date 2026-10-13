"""The runner's term socket (§12.6.1 to §12.6.4): ``<run>/term/<uid>/term.sock``, where the dashboard server attaches.

The host decides every attach and registers it here with ``attach.allow`` over its command stream (§12.6.2); the
runner never sees a ticket, only its SHA-256. On every new connection (peer UID = the runner's UID) the runner sends
first ``{t:"hello", gen, pid}`` (the dashboard server checks both against what the host told it, §12.6.1 step 4); the
server answers with ``{t:"attach", ticket, nonce}`` within 5 s. Frames use §0.2's codec (4-byte length).

**The capability** is consumed atomically and only once: the hash must match a pending record whose ``runner_gen``,
``nonce`` and expiry hold (10 s unused at most, whatever the host's expiry says), in the current host epoch. A bad
ticket closes the connection with ``{t:"end", reason:"ticket_invalid"}`` and nothing else. Only presentations of a hash
the runner **has seen** (consumed or expired) count toward the lockout (10 per minute, then 60 s of refusals); an
unknown hash closes without counting, so random tickets cannot lock terminals out. The lockout is per runner, not
per client: one client replaying seen tickets locks every attachment out for 60 s (§12.6.2, by design). A pending
capability holds nothing: **the ticket never holds the lease**; the lease is taken only when it is consumed.

**Authorization is checked at use, not only at consumption** (one function, ``TermListener._authorized``, called
before every privileged action: each output frame, each input write (and each retry of a partial write), each take
and each resize): the attachment must still be in the table, not revoked, not closed, in the current host epoch with
the host still there, and for a write action it must still hold the lease at the generation the frame names. An
attachment found revoked is ended (``{t:"end"}``) and nothing more flows in either direction; ``attach.end``,
``end_run``, ``host_lost`` and a new epoch revoke under the same lock that removes it, so there is no window
between the capability check and the byte. A lease that moved only refuses the write action (``writer:false``).

**Limits** at consumption (§12.6.5): 4 attachments per run, 16 per person, 64 per runner; over one the connection ends
with ``{t:"end", reason:"attach.limit"}`` (the host refuses earlier from the counts the runner reports).

**The lease** (§12.6.2): ``take:false`` is refused with ``{t:"end", reason:"writer_busy"}`` when the lease is held or
its generation moved since the capability was registered; ``take:true`` takes it; either way the generation increments,
the old holder gets ``{t:"writer", writer:false, lease_gen}`` and the PTY's size is nudged so full-screen programs
redraw. Closing the holder's connection releases the lease at once (generation +1, every other viewer is told).

**The byte path** (§12.6.3) after ``{t:"ready", start, offset, cols, rows, writer, lease_gen, state}``:

* **Output** ``{t:"out", off, b64}`` (at most 64 KiB decoded), read from the run's 1 MiB ring by one sender thread per
  attachment; the ring never waits for a viewer. Without ``from`` the whole ring is replayed (offsets below
  ``ready.offset`` are replay). An attachment more than 512 KiB behind the ring's end, or behind its start, skips
  ahead and gets one ``{t:"gap", from, to}``. **Control frames are never dropped**: ``gap``, ``resize``, ``writer``,
  ``exit``, ``ack``, ``throttle`` and ``end`` wait in their own queue, sent before any further output.
* **Input** ``{t:"in", input_id, lease_gen, b64}`` (at most 64 KiB) only from the lease holder with the current lease
  generation (otherwise ``{t:"writer", writer:false, lease_gen}`` and nothing is written). An ``input_id`` at or below
  the last one written is never written again (it is acknowledged again). Each frame is written to the PTY in full,
  partial writes retried here, then ``{t:"ack", input_id}``. Over 256 KiB/s or 1 MiB per 10 s per run the frame is not
  written and the runner answers ``{t:"throttle", retry_ms}``; the server resends it with the same id.
* **Resize** ``{t:"resize", lease_gen, cols, rows}`` applies only from the current writer; every viewer then gets
  ``{t:"resize", cols, rows}``. A read-only viewer's resize is ignored.
* **The end**: after the run's leader exits and its PTY is drained and closed, the remaining output, then
  ``{t:"exit", code, signal}`` and ``{t:"end", reason:"exited"}``. ``attach.end`` ends an attachment with
  ``{t:"end", reason}`` and discards its queued output; losing the host ends every attachment
  (``host.restarting``) and refuses capabilities until the next ``runner.hello`` is answered (a new epoch).

Terminal content is never logged. Every change of a run's attachments, lease or size is reported to the host
(``on_change``), which counts attachments against its limits and shows ``viewers``/``writer`` in ``Run``.
"""

from __future__ import annotations

import base64
import binascii
import collections
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
from dataclasses import dataclass, field
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
TICKET_LEN = 43  # 32 bytes, base64url without padding
#: connections handled at once; more are closed at once (any same-UID process could otherwise exhaust threads)
MAX_CONNECTIONS = 32
#: §0.11 (v1.6) attachment limits
PER_RUN, PER_PERSON, PER_RUNNER = 4, 16, 64
#: §12.6.3: output and input frames, the per-attachment queue, the input rate per run
CHUNK = 64 * 1024
QUEUE = 512 * 1024
RATE_SECOND = 256 * 1024
RATE_WINDOW, RATE_WINDOW_BYTES = 10.0, 1024 * 1024
#: a viewer that does not read for this long is disconnected (its sender would otherwise wait forever)
SEND_TIMEOUT = 30.0
#: how long an ending attachment may take to deliver its last frames before its socket is shut
END_GRACE = 2.0
WAKE = 0.1


class TicketError(Exception):
    def __init__(self, message: str, code: str = "invalid.input") -> None:
        super().__init__(message)
        self.code = code


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


@dataclass(eq=False)
class Attachment:
    att: str
    run: str
    person: str
    mode: str
    conn: socket.socket
    writer: bool = False
    #: the next output offset to send
    pos: int = 0
    #: the last input id written to the PTY (ids at or below it are never written again)
    last_input: int = 0
    ctl: collections.deque[dict[str, Any]] = field(default_factory=collections.deque)
    wake: threading.Event = field(default_factory=threading.Event)
    #: ``end`` queued: no more output, the socket closes after the control queue
    ending: bool = False
    closed: bool = False
    #: the host epoch the capability was consumed in (an attachment never outlives it)
    epoch: int = 0
    #: set under the lock by whoever takes the attachment out of the table (``end``, ``host_lost``, a new epoch):
    #: from then on :meth:`TermListener._authorized` refuses everything, in both directions
    revoked: bool = False


class _Bucket:
    """A token bucket: ``rate`` bytes per second, at most ``size`` in reserve."""

    def __init__(self, rate: float, size: float, now: float) -> None:
        self.rate, self.size, self.level, self.at = rate, size, size, now

    def wait_for(self, n: int, now: float) -> float:
        self.level = min(self.size, self.level + (now - self.at) * self.rate)
        self.at = now
        return 0.0 if self.level >= n else (n - self.level) / self.rate


def _parse_time(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError("expires_at")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class TermListener:
    def __init__(
        self,
        path: Path,
        uid: int,
        *,
        gen: str = "0" * 32,
        info: Callable[[str], dict[str, Any] | None] | None = None,
        sup: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.uid = uid
        self.gen = gen
        #: the supervisor (:class:`orch.runner.supervise.Supervisor`): rings, input, sizes; ``None`` streams nothing
        self.sup = sup
        if sup is not None:
            sup.on_output = self._output
        default = sup.attach_info if sup is not None else None
        self.info = info or default or (lambda run: {"start": 0, "offset": 0, "cols": 120, "rows": 40,
                                                       "state": "running"})  # fmt: skip
        self.clock = clock
        self.lock = threading.Lock()
        self.allows: dict[str, Allow] = {}  # by ticket hash
        self.attachments: dict[str, Attachment] = {}
        self._by_run: dict[str, tuple[Attachment, ...]] = {}
        self.leases: dict[str, tuple[str | None, int]] = {}  # run -> (holder att, lease generation)
        self.accepting = False
        self.epoch = 0
        self.seen: dict[str, float] = {}  # hashes of consumed or expired capabilities
        #: attachments the host ended before their connection was registered (a cancel between consume and take)
        self.cancelled: dict[str, float] = {}
        self.strikes: list[float] = []
        self.locked_until = 0.0
        self.sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closing = False
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._rates: dict[str, tuple[_Bucket, _Bucket]] = {}
        #: called with a run id (outside every lock) when its attachments, lease or size changed
        self.on_change: Callable[[str], None] | None = None

    # -- the socket
    def open(self) -> None:
        directory = self.path.parent
        with contextlib.suppress(FileExistsError):
            os.mkdir(directory.parent, 0o711)  # <run>/term: only traversed
        top = os.lstat(directory.parent)
        if not stat.S_ISDIR(top.st_mode) or top.st_uid not in (0, self.uid) or stat.S_IMODE(top.st_mode) & 0o022:
            why = f"{directory.parent}: expected a directory of root or UID {self.uid}, not writable by others"
            raise OSError(errno.EPERM, why)
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
            moved = epoch != self.epoch
            stale = list(self.attachments.values()) if moved else []
            if moved:
                for att in stale:
                    att.revoked = True
                    del self.attachments[att.att]
                    self._unindex(att)
                self.leases.clear()
            self.epoch = epoch
            self.accepting = True
        for att in stale:
            self._finish(att, "host.restarting")

    def host_lost(self, reason: str = "host.restarting") -> None:
        with self.lock:
            self.accepting = False
            for item in self.allows.values():
                self._remember(item.ticket_hash)
            self.allows.clear()
            ended = list(self.attachments.values())
            for att in ended:
                att.revoked = True
            self.attachments.clear()
            self._by_run.clear()
            self.leases.clear()
        for att in ended:
            self._finish(att, reason)

    def allow(self, args: dict[str, Any], *, epoch: int | None = None) -> dict[str, Any]:
        """Register a capability. ``epoch``: the host connection the command arrived on; one from an earlier
        connection is refused (old-epoch capabilities never register, F4). Returns what ``term.attach`` answers at
        minting: ``{start, offset, cols, rows, lease_gen}``. For ``take:false`` the runner's own lease generation now
        is the one the consumption must still find (the host's view of it may be a moment old)."""
        now = self.clock()
        try:
            item = Allow(
                str(args["att"]), str(args["ticket_hash"]), str(args["workspace_id"]), str(args["person"]),
                str(args["run"]), args.get("session"), str(args["mode"]), bool(args.get("take")),
                int(args.get("lease_gen") or 0), str(args["runner_gen"]), args.get("from"), str(args["nonce"]),
                min(_parse_time(args["expires_at"]), now + ALLOW_SECONDS), self.epoch if epoch is None else epoch,
            )  # fmt: skip
        except (KeyError, TypeError, ValueError):
            raise TicketError("invalid attach.allow") from None
        if item.mode not in ("read", "write") or len(item.ticket_hash) != 64 or len(item.nonce) != 32:
            raise TicketError("invalid attach.allow")
        if item.start_from is not None and (not _int(item.start_from) or item.start_from < 0):
            raise TicketError("invalid attach.allow")
        if item.runner_gen != self.gen:
            raise TicketError("attach.allow for another runner generation")
        info = self.info(item.run)
        if info is None:
            raise TicketError("the run's terminal is not kept", "term.not_attachable")
        with self.lock:
            if not self.accepting or item.epoch != self.epoch:
                raise TicketError("no host epoch, or one that ended")
            self._expire(now)
            item.lease_gen = self.leases.get(item.run, (None, 0))[1]
            self.allows[item.ticket_hash] = item
        return {"start": info["start"], "offset": info["offset"], "cols": info["cols"], "rows": info["rows"],
                "lease_gen": item.lease_gen}  # fmt: skip

    def end(self, att: str, reason: str) -> bool:
        with self.lock:
            found = self.attachments.pop(att, None)
            for key in [k for k, a in self.allows.items() if a.att == att]:
                self._remember(key)
                del self.allows[key]
            if found is None:
                self.cancelled[att] = self.clock()  # it may be consumed but not registered yet: it must not register
                if len(self.cancelled) > SEEN_KEEP:
                    del self.cancelled[next(iter(self.cancelled))]
            else:
                found.revoked = True
                self._unindex(found)
            others = self._release(found) if found is not None else []
        if found is not None:
            self._finish(found, reason)
        for other, frame in others:
            self._queue(other, frame)
        if found is not None:
            self._changed(found.run)
        return found is not None

    def end_run(self, run: str, reason: str) -> int:
        """End every attachment of a run (the person stopped it): each gets ``{t:"end", reason}``."""
        with self.lock:
            atts = [a.att for a in self.attachments.values() if a.run == run]
        return sum(1 for att in atts if self.end(att, reason))

    def summary(self, run: str) -> dict[str, Any]:
        """What the host is told about a run's terminal (``runner.term``)."""
        with self.lock:
            atts = [{"att": a.att, "mode": a.mode, "writer": a.writer} for a in self._by_run.get(run, ())]
            lease_gen = self.leases.get(run, (None, 0))[1]
        live = self.sup.live_of(run) if self.sup is not None else None
        cols, rows = (live.cols, live.rows) if live is not None else (None, None)
        return {"run": run, "atts": atts, "lease_gen": lease_gen, "cols": cols, "rows": rows}

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
    def _take(self, item: Allow, att: Attachment) -> tuple[bool, list[tuple[Attachment, dict[str, Any]]]]:
        holder, gen = self.leases.get(item.run, (None, 0))
        if not item.take and (holder is not None or gen != item.lease_gen):
            return False, []
        gen += 1
        notes = []
        old = self.attachments.get(holder) if holder else None
        if old is not None:
            old.writer = False
            notes.append((old, {"t": "writer", "writer": False, "lease_gen": gen}))
        self.leases[item.run] = (att.att, gen)
        att.writer = True
        return True, notes

    def _release(self, att: Attachment) -> list[tuple[Attachment, dict[str, Any]]]:
        holder, gen = self.leases.get(att.run, (None, 0))
        if holder != att.att:
            return []
        gen += 1
        self.leases[att.run] = (None, gen)
        att.writer = False
        return [(a, {"t": "writer", "writer": False, "lease_gen": gen}) for a in self._by_run.get(att.run, ())
                if a.att != att.att]  # fmt: skip

    def _index(self, att: Attachment) -> None:
        self._by_run[att.run] = (*self._by_run.get(att.run, ()), att)

    def _unindex(self, att: Attachment) -> None:
        rest = tuple(a for a in self._by_run.get(att.run, ()) if a is not att)
        if rest:
            self._by_run[att.run] = rest
        else:
            self._by_run.pop(att.run, None)

    def _over_limit(self, item: Allow) -> bool:
        live = list(self.attachments.values())
        return (
            sum(1 for a in live if a.run == item.run) >= PER_RUN
            or sum(1 for a in live if a.person == item.person) >= PER_PERSON
            or len(live) >= PER_RUNNER
        )

    # -- frames to one attachment (only its sender thread writes its socket)
    def _queue(self, att: Attachment, frame: dict[str, Any]) -> None:
        if att.closed:
            return
        att.ctl.append(frame)
        att.wake.set()

    def _finish(self, att: Attachment, reason: str) -> None:
        """Queue ``end`` (after the control frames already queued; queued output is discarded) and make sure the
        socket is shut soon even when the viewer does not read."""
        if att.ending or att.closed:
            return
        att.ending = True
        self._queue(att, {"t": "end", "reason": reason})
        timer = threading.Timer(END_GRACE, _shut, args=(att.conn,))
        timer.daemon = True
        timer.start()

    def _output(self, run: str) -> None:
        """The supervisor's wake-up: new output, an exit, a closed PTY (I/O thread; never blocks)."""
        for att in self._by_run.get(run, ()):
            att.wake.set()

    def _changed(self, run: str) -> None:
        hook = self.on_change
        if hook is not None:
            try:
                hook(run)
            except Exception:  # noqa: BLE001 - reporting never breaks an attachment
                LOG.exception("term change report failed")

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
            if not self._slots.acquire(blocking=False):
                with contextlib.suppress(OSError):
                    conn.close()
                continue
            threading.Thread(target=self._handle_slot, args=(conn,), daemon=True).start()

    def _handle_slot(self, conn: socket.socket) -> None:
        try:
            self._handle(conn)
        finally:
            self._slots.release()

    def _handle(self, conn: socket.socket) -> None:
        att: Attachment | None = None
        try:
            if peer_credentials(conn).uid != self.uid:
                return
            _send(conn, {"t": "hello", "gen": self.gen, "pid": os.getpid()})
            decoder = frames.Decoder()
            frame, rest = _read_frame(conn, ATTACH_WAIT, decoder)
            ok = isinstance(frame, dict) and frame.get("t") == "attach"
            ticket, nonce = (frame.get("ticket"), frame.get("nonce")) if ok else (None, None)
            item = None
            if isinstance(ticket, str) and isinstance(nonce, str) and len(ticket) == TICKET_LEN:
                item = self.consume(ticket, nonce)
            info = self.info(item.run) if item is not None else None
            if item is None or info is None:
                _end(conn, "ticket_invalid")
                return
            new = Attachment(item.att, item.run, item.person, item.mode, conn, epoch=item.epoch)
            with self.lock:
                if not self.accepting or item.epoch != self.epoch:
                    _end(conn, "ticket_invalid")
                    return
                if self.cancelled.pop(item.att, None) is not None:
                    _end(conn, "cancelled")
                    return
                if self._over_limit(item):
                    _end(conn, "attach.limit")
                    return
                notes: list[tuple[Attachment, dict[str, Any]]] = []
                if item.mode == "write":
                    taken, notes = self._take(item, new)
                    if not taken:
                        _end(conn, "writer_busy")
                        return
                info = self.info(item.run) or info  # the offsets at the moment the attachment exists
                start, offset = int(info["start"]), int(info["offset"])
                new.pos = start if item.start_from is None else max(int(item.start_from), 0)
                self.attachments[item.att] = att = new
                self._index(new)
                lease_gen = self.leases.get(item.run, (None, 0))[1]
            ready = {"t": "ready", "start": start, "offset": offset, "cols": info["cols"], "rows": info["rows"],
                     "writer": new.writer, "lease_gen": lease_gen, "state": info["state"]}  # fmt: skip
            new.ctl.append(ready)
            for other, note in notes:
                self._queue(other, note)
            if new.writer and self.sup is not None:
                self.sup.nudge(item.run)  # the new writer sees a fresh screen (§12.6.2, dashboard G9)
            threading.Thread(target=self._pump, args=(new,), name="orch-runner-term-out", daemon=True).start()
            self._changed(item.run)
            self._serve(new, decoder, rest)
        except OSError:
            pass
        finally:
            if att is not None:
                self._drop(att)
            with contextlib.suppress(OSError):
                conn.close()

    def _drop(self, att: Attachment) -> None:
        """The attachment's connection is gone: forget it and release its lease (idempotent)."""
        with self.lock:
            others: list[tuple[Attachment, dict[str, Any]]] = []
            dropped = self.attachments.get(att.att) is att
            if dropped:
                del self.attachments[att.att]
                self._unindex(att)
                others = self._release(att)
        att.closed = True
        att.wake.set()
        for other, note in others:
            self._queue(other, note)
        if dropped:
            self._changed(att.run)

    # -- the sender: control frames first, then output from the ring
    def _pump(self, att: Attachment) -> None:
        conn = att.conn
        try:
            conn.settimeout(SEND_TIMEOUT)
            while not att.closed:
                att.wake.clear()
                while att.ctl:
                    frame = att.ctl.popleft()
                    conn.sendall(frames.encode(frame))
                    if frame.get("t") == "end":
                        _shut(conn)
                        return
                if att.ending:
                    att.wake.wait(WAKE)
                    continue
                if self._step(att):
                    continue
                att.wake.wait(WAKE)
        except (OSError, frames.FrameError):
            pass
        finally:
            att.closed = True
            _shut(conn)

    def _step(self, att: Attachment) -> bool:
        """Queue or send what is next for one attachment; ``True`` when something was done."""
        if not self._authorized(att):
            return False
        live = self.sup.live_of(att.run) if self.sup is not None else None
        if live is None:
            if self.sup is not None:
                self._finish(att, "exited")  # the run's ring is no longer kept
                return True
            return False
        ring = live.ring
        start, end = ring.start, ring.end
        if att.pos < start or end - att.pos > QUEUE:
            skip_to = max(start, end - QUEUE)
            att.ctl.append({"t": "gap", "from": att.pos, "to": skip_to})
            att.pos = skip_to
            return True
        if att.pos < end:
            _, begin, data = ring.read(att.pos, CHUNK)
            if begin > att.pos:  # the ring moved on between the two reads
                att.ctl.append({"t": "gap", "from": att.pos, "to": begin})
                att.pos = begin
                return True
            att.pos = begin + len(data)
            if data and self._authorized(att):
                att.conn.sendall(frames.encode({"t": "out", "off": begin, "b64": base64.b64encode(data).decode()}))
            return True
        if live.exit is not None and live.pty_closed and not att.ending:
            exit_ = live.exit
            att.ctl.append({"t": "exit", "code": exit_.get("code"), "signal": exit_.get("signal")})
            self._finish(att, "exited")
            return True
        return False

    # -- the reader: input and resize from the dashboard server
    def _serve(self, att: Attachment, decoder: frames.Decoder, pending: list[Any]) -> None:
        conn = att.conn
        conn.settimeout(None)
        queue = list(pending)
        while not att.closed:
            while queue:
                frame = queue.pop(0)
                if isinstance(frame, frames.FrameError) or not isinstance(frame, dict):
                    self._finish(att, "protocol")
                    return
                kind = frame.get("t")
                if kind == "in":
                    ok = self._input(att, frame)
                elif kind == "resize":
                    ok = self._resize(att, frame)
                else:
                    ok = False
                if not ok:
                    self._finish(att, "protocol")
                    return
            try:
                data = conn.recv(65536)
            except OSError:
                return
            if not data:
                return
            queue = decoder.feed(data)

    def _authorized(self, att: Attachment, lease_gen: Any = None) -> bool:
        """The one authorization check, called before every privileged action (§12.6.2, N5). Without ``lease_gen``:
        may this attachment still see output (it is registered, not revoked or closed, in the current epoch, with the
        host there)? With it: may it also write, i.e. is it a write attachment holding the lease at ``lease_gen``?
        A revoked attachment is ended here, whoever noticed first; a lease that moved only refuses the write
        (``writer:false``)."""
        with self.lock:
            live = (
                self.attachments.get(att.att) is att and not att.revoked and not att.closed
                and self.accepting and att.epoch == self.epoch
            )  # fmt: skip
            holder, gen = self.leases.get(att.run, (None, 0))
            leased = att.mode == "write" and att.writer and holder == att.att and lease_gen == gen
            if not live:
                att.revoked = True
                att.writer = False
        if not live:
            self._finish(att, "host.restarting" if not self.accepting or att.epoch != self.epoch else "cancelled")
            return False
        if lease_gen is None or leased:
            return True
        self._queue(att, {"t": "writer", "writer": False, "lease_gen": gen})
        return False

    def downgrade(self, att_id: str) -> bool:
        """Take the lease from an attachment without ending it (it keeps reading): the next write or resize it sends
        is refused. ``True`` when it held the lease."""
        with self.lock:
            att = self.attachments.get(att_id)
            if att is None or not att.writer:
                return False
            notes = self._release(att)
            run = att.run
            gen = self.leases.get(run, (None, 0))[1]
        self._queue(att, {"t": "writer", "writer": False, "lease_gen": gen})
        for other, note in notes:
            self._queue(other, note)
        self._changed(run)
        return True

    def _input(self, att: Attachment, frame: dict[str, Any]) -> bool:
        """One ``in`` frame; ``False`` for a malformed one (the attachment ends with ``protocol``)."""
        iid, lease_gen, b64 = frame.get("input_id"), frame.get("lease_gen"), frame.get("b64")
        if not (_int(iid) and iid >= 1 and _int(lease_gen) and isinstance(b64, str)) or len(b64) > CHUNK * 4 // 3 + 4:
            return False
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return False
        if len(data) > CHUNK:
            return False
        if att.mode != "write":
            return True  # a read attachment's input is dropped (the server should not send it)
        if not self._authorized(att, lease_gen):
            return True
        if iid <= att.last_input:  # a repeated or lower id is never typed again
            self._queue(att, {"t": "ack", "input_id": iid})
            return True
        retry = self._rate(att.run, len(data))
        if retry > 0:
            self._queue(att, {"t": "throttle", "retry_ms": max(1, int(retry * 1000 + 0.999))})
            return True
        if not self._write_all(att, data, lease_gen):
            return True  # the run ended or the attachment closed mid-write: no ack (the input may not have arrived)
        att.last_input = iid
        self._queue(att, {"t": "ack", "input_id": iid})
        return True

    def _rate(self, run: str, n: int) -> float:
        """Seconds until ``n`` input bytes fit both per-run limits (``0``: taken now)."""
        now = time.monotonic()
        with self.lock:
            buckets = self._rates.get(run)
            if buckets is None:
                buckets = (_Bucket(RATE_SECOND, RATE_SECOND, now),
                           _Bucket(RATE_WINDOW_BYTES / RATE_WINDOW, RATE_WINDOW_BYTES, now))  # fmt: skip
                self._rates[run] = buckets
            wait = max(b.wait_for(n, now) for b in buckets)
            if wait == 0:
                for b in buckets:
                    b.level -= n
            return wait

    def _write_all(self, att: Attachment, data: bytes, lease_gen: int) -> bool:
        """Write every byte (partial writes retried here, never by the client); ``False`` when it cannot finish."""
        if self.sup is None:
            return False
        from .supervise import SpawnError

        view = memoryview(data)
        while view:
            if att.closed or not self._authorized(att, lease_gen):
                return False
            try:
                n = self.sup.write_input(att.run, bytes(view))
            except SpawnError:
                return False
            if n == 0:
                time.sleep(0.005)  # the PTY's input queue is full: the program is not reading yet
                continue
            view = view[n:]
        return True

    def _resize(self, att: Attachment, frame: dict[str, Any]) -> bool:
        cols, rows, lease_gen = frame.get("cols"), frame.get("rows"), frame.get("lease_gen")
        if not (_int(cols) and _int(rows) and _int(lease_gen)):
            return False
        if att.mode != "write" or not (20 <= cols <= 500 and 5 <= rows <= 200):
            return True  # a read viewer's resize, or a size out of range: ignored (§12.6.4)
        if not self._authorized(att, lease_gen) or self.sup is None:
            return True
        live = self.sup.live_of(att.run)
        if live is None or (live.cols, live.rows) == (cols, rows):
            return True
        from .supervise import SpawnError

        try:
            self.sup.resize(att.run, cols, rows)
        except SpawnError:
            return True
        for other in self._by_run.get(att.run, ()):
            self._queue(other, {"t": "resize", "cols": cols, "rows": rows})
        self._changed(att.run)
        return True


def _read_frame(conn: socket.socket, wait: float, decoder: frames.Decoder) -> tuple[Any, list[Any]]:
    """The first frame within ``wait`` seconds and any frames that arrived with it (``(None, [])`` on failure)."""
    deadline = time.monotonic() + wait
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None, []
        conn.settimeout(left)
        try:
            part = conn.recv(4096)
        except (TimeoutError, OSError):
            return None, []
        if not part:
            return None, []
        got = decoder.feed(part)
        if got:
            return (None, []) if isinstance(got[0], frames.FrameError) else (got[0], got[1:])


def _send(conn: socket.socket, frame: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        conn.settimeout(2)
        conn.sendall(frames.encode(frame))


def _shut(conn: socket.socket) -> None:
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_RDWR)


def _end(conn: socket.socket, reason: str) -> None:
    _send(conn, {"t": "end", "reason": reason})
    _shut(conn)

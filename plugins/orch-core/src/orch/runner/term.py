"""The runner's term socket (§12.6.1 to §12.6.4): ``<run>/term/<uid>/term.sock``, where the dashboard server attaches.

The host decides every attach and registers it here with ``attach.allow`` over its command stream (§12.6.2); the
runner never sees a ticket, only its SHA-256. On every new connection (peer UID = the runner's UID; in tier B the
PERSON's, §13.5) the runner sends
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
before every privileged action: each non-blocking ``send`` of an output frame, each input write (and each retry of a
partial write), each take and each resize): the attachment must still be in the table, not revoked, not closed, in
the current host epoch with the host still there, and for a write action it must still hold the lease at the
generation the frame names. The action itself (the ``send``, the PTY write, the resize) runs non-blocking under the
same lock as the check, and ``attach.end``, ``end_run``, ``host_lost``, a new epoch and an overflow revoke under that
lock, so no output byte is sent, and no input byte or resize is applied, after the revocation. An output frame that
a revocation cuts short is never completed: the socket is shut without another byte. An attachment found revoked is
otherwise ended (``{t:"end"}``). A lease that moved only refuses the write action (``writer:false``).

**Limits** at consumption (§12.6.5): 4 attachments per run, 16 per person, 64 per runner; over one the connection ends
with ``{t:"end", reason:"attach.limit"}`` (the host refuses earlier from the counts the runner reports). At most 256
capabilities wait to be presented (``attach.allow`` beyond is ``attach.limit``). Per attachment at most 4 096 control
frames wait to be sent and 64 frames are read ahead while a PTY write waits for room; beyond either the attachment is
revoked and ends with ``{t:"end", reason:"overflow"}``.

**The socket** is non-blocking once the attachment exists, with separate deadlines: the sender waits for write
readiness and gives up after ``SEND_TIMEOUT`` (30 s) without progress; the reader waits for read readiness with no
deadline (an idle viewer stays attached). While a PTY write waits for room the reader keeps watching the socket, so a
viewer that disconnects cancels the pending write at once.

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
  generation (otherwise ``{t:"writer", writer:false, lease_gen}`` and nothing is written). Input ids are consecutive:
  only ``last + 1`` is written; an ``input_id`` at or below the last one written is never written again (it is
  acknowledged again); a higher one (a gap) is refused with ``{t:"throttle", retry_ms}`` and never acknowledged, so a
  frame that was throttled can never be skipped by a later one. Each frame is written to the PTY in full,
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

from orch import fdwait
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
#: connections handled at once: 64 attachments per runner plus some still in their 5 s handshake; more are closed
#: at once (any same-UID process could otherwise exhaust threads)
MAX_CONNECTIONS = 96
#: §0.11 (v1.6) attachment limits
PER_RUN, PER_PERSON, PER_RUNNER = 4, 16, 64
#: §12.6.3: output and input frames, the per-attachment queue, the input rate per run
CHUNK = 64 * 1024
QUEUE = 512 * 1024
RATE_SECOND = 256 * 1024
RATE_WINDOW, RATE_WINDOW_BYTES = 10.0, 1024 * 1024
#: a viewer that does not read for this long is disconnected (its sender would otherwise wait forever); an idle
#: reader (one that sends nothing) is never disconnected: reading and writing have separate deadlines
SEND_TIMEOUT = 30.0
#: control frames waiting for one attachment; over it the attachment ends with ``overflow``
CTL_MAX = 4096
#: frames read ahead while a PTY write waits for room; over it the attachment ends with ``overflow``
INBOX_MAX = 64
#: bytes per read from a viewer's socket; outside a stalled write the next read waits until every frame of the last one
#: was handled (backpressure), so at most one read's frames are buffered
READ_BYTES = 64 * 1024
#: the largest frame a viewer may send (an ``in`` frame of CHUNK bytes is about 87 KiB); a longer length prefix ends
#: the attachment with ``protocol`` before its body is buffered
FRAME_MAX = 128 * 1024
#: capabilities registered and not yet presented, per runner
MAX_ALLOWS = 256
#: what an ``in`` frame past a gap is answered with: ``throttle`` (resend the missing one first)
GAP_RETRY_MS = 50
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
    #: set when the sender thread is done (the ``end`` frame was written, or the socket failed)
    sent: threading.Event = field(default_factory=threading.Event)
    #: frames read but not handled yet (read ahead while a PTY write waits for room)
    inbox: collections.deque[Any] = field(default_factory=collections.deque)
    decoder: frames.Decoder = field(default_factory=frames.Decoder)
    #: the peer closed, the socket failed or the read-ahead overflowed: the reader stops
    stop_reading: bool = False
    #: an output frame was cut short by a revocation: nothing more may be written to the socket
    broken: bool = False


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


_ACL_ACCESS = "system.posix_acl_access"
_ACL_UNDEF = 0xFFFFFFFF


def acl_blob(owner: int, person: int, grant: int) -> bytes:
    """The access ACL (Linux xattr format, version 2) ``owner`` rwx for dirs / rw for sockets, ``person`` ``grant``
    (rwx bits), group and other nothing, mask = ``grant``. ``owner`` is the owner's own permission bits."""
    import struct

    entries = [(0x01, owner, _ACL_UNDEF), (0x02, grant, person), (0x04, 0, _ACL_UNDEF),
               (0x10, grant, _ACL_UNDEF), (0x20, 0, _ACL_UNDEF)]  # fmt: skip
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", tag, perm, ident) for tag, perm, ident in entries)


def _grant_acl(path: Path, person: int, grant: int) -> None:
    """Give ``person`` ``grant`` (4 r, 2 w, 1 x) on ``path`` and nothing to group or other, by POSIX ACL, then read it
    back: any failure (no ACL support, a different answer) raises, and the caller serves nothing (fail closed)."""
    is_dir = stat.S_ISDIR(os.lstat(path).st_mode)
    blob = acl_blob(0o7 if is_dir else 0o6, person, grant)
    try:
        os.setxattr(path, _ACL_ACCESS, blob, follow_symlinks=False)
        got = os.getxattr(path, _ACL_ACCESS, follow_symlinks=False)
    except (AttributeError, OSError) as exc:
        raise OSError(errno.EPERM, f"{path}: the person's ACL cannot be set ({exc})") from None
    info = os.lstat(path)
    if got != blob or stat.S_IMODE(info.st_mode) & 0o007:
        raise OSError(errno.EPERM, f"{path}: the ACL read back differs")


def _drop_default_acl(path: Path) -> None:
    """No default ACL on the two directories (a leftover or inherited one would be handed to whatever is created in
    them): remove it, so their ACL state is always exactly the access ACL of :func:`_grant_acl`."""
    try:
        os.removexattr(path, "system.posix_acl_default", follow_symlinks=False)
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise OSError(errno.EPERM, f"{path}: the default ACL cannot be removed ({exc})") from None
    except AttributeError:
        raise OSError(errno.EPERM, f"{path}: no ACL support") from None


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
        peer_uid: int | None = None,
    ) -> None:
        self.path = Path(path)
        self.uid = uid
        #: whose connections are served: the runner's own UID in tier A (``None``); in tier B (spec 13.5) the PERSON's
        #: UID, the only one that may connect. Other processes of the agent user (the runs) are refused by the kernel's
        #: peer UID, and the filesystem grants the person alone by ACL
        self.peer_uid = uid if peer_uid is None else peer_uid
        self.tier_b = peer_uid is not None and peer_uid != uid
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
        #: ``runner.term`` sequence number, taken under ``lock`` with the snapshot: the host ignores an older report
        #: that arrives after a newer one (two threads may send their snapshots in either order)
        self._report_seq = 0

    # -- the socket
    def open(self) -> None:
        directory = self.path.parent
        if self.tier_b:
            self._open_dirs_tier_b(directory)
        else:
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
        try:
            if self.tier_b:
                _grant_acl(self.path, self.peer_uid, 0o6)  # connect needs write: the person alone, by ACL
            else:
                os.chmod(self.path, 0o600)
        except BaseException:
            sock.close()
            with contextlib.suppress(OSError):
                os.unlink(self.path)
            raise
        sock.listen(16)
        self.sock = sock
        self._thread = threading.Thread(target=self._accept_loop, name="orch-runner-term", daemon=True)
        self._thread.start()

    def _open_dirs_tier_b(self, directory: Path) -> None:
        """Tier B (spec 13.3, 13.5): ``<agent run>/term`` and ``term/<uid>`` belong to the agent (never root's, never
        anyone's else), carry no permission for group or other, and grant the person ``--x`` by ACL, read back. The
        agent run directory above them is made by root with the person's ACL and is not ours to repair."""
        if self.peer_uid in (0, self.uid):
            raise OSError(errno.EPERM, "tier B: the peer must be a person's UID, not root and not the agent")
        for path in (directory.parent, directory):
            with contextlib.suppress(FileExistsError):
                os.mkdir(path, 0o700)
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.uid:
                raise OSError(errno.EPERM, f"{path}: expected a directory of UID {self.uid}")
            _grant_acl(path, self.peer_uid, 0o1)
            _drop_default_acl(path)

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
            if len(self.allows) >= MAX_ALLOWS:
                raise TicketError("too many capabilities waiting to be presented", "attach.limit")
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
            self._report_seq += 1
            seq = self._report_seq
        live = self.sup.live_of(run) if self.sup is not None else None
        cols, rows = (live.cols, live.rows) if live is not None else (None, None)
        return {"run": run, "seq": seq, "atts": atts, "lease_gen": lease_gen, "cols": cols, "rows": rows}

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
        if len(att.ctl) >= CTL_MAX and frame.get("t") != "end":
            self._overflow(att)
            return
        att.ctl.append(frame)
        att.wake.set()

    def _overflow(self, att: Attachment) -> None:
        """Too much waits for a viewer that does not read: the attachment is revoked (nothing more is authorized) and
        ends with ``overflow`` (its queued frames are discarded; the socket is shut after the grace). Its write lease
        is released at once, not only when the connection is dropped after the grace."""
        with self.lock:  # never called with the lock held
            att.revoked = True
            held = self.leases.get(att.run, (None, 0))[0] == att.att
            notes = self._release(att)
            att.writer = False
        for other, note in notes:
            self._queue(other, note)
        if held:
            self._changed(att.run)
        if att.ending:
            return
        att.ctl.clear()
        self._finish(att, "overflow")

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
            if peer_credentials(conn).uid != self.peer_uid:
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
            if item.mode == "write" and info.get("state") != "running":
                _end(conn, "ticket_invalid")  # a finished run takes no input, whatever the capability says
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
                lease_gen = self.leases.get(item.run, (None, 0))[1]
                # the immutable snapshot is queued before the attachment is visible: no takeover, resize or lease note
                # of another thread can reach this attachment ahead of its ``ready``
                new.ctl.append({"t": "ready", "start": start, "offset": offset, "cols": info["cols"],
                                "rows": info["rows"], "writer": new.writer, "lease_gen": lease_gen,
                                "state": info["state"]})  # fmt: skip
                new.decoder = decoder
                new.inbox.extend(rest)
                conn.setblocking(False)  # from here on: readiness waits with separate read and write deadlines
                self.attachments[item.att] = att = new
                self._index(new)
            for other, note in notes:
                self._queue(other, note)
            if new.writer and self.sup is not None:
                self.sup.nudge(item.run)  # the new writer sees a fresh screen (§12.6.2, dashboard G9)
            threading.Thread(target=self._pump, args=(new,), name="orch-runner-term-out", daemon=True).start()
            self._changed(item.run)
            self._serve(new)
        except OSError:
            pass
        finally:
            if att is not None:
                if att.ending:  # the reader gave up (protocol error): let the sender deliver the ``end`` first
                    att.sent.wait(END_GRACE + 1)
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
            while not att.closed:
                att.wake.clear()
                while att.ctl:
                    frame = att.ctl.popleft()
                    self._send_bytes(att, frames.encode(frame), guarded=False)
                    if frame.get("t") == "end":
                        _shut(conn)
                        return
                if att.ending:
                    att.wake.wait(WAKE)
                    continue
                if self._step(att):
                    if att.broken:
                        return  # an output frame was cut short by a revocation: nothing more may follow it
                    continue
                att.wake.wait(WAKE)
        except (OSError, frames.FrameError):
            pass
        finally:
            att.closed = True
            att.sent.set()
            _shut(conn)

    def _send_bytes(self, att: Attachment, data: bytes, *, guarded: bool) -> bool:
        """Write one encoded frame to the (non-blocking) socket. ``guarded`` (terminal output): every ``send`` runs
        under the authorization check's lock (:meth:`_authorized` with an action), so a revocation is either before
        a send (nothing of it leaves) or after it; once revoked mid-frame the rest is never written and the attachment
        is ``broken``. ``False`` when refused. ``TimeoutError`` when the viewer reads nothing for ``SEND_TIMEOUT``."""
        conn = att.conn
        view = memoryview(data)
        idle_since = time.monotonic()
        while view:
            if guarded:
                sent: list[int] = []
                if not self._authorized(att, act=lambda v=view, out=sent: out.append(_try_send(conn, v))):
                    if len(view) != len(data):
                        att.broken = True
                    return False
                n = sent[0]
            else:
                n = _try_send(conn, view)
            if n:
                view = view[n:]
                idle_since = time.monotonic()
                continue
            if att.closed:
                raise OSError(errno.EPIPE, "the attachment closed")
            left = SEND_TIMEOUT - (time.monotonic() - idle_since)
            if left <= 0:
                raise TimeoutError("the viewer does not read")
            fdwait.writable(conn, min(left, WAKE))
        return True

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
            if data:
                frame = frames.encode({"t": "out", "off": begin, "b64": base64.b64encode(data).decode()})
                self._send_bytes(att, frame, guarded=True)
            return True
        if live.exit is not None and live.pty_closed and not att.ending:
            exit_ = live.exit
            att.ctl.append({"t": "exit", "code": exit_.get("code"), "signal": exit_.get("signal")})
            self._finish(att, "exited")
            return True
        return False

    # -- the reader: input and resize from the dashboard server
    def _serve(self, att: Attachment) -> None:
        """Handle the inbox frame by frame, then read again (never while frames wait: backpressure). Of a run of
        consecutive ``resize`` frames only the last eligible one (shape, range, write mode, lease) is applied; a frame
        that could not apply never discards a valid one (each is still checked for its shape), so a burst costs
        one PTY resize, one note per viewer and one ``runner.term``, not one per frame."""
        pending: dict[str, Any] | None = None  # the last eligible resize of the run being coalesced
        while not att.closed and not att.stop_reading:
            while att.inbox:
                frame = att.inbox.popleft()
                if isinstance(frame, frames.FrameError) or not isinstance(frame, dict):
                    self._finish(att, "protocol")
                    return
                kind = frame.get("t")
                if kind == "resize":
                    if not all(_int(frame.get(k)) for k in ("cols", "rows", "lease_gen")):
                        self._finish(att, "protocol")
                        return
                    if self._resize_eligible(att, frame):
                        pending = frame  # an ineligible frame never supersedes a valid one
                    if att.inbox and isinstance(att.inbox[0], dict) and att.inbox[0].get("t") == kind:
                        continue  # the run goes on: only its last eligible resize is applied
                    frame, pending = pending, None
                    if frame is None:
                        continue
                if kind == "in":
                    ok = self._input(att, frame)
                elif kind == "resize":
                    ok = self._resize(att, frame)
                else:
                    ok = False
                if not ok:
                    self._finish(att, "protocol")
                    return
                if att.stop_reading:
                    return
            self._read_some(att, WAKE)

    def _read_some(self, att: Attachment, wait: float, limit: int | None = None) -> bool:
        """Wait up to ``wait`` for the socket to be readable and read what is there into ``att.inbox``. ``False`` (and
        ``stop_reading``) when the peer closed or the socket failed. ``limit`` (only the read-ahead while a PTY write
        waits, :meth:`_write_all`): more frames than that in the inbox revoke the attachment (``overflow``). An
        ordinary read has no frame cap: :meth:`_serve` handles the frames of one ``recv`` one by one before it reads
        again, so a burst of small frames (a coalesced run of resizes) never ends a healthy attachment. There is no
        read deadline: a viewer that sends nothing stays attached. Memory is bounded in every state: an ordinary read
        happens only with an empty inbox (at most one read of ``READ_BYTES`` is buffered), the read-ahead of a stall
        adds at most ``INBOX_MAX`` frames, and the decoder holds at most one partial frame of ``FRAME_MAX`` bytes."""
        if limit is None and att.inbox:
            return True  # backpressure: the frames already read are handled first
        conn = att.conn
        try:
            if not fdwait.readable(conn, wait):
                return True
            data = conn.recv(READ_BYTES)
        except (BlockingIOError, InterruptedError):
            return True
        except (OSError, ValueError):
            att.stop_reading = True
            return False
        if not data:
            att.stop_reading = True
            return False
        att.inbox.extend(att.decoder.feed(data))
        if att.decoder.body_started and (att.decoder.body_length or 0) > FRAME_MAX:
            att.stop_reading = True  # a frame longer than any valid one: never buffered to its end
            self._finish(att, "protocol")
            return False
        if limit is not None and len(att.inbox) > limit:
            att.stop_reading = True
            self._overflow(att)
            return False
        return True

    def _authorized(self, att: Attachment, lease_gen: Any = None, act: Callable[[], Any] | None = None) -> bool:
        """The one authorization check, called before every privileged action (§12.6.2, N5). Without ``lease_gen``:
        may this attachment still see output (it is registered, not revoked or closed, in the current epoch, with the
        host there)? With it: may it also write, i.e. is it a write attachment holding the lease at ``lease_gen``?
        A revoked attachment is ended here, whoever noticed first; a lease that moved only refuses the write
        (``writer:false``). ``act``: a non-blocking privileged action (the PTY write, the resize) run under the same
        lock as the check, so a revocation (which takes the lock) can never fall between the check and the action;
        its exceptions propagate after the lock is released."""
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
            elif act is not None and (lease_gen is None or leased):
                act()
        if not live:
            self._finish(att, "host.restarting" if not self.accepting or att.epoch != self.epoch else "cancelled")
            return False
        if lease_gen is None or leased:
            return True
        self._queue(att, {"t": "writer", "writer": False, "lease_gen": gen})
        return False

    def _input(self, att: Attachment, frame: dict[str, Any]) -> bool:
        """One ``in`` frame; ``False`` for a malformed one (the attachment ends with ``protocol``)."""
        iid, lease_gen, b64 = frame.get("input_id"), frame.get("lease_gen"), frame.get("b64")
        if not (_int(iid) and iid >= 1 and _int(lease_gen) and isinstance(b64, str)) or len(b64) > CHUNK * 4 // 3 + 4:
            return False
        if att.mode != "write":
            return True  # a read attachment's input is dropped, not even decoded (the server should not send it)
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return False
        if len(data) > CHUNK:
            return False
        if not self._authorized(att, lease_gen):
            return True
        if iid <= att.last_input:  # a repeated or lower id is never typed again
            self._queue(att, {"t": "ack", "input_id": iid})
            return True
        if iid != att.last_input + 1:  # past a gap: refused, never acknowledged; the missing frame comes first
            self._queue(att, {"t": "throttle", "retry_ms": GAP_RETRY_MS})
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

    def _write_once(self, att: Attachment, lease_gen: int, data: bytes) -> int | None:
        """One PTY write under the authorization check: the bytes written, ``None`` when refused."""
        done: list[int] = []
        sup = self.sup
        if not self._authorized(att, lease_gen, lambda: done.append(sup.write_input(att.run, data))):
            return None
        return done[0]

    def _write_all(self, att: Attachment, data: bytes, lease_gen: int) -> bool:
        """Write every byte (partial writes retried here, never by the client); ``False`` when it cannot finish."""
        if self.sup is None:
            return False
        from .supervise import SpawnError

        view = memoryview(data)
        ahead = len(att.inbox) + INBOX_MAX  # the frames already read are not read-ahead of this write
        while view:
            if att.closed or att.stop_reading:
                return False
            try:
                done = self._write_once(att, lease_gen, bytes(view))
            except SpawnError:
                return False
            if done is None:
                return False
            n = done
            if n == 0:
                # the PTY's input queue is full (the program is not reading yet): meanwhile watch the socket, so a
                # viewer that disconnects cancels this write at once (frames that arrive are handled after it)
                if not self._read_some(att, 0.005, ahead):
                    return False
                continue
            view = view[n:]
        return True

    def _resize_eligible(self, att: Attachment, frame: dict[str, Any]) -> bool:
        cols, rows = frame["cols"], frame["rows"]
        return (
            att.mode == "write" and 20 <= cols <= 500 and 5 <= rows <= 200 and self._authorized(att, frame["lease_gen"])
        )

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
            if not self._authorized(att, lease_gen, lambda: self.sup.resize(att.run, cols, rows)):
                return True
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


def _try_send(conn: socket.socket, view: memoryview) -> int:
    """One non-blocking ``send``: the bytes taken, ``0`` when the socket's buffer is full."""
    try:
        return conn.send(view)
    except (BlockingIOError, InterruptedError):
        return 0


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

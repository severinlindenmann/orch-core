"""Reading the event logs (ticket-format §5, §5.5 "Reading a log"): strict parse of every line, schema check, and what
the store remembers per log (byte offset, last seq, the head of every event).

A line that is not exactly ``cj`` of its strict parse, or that fails its schema, ends the readable part of the log
(``error`` says where): the chain is broken there and nothing after it counts. ``host_sig``, ``prev`` and
authorization are the model's replay.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch import canon, schema

from .errors import StoreError
from .fsio import NotRegularFile, open_nofollow
from .rootfs import RootFS

__all__ = ["LogInfo", "parse_line", "read_new_lines"]

WORKSPACE = "workspace"
MAX_LINE = schema.MAX_EVENT_LINE_BYTES  # an event line is at most this long (with its LF): the cap before parsing


@dataclass
class LogInfo:
    name: str  # "workspace" or the ticket uid
    path: Path
    offset: int = 0  # bytes of complete, good lines
    seq: int = 0
    heads: list[str] = field(default_factory=list)  # heads[i] is the head of seq i + 1
    error: str | None = None  # first unreadable line, if any
    error_seq: int = 0
    seen_size: int = 0  # size of the file when it was opened for reading (a broken log has unread bytes past `offset`)
    ino: int | None = None  # inode of the file when it was read (the freshness check compares it, see Store._fresh)
    seen_times: tuple[int, int] = (0, 0)  # (mtime_ns, ctime_ns) of the file when it was read: part of its signature
    starts: list[int] = field(default_factory=list)  # starts[i] is the byte offset of the line of seq i + 1

    accs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for head in self.heads[len(self.accs) :]:
            self.accs.append(canon.log_acc(self.acc, head))

    @property
    def acc(self) -> str:
        return self.accs[-1] if self.accs else "sha256:" + "0" * 64

    @property
    def head(self) -> str | None:
        return self.heads[-1] if self.heads else None

    @property
    def kind(self) -> str:
        return "workspace" if self.name == WORKSPACE else "ticket"


def _open_ro(path: Path, fs: RootFS | None) -> int:
    """A log opened for reading: below the pinned root (``fs``, #516) when the store has one, else by path."""
    return fs.open(path, os.O_RDONLY) if fs is not None else open_nofollow(path, os.O_RDONLY)


LINE_ERRORS = (canon.HashError, schema.SchemaError, ValueError, KeyError, TypeError, RecursionError)


def parse_line(line: bytes, seq: int, kind: str, *, validate: bool = True) -> tuple[dict[str, Any], str]:
    """One log line (with its LF) as the store reads it: strict ``cj`` parse, the event schema of ``kind``
    (``"workspace"`` or ``"ticket"``) and the expected ``seq``. Returns ``(event, head)``; raises one of
    :data:`LINE_ERRORS`. The production line parser: every log read and the reader KAT (``orch.identity.reader``)
    use it, so a KAT result is what a real read gives."""
    e = canon.parse_event_line(line)
    if validate:
        schema.validate("event", e, log=kind)
    if e.get("seq") != seq:
        raise ValueError(f"seq is {e.get('seq')!r}, expected {seq}")
    return e, canon.event_head(e)


def read_new_lines(info: LogInfo, *, validate: bool = True, fs: RootFS | None = None) -> list[dict[str, Any]]:
    """Parse the lines of ``info.path`` after ``info.offset``; extend ``info`` with them; return the events.

    Stops at the first line that is not a good event, setting ``info.error`` (a torn final line without LF included).
    """
    out: list[dict[str, Any]] = []
    try:
        if fs.is_link(info.path) if fs is not None else os.path.islink(info.path):
            info.error, info.error_seq = "the log is a symlink", info.seq + 1
            return []
        fd = _open_ro(info.path, fs)
    except FileNotFoundError:
        return []
    except (NotRegularFile, IsADirectoryError):  # a FIFO or a device is never read (it would block under the lock)
        info.error, info.error_seq = "the log is not a regular file", info.seq + 1
        return []
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        info.ino, info.seen_size = st.st_ino, st.st_size
        info.seen_times = (st.st_mtime_ns, st.st_ctime_ns)
        f.seek(info.offset)
        while info.error is None:
            # one line at a time, never more than a line's limit: a file of any size cannot make us allocate more
            line = f.readline(MAX_LINE + 1)
            if not line:
                break
            seq = info.seq + 1
            if len(line) > MAX_LINE:
                info.error, info.error_seq = f"line longer than {MAX_LINE} bytes", seq
                break
            if not line.endswith(b"\n"):
                info.error, info.error_seq = "the last line has no LF (a torn append)", seq
                break
            try:
                e, head = parse_line(line, seq, info.kind, validate=validate)
            except LINE_ERRORS as err:
                info.error, info.error_seq = f"{type(err).__name__}: {err}"[:300], seq
                break
            out.append(e)
            info.starts.append(info.offset)
            info.accs.append(canon.log_acc(info.acc, head))
            info.heads.append(head)
            info.seq = seq
            info.offset += len(line)
    return out


def file_size(path: Path) -> int:
    try:
        return os.stat(path).st_size
    except FileNotFoundError:
        return 0


def stat_sig(path: Path, fs: RootFS | None = None) -> tuple[int, int, int, int] | None:
    """``(size, inode, mtime_ns, ctime_ns)`` of a log, or None if it is missing; a symlink has its own signature
    (never a file's). The times make a log that was replaced by another file of the same size and inode number (or
    edited in place without a size change) differ from the one that was verified."""
    try:
        st = fs.lstat(path) if fs is not None else os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except StoreError:  # a symlinked directory on the way (the pinned root refuses it): never a log's signature
        return (-1, -1, -1, -1)
    if stat.S_ISLNK(st.st_mode):
        return (-1, -1, -1, -1)
    return (st.st_size, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


def info_sig(info: LogInfo | None) -> tuple[int, int, int, int] | None:
    """The signature the store recorded when it read (or wrote) ``info``'s log, comparable with :func:`stat_sig`."""
    if info is None or info.ino is None:
        return None
    return (info.seen_size, info.ino, *info.seen_times)


def last_line(path: Path, fs: RootFS | None = None) -> bytes | None:
    """The last complete line of a log (with its LF), read from the end; None if there is none. Unverified: used only
    as a hint (see ``Store._hints``)."""
    try:
        fd = _open_ro(path, fs)
    except (OSError, StoreError):
        return None
    with os.fdopen(fd, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        end, chunk, buf = size, 8192, b""
        while end > 0:
            start = max(0, end - chunk)
            f.seek(start)
            buf = f.read(end - start) + buf
            end = start
            body = buf[:-1] if buf.endswith(b"\n") else None
            if body is None:
                return None
            k = body.rfind(b"\n")
            if k >= 0:
                return body[k + 1 :] + b"\n"
            if len(buf) > MAX_LINE + 1:
                return None
        return buf if buf.endswith(b"\n") else None


def first_line(path: Path, fs: RootFS | None = None) -> bytes | None:
    try:
        fd = _open_ro(path, fs)
    except (OSError, StoreError):
        return None
    with os.fdopen(fd, "rb") as f:
        line = f.readline(MAX_LINE + 1)
    return line if line.endswith(b"\n") and len(line) <= MAX_LINE else None

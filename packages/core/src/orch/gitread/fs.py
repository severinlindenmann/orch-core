"""Pinned directories and the one way the reader opens a file (F7: "every open stays under the pins, with
``O_NOFOLLOW`` and a regular-file check").

A :class:`DirPin` names a directory by its real path, device and inode, taken once at registration. :func:`open_pin`
opens the real path and refuses unless the device and inode are still the pinned ones. Below a pinned directory,
everything is opened **relative to a directory fd, one component at a time, with ``O_NOFOLLOW``**, through
:func:`_openat`, the only relative ``os.open`` in the reader. The child's audit hook (:mod:`orch.gitread.sandbox`)
recognises that function by its code object and checks the logical path it carries against the profile's pins.

Every failure is :class:`~orch.gitread.errors.Unavailable`: a symlink (``ELOOP``), a missing file, a FIFO, a file over
its size cap or a pin whose inode changed.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from dataclasses import dataclass
from typing import Any

from .errors import Unavailable

__all__ = ["Dir", "DirPin", "RepoPins", "open_pin", "split_rel"]

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_NOCTTY = getattr(os, "O_NOCTTY", 0)


@dataclass(frozen=True)
class DirPin:
    path: str  # a real path (no symlink component), absolute
    dev: int
    ino: int

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "dev": self.dev, "ino": self.ino}

    @classmethod
    def from_json(cls, d: Any) -> DirPin:
        if not isinstance(d, dict) or set(d) != {"path", "dev", "ino"}:
            raise Unavailable("malformed pin")
        p, dev, ino = d["path"], d["dev"], d["ino"]
        if not isinstance(p, str) or not p.startswith("/") or "\0" in p:
            raise Unavailable("malformed pin path")
        if type(dev) is not int or type(ino) is not int:
            raise Unavailable("malformed pin")
        return cls(p, dev, ino)


@dataclass(frozen=True)
class RepoPins:
    """The three pins of one repository: its working copy (the declared ``path``), its gitdir and its
    ``commondir`` (equal to the gitdir unless the gitdir is a linked worktree's)."""

    worktree: DirPin
    gitdir: DirPin
    commondir: DirPin

    def to_json(self) -> dict[str, Any]:
        return {
            "worktree": self.worktree.to_json(),
            "gitdir": self.gitdir.to_json(),
            "commondir": self.commondir.to_json(),
        }

    @classmethod
    def from_json(cls, d: Any) -> RepoPins:
        if not isinstance(d, dict) or set(d) != {"worktree", "gitdir", "commondir"}:
            raise Unavailable("malformed pins")
        return cls(DirPin.from_json(d["worktree"]), DirPin.from_json(d["gitdir"]), DirPin.from_json(d["commondir"]))


def split_rel(rel: str) -> list[str]:
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") or "\0" in p for p in parts):
        raise Unavailable(f"bad relative path {rel!r}")
    return parts


def _openat(dirfd: int, base: str, name: str, flags: int) -> int:
    """The reader's only relative open. ``base`` is the logical path of ``dirfd`` (the sandbox checks it)."""
    if not name or "/" in name or "\0" in name or name in (".", ".."):
        raise Unavailable(f"bad name {name!r}")
    return os.open(name, flags | _NOFOLLOW | _CLOEXEC | _NOCTTY, dir_fd=dirfd)


def _fail(what: str, e: OSError) -> Unavailable:
    if e.errno == errno.ELOOP:
        return Unavailable(f"{what} is a symlink")
    if e.errno == errno.ENOTDIR:  # macOS answers O_DIRECTORY|O_NOFOLLOW on a symlink with ENOTDIR
        return Unavailable(f"{what} is a symlink or not a directory")
    if e.errno == errno.ENOENT:
        return Unavailable(f"{what} does not exist")
    if isinstance(e, PermissionError):
        return Unavailable(f"{what} cannot be opened")
    return Unavailable(f"{what} cannot be read ({errno.errorcode.get(e.errno or 0, 'error')})")


class Dir:
    """An open directory and its logical path. ``close()`` it (or use ``with``)."""

    def __init__(self, fd: int, path: str) -> None:
        self.fd = fd
        self.path = path

    def close(self) -> None:
        if self.fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> Dir:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _child(self, name: str) -> str:
        return self.path.rstrip("/") + "/" + name

    def sub(self, rel: str) -> Dir:
        """The directory ``rel`` below this one, walked with ``O_NOFOLLOW``."""
        cur = self
        for comp in split_rel(rel):
            try:
                fd = _openat(cur.fd, cur.path, comp, os.O_RDONLY | _DIRECTORY)
            except OSError as e:
                if cur is not self:
                    cur.close()
                raise _fail(cur._child(comp), e) from None
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                os.close(fd)
                raise Unavailable(f"{cur._child(comp)} is not a directory")
            nxt = Dir(fd, cur._child(comp))
            if cur is not self:
                cur.close()
            cur = nxt
        return cur

    def open_file(self, rel: str) -> tuple[int, os.stat_result]:
        """A regular file below this directory: ``(fd, stat)``. The caller closes the fd."""
        parts = split_rel(rel)
        parent = self.sub("/".join(parts[:-1])) if len(parts) > 1 else self
        try:
            try:
                fd = _openat(parent.fd, parent.path, parts[-1], os.O_RDONLY | _NONBLOCK)
            except OSError as e:
                raise _fail(parent._child(parts[-1]), e) from None
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                raise Unavailable(f"{parent._child(parts[-1])} is not a regular file")
            os.set_blocking(fd, True)
            return fd, st
        finally:
            if parent is not self:
                parent.close()

    def read(self, rel: str, limit: int) -> bytes:
        """The whole regular file ``rel``; more than ``limit`` bytes is refused."""
        fd, st = self.open_file(rel)
        try:
            if st.st_size > limit:
                raise Unavailable(f"{self._child(rel)} is larger than {limit} bytes")
            chunks, total = [], 0
            while True:
                b = os.read(fd, min(1 << 20, limit + 1 - total))
                if not b:
                    break
                chunks.append(b)
                total += len(b)
                if total > limit:
                    raise Unavailable(f"{self._child(rel)} is larger than {limit} bytes")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def read_optional(self, rel: str, limit: int) -> bytes | None:
        """As :meth:`read`, ``None`` when the name does not exist (any other failure still refuses)."""
        if self.kind(rel) is None:
            return None
        return self.read(rel, limit)

    def kind(self, rel: str) -> str | None:
        """``"dir"``, ``"file"``, ``"link"``, ``"other"`` or ``None`` (absent), by ``lstat`` (no open)."""
        parts = split_rel(rel)
        parent = self.sub("/".join(parts[:-1])) if len(parts) > 1 else self
        try:
            try:
                st = os.stat(parts[-1], dir_fd=parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            except NotADirectoryError:
                return None
            except OSError as e:
                raise _fail(parent._child(parts[-1]), e) from None
        finally:
            if parent is not self:
                parent.close()
        if stat.S_ISDIR(st.st_mode):
            return "dir"
        if stat.S_ISREG(st.st_mode):
            return "file"
        if stat.S_ISLNK(st.st_mode):
            return "link"
        return "other"

    def listdir(self) -> list[str]:
        try:
            return os.listdir(self.fd)
        except OSError as e:
            raise _fail(self.path, e) from None


def abs_parts(path: str) -> list[str]:
    """The components of an absolute path, refused unless it is lexically clean (no ``.``, ``..`` or empty part)."""
    if not path.startswith("/") or "\0" in path:
        raise Unavailable(f"not an absolute path: {path!r}")
    if path == "/":
        return []
    parts = path.split("/")[1:]
    if any(p in ("", ".", "..") for p in parts):  # a trailing slash is an empty last component: refused too
        raise Unavailable(f"not a clean absolute path: {path!r}")
    return parts


def open_walk(path: str) -> tuple[Dir, os.stat_result]:
    """An absolute directory, walked from ``/`` **one component at a time with ``O_NOFOLLOW``** (every component, not
    only the last). A path that is checked and then swapped for a symlink anywhere along it is refused (``ELOOP``)
    instead of followed, so the directory opened is the one at that lexical path. Used for pins and pinning."""
    parts = abs_parts(path)
    try:
        fd = os.open("/", os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    except OSError as e:
        raise _fail("/", e) from None
    root = Dir(fd, "/")
    if not parts:
        return root, os.fstat(root.fd)
    try:
        d = root.sub("/".join(parts))
    finally:
        root.close()
    return d, os.fstat(d.fd)


def open_pin(pin: DirPin) -> Dir:
    """The pinned directory, walked as :func:`open_walk` does, refused unless its device and inode (``fstat`` of the
    opened fd) are still the pinned ones. Everything below it is then opened relative to this fd."""
    d, st = open_walk(pin.path)
    if (st.st_dev, st.st_ino) != (pin.dev, pin.ino):
        d.close()
        raise Unavailable(f"{pin.path} is not the pinned directory any more")
    return d

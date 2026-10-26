"""Unpack a verified npm tarball the way orch does it (spec 8.2, design G4 section 2): NOT ``npm``.

The tarball was hashed against the lock's sha512 before anything opens it. What is unpacked is judged member by member:
regular files and directories only (a symbolic link, hard link, device, pipe or sparse member is ``link``); the first
path component is dropped (``package/``); no absolute path, ``..``, ``.``, empty or backslash segment, no control
character; no ``node_modules`` segment inside a tarball (bundled code is code the lock does not list); no two names that
differ only in case; limits on entries, file size and total size. Files are created ``O_EXCL`` and ``O_NOFOLLOW`` below
a directory descriptor, with normalized modes (0644, 0755 when any execute bit was set). No script runs, no ``.bin``
link is made, npm is never started. ``dest_fd=None`` judges the tarball without writing (the check at stage time).
"""

from __future__ import annotations

import contextlib
import os
import re
import tarfile
import zlib
from dataclasses import dataclass
from typing import Any

from orch.addons.envtree import MAX_DEPTH, MAX_PATH, Budget, TreeError

__all__ = ["MAX_FILE", "MAX_HEADER", "Unpacked", "unpack"]

MAX_FILE = 64 << 20
MAX_HEADER = 8 << 10  # a PAX or long-name header body (the path of one member): a few KiB is plenty
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class Unpacked:
    files: int
    bytes: int
    compiled: bool  # a ``*.node`` file: native code


def _rel(name: str) -> list[str]:
    """The checked path parts of a member below its first component (``[]`` for the root itself)."""
    if not name or name.startswith("/") or "\\" in name or _CONTROL.search(name) or len(name) > MAX_PATH:
        raise TreeError("path_escape")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise TreeError("path_escape") from None
    parts = name.rstrip("/").split("/")
    if "" in parts or "." in parts or ".." in parts:
        raise TreeError("path_escape")
    rest = parts[1:]  # the first component ("package") is dropped
    if len(rest) > MAX_DEPTH:
        raise TreeError("path_escape")
    if any(p.casefold() == "node_modules" for p in rest):
        raise TreeError("package")  # bundled code is not in the lock
    return rest


class _Gunzip:
    """The decompressed stream of a gzip file, read in bounded pieces and drawn from the shared :class:`Budget` BEFORE
    each piece is kept: a gzip bomb is stopped after about the budget, never after it has been inflated into memory,
    and the next tarball of the same environment finds what this one used already gone. ``tarfile`` is given this
    object, not the file."""

    def __init__(self, fd: int, budget: Budget) -> None:
        self._fd = fd
        self._budget = budget
        self._d = zlib.decompressobj(wbits=31)  # gzip framing
        self._tail = b""
        self._eof = False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0 or n > 1 << 20:
            n = 1 << 20  # never inflate more than a megabyte per call
        got = bytearray()
        while len(got) < n and not self._eof:
            raw = self._tail or os.read(self._fd, 1 << 16)
            if not raw and not self._d.unconsumed_tail:
                if not self._d.eof:
                    raise TreeError("package")  # truncated
                self._eof = True
                break
            piece = self._d.decompress(raw, min(n - len(got), self._budget.bytes + 1))  # never more than is left, +1
            self._tail = self._d.unconsumed_tail
            self._budget.take(len(piece))
            got += piece
            if self._d.eof:
                self._eof = True
        return bytes(got)


class _Info(tarfile.TarInfo):
    """A member header whose extension bodies (PAX, GNU long name) are capped before ``tarfile`` reads them."""

    def _cap(self) -> None:
        if self.size > MAX_HEADER:
            raise TreeError("too_large")

    def _proc_gnulong(self, tarfile: Any) -> Any:
        self._cap()
        return super()._proc_gnulong(tarfile)

    def _proc_pax(self, tarfile: Any) -> Any:
        self._cap()
        return super()._proc_pax(tarfile)

    def _proc_sparse(self, tarfile: Any) -> Any:
        raise TreeError("link")  # sparse members are never accepted


def unpack(fd: int, dest_fd: int | None, budget: Budget) -> Unpacked:
    """Unpack the gzip tarball open at ``fd`` below the directory ``dest_fd`` (or only judge it when ``None``).
    ``budget`` is shared by every archive of one environment (decompressed bytes and members). Raises
    :class:`orch.addons.envtree.TreeError` (``link``, ``path_escape``, ``too_large``, ``package``)."""
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()
    folded: dict[str, str] = {}  # casefolded full path -> path as written
    chain = _Chain(dest_fd)
    total = 0
    compiled = False
    f = os.fdopen(os.dup(fd), "rb")
    try:
        with tarfile.open(fileobj=_Gunzip(f.fileno(), budget), mode="r|", tarinfo=_Info) as tar:
            for m in tar:
                budget.take(n_entries=1)  # every member counts, the root entry and skipped ones too
                if m.sparse is not None or any(k.startswith("GNU.sparse") for k in m.pax_headers):
                    raise TreeError("link")  # tarfile would expand the holes into bytes nobody drew from the budget
                if m.isdir():
                    kind = "d"
                elif m.isreg():
                    kind = "f"
                else:
                    raise TreeError("link")
                parts = _rel(m.name)
                if not parts:
                    continue  # the root directory entry
                if kind == "f":
                    if m.size > MAX_FILE:
                        raise TreeError("too_large")
                    total += m.size  # the bytes themselves were drawn from the budget as they were decompressed
                rel = "/".join(parts)
                for i in range(1, len(parts) + (1 if kind == "d" else 0)):  # every directory above (and this one)
                    d = "/".join(parts[:i])
                    if d in seen_files:
                        raise TreeError("package")
                    key = d.casefold()
                    if folded.setdefault(key, d) != d:
                        raise TreeError("package")
                    seen_dirs.add(d)
                if kind == "d":
                    chain.dir(parts)
                    continue
                key = rel.casefold()
                if rel in seen_files or rel in seen_dirs or folded.setdefault(key, rel) != rel:
                    raise TreeError("package")
                seen_files.add(rel)
                if parts[-1].lower().endswith(".node"):
                    compiled = True
                data = tar.extractfile(m)
                if data is None:
                    raise TreeError("link")
                if dest_fd is None:
                    while data.read(1 << 16):
                        pass
                    continue
                parent = chain.dir(parts[:-1])
                out = os.open(
                    parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent
                )
                try:
                    written = 0
                    while chunk := data.read(1 << 16):
                        written += len(chunk)
                        if written > m.size:
                            raise TreeError("package")
                        _write_all(out, chunk)
                    os.fchmod(out, 0o755 if m.mode & 0o111 else 0o644)
                finally:
                    os.close(out)
    except TreeError:
        raise
    except (tarfile.TarError, EOFError, zlib.error, OSError, ValueError):
        raise TreeError("package") from None
    finally:
        f.close()
        chain.close()
    return Unpacked(len(seen_files), total, compiled)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


class _Chain:
    """The directories of the member being written: only the CURRENT ancestor chain is held open (at most MAX_DEPTH
    descriptors, never one per directory of the archive); a member in another directory closes what it does not share
    and opens the rest below the destination. Nothing is followed: every open is ``O_NOFOLLOW | O_DIRECTORY``."""

    def __init__(self, root: int | None) -> None:
        self.root = root
        self.parts: list[str] = []
        self.fds: list[int] = []

    def dir(self, parts: list[str]) -> int:
        """Descriptor of ``dest/<parts>``, creating each missing directory (mode 0755)."""
        if self.root is None:
            return -1
        k = 0
        while k < len(self.parts) and k < len(parts) and self.parts[k] == parts[k]:
            k += 1
        while len(self.fds) > k:
            os.close(self.fds.pop())
            self.parts.pop()
        fd = self.fds[-1] if self.fds else self.root
        for part in parts[k:]:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o755, dir_fd=fd)
            nxt = os.open(part, _DIR, dir_fd=fd)
            os.fchmod(nxt, 0o755)
            self.fds.append(nxt)
            self.parts.append(part)
            fd = nxt
        return fd

    def close(self) -> None:
        while self.fds:
            os.close(self.fds.pop())
        self.parts.clear()

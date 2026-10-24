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

import os
import re
import tarfile
import zlib
from dataclasses import dataclass
from typing import Any

from orch.addons.envtree import MAX_BYTES, MAX_DEPTH, MAX_ENTRIES, MAX_PATH, TreeError

__all__ = ["MAX_FILE", "MAX_HEADER", "Unpacked", "unpack"]

MAX_FILE = 64 << 20
MAX_HEADER = 8 << 10  # a PAX or long-name header body (the path of one member): a few KiB is plenty
HEADER_ALLOWANCE = 2048  # per member: its header block, padding and a PAX header, on top of the file bytes
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
    """The decompressed stream of a gzip file, read in bounded pieces and COUNTED: once more than the limit has come out
    (``MAX_BYTES`` of file data plus a small allowance per member), :class:`TreeError` ``too_large`` is raised, so a
    gzip bomb is stopped after about the limit, never after it has been inflated into memory. ``tarfile`` is given
    this object, not the file."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._d = zlib.decompressobj(wbits=31)  # gzip framing
        self._tail = b""
        self._out = 0
        self._eof = False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0 or n > 1 << 20:
            n = 1 << 20  # never inflate more than a megabyte per call
        limit = MAX_BYTES + MAX_ENTRIES * HEADER_ALLOWANCE
        got = bytearray()
        while len(got) < n and not self._eof:
            raw = self._tail or os.read(self._fd, 1 << 16)
            if not raw and not self._d.unconsumed_tail:
                if not self._d.eof:
                    raise TreeError("package")  # truncated
                self._eof = True
                break
            piece = self._d.decompress(raw, n - len(got))
            self._tail = self._d.unconsumed_tail
            self._out += len(piece)
            if self._out > limit:
                raise TreeError("too_large")
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


def unpack(fd: int, dest_fd: int | None) -> Unpacked:
    """Unpack the gzip tarball open at ``fd`` below the directory ``dest_fd`` (or only judge it when ``None``).
    Raises :class:`orch.addons.envtree.TreeError` (``link``, ``path_escape``, ``too_large``, ``package``)."""
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()
    folded: dict[str, str] = {}  # casefolded full path -> path as written
    opened: dict[str, int] = {"": dest_fd} if dest_fd is not None else {}
    n = total = 0
    compiled = False
    f = os.fdopen(os.dup(fd), "rb")
    try:
        with tarfile.open(fileobj=_Gunzip(f.fileno()), mode="r|", tarinfo=_Info) as tar:
            members = 0
            for m in tar:
                members += 1
                if members > MAX_ENTRIES:  # every member counts, the root entry and skipped ones too
                    raise TreeError("too_large")
                if m.isdir():
                    kind = "d"
                elif m.isreg():
                    kind = "f"
                else:
                    raise TreeError("link")
                parts = _rel(m.name)
                if not parts:
                    continue  # the root directory entry
                n += 1
                if n > MAX_ENTRIES:
                    raise TreeError("too_large")
                if kind == "f":
                    if m.size > MAX_FILE:
                        raise TreeError("too_large")
                    total += m.size
                    if total > MAX_BYTES:
                        raise TreeError("too_large")
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
                    _make_dirs(opened, dest_fd, parts)
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
                parent = _make_dirs(opened, dest_fd, parts[:-1])
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
        for rel, ofd in opened.items():
            if rel and ofd != dest_fd:
                os.close(ofd)
    return Unpacked(len(seen_files), total, compiled)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _make_dirs(opened: dict[str, int], dest_fd: int | None, parts: list[str]) -> int:
    """Descriptor of ``dest/<parts>``, creating each missing directory (mode 0755) without following a link."""
    if dest_fd is None:
        return -1
    cur = ""
    fd = dest_fd
    for part in parts:
        cur = f"{cur}/{part}" if cur else part
        if cur not in opened:
            try:
                os.mkdir(part, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            opened[cur] = os.open(part, _DIR, dir_fd=fd)
            os.fchmod(opened[cur], 0o755)
        fd = opened[cur]
    return fd

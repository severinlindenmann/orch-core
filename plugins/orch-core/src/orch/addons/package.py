"""Addon package reading and the package digest (ticket-format §8.1).

A package is a directory with ``orch-addon.json`` at its top. :func:`read_package` reads it **once** into memory with
strict rules (regular files only, plain ASCII names, size and count caps) and returns the bytes, the listing and the
digest; every later decision (digest, manifest, staging for the runner) uses those bytes, never the directory again.
The directory is walked with ``O_NOFOLLOW`` file descriptors, so a symbolic link swapped in during the read is
refused instead of followed.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass

__all__ = [
    "MANIFEST_FILE",
    "MAX_DEPTH",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_TOTAL_BYTES",
    "Package",
    "PackageError",
    "check_path",
    "package_digest",
    "read_package",
]

MANIFEST_FILE = "orch-addon.json"
MAX_DEPTH = 6
MAX_FILES = 256  # files and directories together
MAX_FILE_BYTES = 4 << 20
MAX_TOTAL_BYTES = 16 << 20
MAX_MANIFEST_BYTES = 64 << 10
REFUSED_SUFFIXES = (".pyc", ".pyo", ".so", ".dylib", ".dll", ".pyd")  # opaque to a reviewing owner (§8.1)
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?![\s\S])")


class PackageError(ValueError):
    """The package breaks a rule of §8.1; the message names the path and the rule, never file content."""


@dataclass(frozen=True)
class Package:
    files: dict[str, bytes]  # relative POSIX path -> content
    digest: str  # sha256:<hex>, §8.1

    @property
    def manifest_bytes(self) -> bytes:
        return self.files[MANIFEST_FILE]


def package_digest(files: dict[str, bytes]) -> str:
    """``sha256:`` + SHA-256 of ``<sha256 hex>  <path>\\n`` lines in byte order of the path (``sha256sum`` style)."""
    if not files:
        raise PackageError("an empty package")
    lines = []
    for path in sorted(files, key=lambda p: p.encode("utf-8")):
        lines.append(f"{hashlib.sha256(files[path]).hexdigest()}  {path}\n")
    return "sha256:" + hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def _check_segment(name: str, shown: str) -> None:
    if name == "__pycache__" or not _SEGMENT.fullmatch(name):
        raise PackageError(
            f"{ascii(shown)}: a file name must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}} and not be __pycache__"
        )


def check_path(rel: str) -> None:
    """Raise :class:`PackageError` unless ``rel`` is a relative POSIX path of valid segments within the depth limit,
    and a file name that is not a compiled object."""
    parts = rel.split("/")
    if len(parts) > MAX_DEPTH:
        raise PackageError(f"{ascii(rel)}: more than {MAX_DEPTH} levels")
    for part in parts:
        _check_segment(part, rel)
    _check_file_name(parts[-1], rel)


def _check_file_name(name: str, shown: str) -> None:
    if name.lower().endswith(REFUSED_SUFFIXES):
        raise PackageError(f"{ascii(shown)}: compiled files ({', '.join(REFUSED_SUFFIXES)}) are not allowed")


def _open_dir(path: str, dir_fd: int | None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, dir_fd=dir_fd)


def read_package(directory: str | os.PathLike[str], *, dir_fd: int | None = None) -> Package:
    """Read the package in ``directory`` (a name relative to ``dir_fd`` when that is given) under the rules of §8.1 or
    raise :class:`PackageError`."""
    root = os.fspath(directory)
    try:
        if stat.S_ISLNK(os.lstat(root, dir_fd=dir_fd).st_mode):
            raise PackageError("the package directory is a symbolic link")
        fd = _open_dir(root, dir_fd)
    except OSError as e:
        raise PackageError(f"cannot open the package directory ({e.strerror or 'error'})") from None
    files: dict[str, bytes] = {}
    total = entries = 0
    try:
        stack = [(fd, "", 1)]
        while stack:
            dfd, prefix, depth = stack.pop()
            try:
                names = sorted(os.listdir(dfd))
                if len({n.casefold() for n in names}) != len(names):
                    raise PackageError(f"{prefix or './'}: two names equal when case is ignored")
                for name in names:
                    shown = prefix + name
                    _check_segment(name, shown)
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    entries += 1
                    if entries > MAX_FILES:
                        raise PackageError(f"more than {MAX_FILES} files and directories")
                    if stat.S_ISDIR(st.st_mode):
                        if depth >= MAX_DEPTH:
                            raise PackageError(f"{shown}: more than {MAX_DEPTH} levels")
                        stack.append((_open_dir(name, dfd), shown + "/", depth + 1))
                    elif stat.S_ISREG(st.st_mode):
                        _check_file_name(name, shown)
                        if st.st_nlink > 1:
                            raise PackageError(f"{shown}: a hard link")
                        if st.st_size > MAX_FILE_BYTES:
                            raise PackageError(f"{shown}: larger than {MAX_FILE_BYTES} bytes")
                        data = _read_file(name, dfd, shown)
                        total += len(data)
                        if total > MAX_TOTAL_BYTES:
                            raise PackageError(f"more than {MAX_TOTAL_BYTES} bytes in all")
                        files[shown] = data
                    else:
                        raise PackageError(f"{shown}: not a regular file or directory (symbolic link, device, pipe)")
            except OSError as e:
                raise PackageError(f"{prefix or '.'}: cannot read ({e.strerror or 'error'})") from None
            finally:
                if dfd != fd:
                    os.close(dfd)
    finally:
        os.close(fd)
        for dfd, _, _ in stack:
            if dfd != fd:
                os.close(dfd)
    if MANIFEST_FILE not in files:
        raise PackageError(f"no {MANIFEST_FILE} at the top of the package")
    if len(files[MANIFEST_FILE]) > MAX_MANIFEST_BYTES:
        raise PackageError(f"{MANIFEST_FILE}: larger than {MAX_MANIFEST_BYTES} bytes")
    return Package(files, package_digest(files))


def _read_file(name: str, dfd: int, shown: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(name, flags, dir_fd=dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise PackageError(f"{shown}: not a plain file")
        with os.fdopen(os.dup(fd), "rb") as f:
            data = f.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise PackageError(f"{shown}: larger than {MAX_FILE_BYTES} bytes")
    return data

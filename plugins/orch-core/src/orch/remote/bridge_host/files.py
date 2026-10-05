"""Where the bridge keeps its records, and how it writes and reads them.

Everything lives in the orch config directory, under its permits folder: `permits/bridge/<workspace hex>/` holds the
host signing key, the device registry, the audit log, the request store and the per-device sequence state, and the
channel key when the caller keeps one there. Never in the workspace. The caller passes the config directory in, so
nothing here decides it.

The operating-system user is shared with every agent on the computer, so file modes protect nothing against a local
agent: orch's command guard, which refuses agents' commands and file-tool calls on the permits folder, is the only
barrier (spec §2.7). The guard reads command text, so it deters careless or accidental access and does not stop a
program that assembles the path at run time, or a process outside the agent's tools. Modes are still owner-only
(0700 directories, 0600 files) against other users.

Writes are whole-file and atomic: a new record is created exclusively (O_EXCL, so two writers never both succeed),
a change replaces the file through a temporary file and a rename, and both are flushed to disk with their directory
before the caller relies on them. Reads never follow a link and refuse anything but a plain file with one link of
bounded size: such a file is Damaged, and every caller treats Damaged as the safe answer.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

_WS = re.compile(r"[0-9a-f]{32}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class Damaged(Exception):
    """A record exists but cannot be trusted (not a plain file, a link, too large, unreadable or invalid)."""


def bridge_dir(config_dir: Path, workspace_hex: str) -> Path:
    if not isinstance(workspace_hex, str) or not _WS.fullmatch(workspace_hex):
        raise ValueError("a workspace id is 32 lower-case hex characters")
    return Path(config_dir) / "permits" / "bridge" / workspace_hex


def ensure_dir(path: Path) -> Path:
    """`path` as an owner-only directory that is not a link (Damaged otherwise)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise Damaged(f"{path.name} is not a directory")
    return path


def _sync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | _CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]
    os.fsync(fd)


def create_exclusive(path: Path, data: bytes) -> None:
    """Create `path` with `data`; FileExistsError when it already exists. Durable when this returns."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        _write_all(fd, data)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    os.close(fd)
    _sync_dir(path.parent)


def replace(path: Path, data: bytes) -> None:
    """Replace (or create) `path` with `data` atomically. Durable when this returns."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        _write_all(fd, data)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _sync_dir(path.parent)


def append(path: Path, data: bytes) -> None:
    """Append `data` to `path` (created owner-only), flushed to disk."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise Damaged(f"{path.name} is not a plain file")
        _write_all(fd, data)
    finally:
        os.close(fd)


def read(path: Path, limit: int) -> bytes | None:
    """The bytes of `path`; None when it does not exist; Damaged for a link, anything but a plain file with one
    link, a file over `limit` bytes, or a read error."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | _NOFOLLOW | _CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Damaged(f"{path.name} cannot be opened") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > limit:
            raise Damaged(f"{path.name} is not a plain file of at most {limit} bytes")
        chunks, left = [], limit + 1
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    except OSError as e:
        raise Damaged(f"{path.name} cannot be read") from e
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > limit:
        raise Damaged(f"{path.name} grew past {limit} bytes")
    return data

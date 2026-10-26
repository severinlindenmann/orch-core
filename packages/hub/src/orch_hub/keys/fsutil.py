"""Fail-closed file access for the hub state dir: modes and owner are checked on every read, writes are atomic.

SHORTCUT (hardening #909): keys are 0600 files in a 0700 directory owned by the running user. A directory or file that
is looser, owned by another UID, a symlink, or of the wrong type is refused, never repaired.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


class StateError(Exception):
    """Raised with a stable ``code``; the message never contains file contents."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def check_dir(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise StateError("missing", path.name) from None
    except OSError as e:
        raise StateError("unreadable", path.name) from e
    if not stat.S_ISDIR(st.st_mode):
        raise StateError("bad_state_dir", f"{path.name} is not a directory")
    if st.st_uid != os.geteuid():
        raise StateError("bad_owner", path.name)
    if st.st_mode & 0o077:
        raise StateError("bad_mode", f"{path.name} must be 0700")


def fsync_dir(path: Path) -> None:
    dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def make_dir(path: Path) -> None:
    """Create one directory as 0700 (the parent must exist) and fsync the parent, so the entry is durable before
    anything is committed that refers to it. A pre-existing path is checked (lstat) before any chmod."""
    try:
        os.mkdir(path, DIR_MODE)
    except FileExistsError:
        check_dir(path)  # lstat: refuses a symlink or foreign owner before we touch its mode
    os.chmod(path, DIR_MODE)  # the umask may have narrowed it, never widens
    check_dir(path)
    fsync_dir(path.parent)


def read_file(path: Path) -> bytes:
    """Read a state file; refuses symlinks, other owners, any group/other access bit and non-regular files."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise StateError("missing", path.name) from None
    except OSError as e:
        raise StateError("unreadable", path.name) from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StateError("bad_file", path.name)
        if st.st_uid != os.geteuid():
            raise StateError("bad_owner", path.name)
        if st.st_mode & 0o177:
            raise StateError("bad_mode", f"{path.name} must be 0600")
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read()
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes) -> None:
    """Temp file (0600, exclusive) in the same directory, fsync, rename over the target, fsync the directory.

    On any failure the temp file is removed and the old file, if any, is untouched."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(path.parent)

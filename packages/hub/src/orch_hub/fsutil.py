"""One place that opens the hub's SQLite files (registry.db, access.db, later route-queue.db): private from the first byte.

* absent: created with ``O_CREAT|O_EXCL|O_NOFOLLOW`` and mode 0600 (never "create wide, chmod later");
* present: the path must not be a symlink, and the file (checked by ``fstat`` on an ``O_NOFOLLOW`` descriptor) must be
  a regular file owned by the running user with no group/world bits; anything else is refused, never repaired;
* the parent directory must be owned by the running user and not group/world-writable;
* SQLite takes the mode of the main file for its ``-journal``/``-wal``/``-shm`` files; the connection is opened under
  ``umask 0o077`` as well, and leftover sidecar files are checked with the same rules.

Failure raises :class:`PrivateFileError` (code ``bad_private_file``); callers fail closed.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading

SIDECARS = ("-journal", "-wal", "-shm")
_umask_lock = threading.Lock()


class PrivateFileError(ValueError):
    code = "bad_private_file"

    def __init__(self, detail: str) -> None:
        super().__init__(f"bad_private_file: {detail}")


def _check_fd(fd: int, what: str) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise PrivateFileError(f"{what} is not a regular file")
    if st.st_uid != os.geteuid():
        raise PrivateFileError(f"{what} is owned by another user")
    if st.st_mode & 0o077:
        raise PrivateFileError(f"{what} must be 0600")


def _check_existing(path: str, what: str) -> None:
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise PrivateFileError(f"{what} is a symlink")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise PrivateFileError(f"{what} is not readable as a plain file") from None
    try:
        _check_fd(fd, what)
    finally:
        os.close(fd)


def _check_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    try:
        st = os.stat(parent)
    except OSError:
        raise PrivateFileError("parent directory is not readable") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise PrivateFileError("parent directory must be owned by the running user and not group/world-writable")


def open_private_db(path: str, **kw: object) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite file at ``path`` under the rules above. ``:memory:`` passes through."""
    if path == ":memory:":
        return sqlite3.connect(path, **kw)  # type: ignore[arg-type]
    path = os.fspath(path)
    _check_parent(path)
    try:
        os.lstat(path)
        present = True
    except FileNotFoundError:
        present = False
    except OSError:
        raise PrivateFileError("path is not readable") from None
    if present:
        _check_existing(path, "database")
    else:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            raise PrivateFileError("cannot create the database file") from None
        try:
            _check_fd(fd, "database")
        finally:
            os.close(fd)
    for suffix in SIDECARS:
        if os.path.lexists(path + suffix):
            _check_existing(path + suffix, "database" + suffix)
    with _umask_lock:
        old = os.umask(0o077)
        try:
            return sqlite3.connect(path, **kw)  # type: ignore[arg-type]
        finally:
            os.umask(old)

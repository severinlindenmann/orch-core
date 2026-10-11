"""Checked Unix listener filesystem lifecycle."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import re
import socket
import stat
import sys
import threading
from pathlib import Path
from typing import Literal

from ._unix import CheckedDirectory, UnsafePath, check_acl

SocketKind = Literal["workspace", "control", "worker"]


def check_socket_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts or len(str(path).encode("utf-8")) > 100:
        raise UnsafePath("Socket path must be absolute and at most 100 UTF-8 bytes")


def default_run_directory(host_uid: int) -> Path:
    if sys.platform == "darwin":
        return Path("/Users/Shared/orch/run") / str(host_uid)
    if sys.platform == "linux":
        return Path("/run/orch") / str(host_uid)
    raise UnsafePath("Unix transport is unsupported on this platform")


class Listener:
    """Own the locked, pinned directory and bound socket until close()."""

    def __init__(self, run: Path, name: str, *, host_uid: int, gid: int, kind: SocketKind = "workspace"):
        if kind not in {"workspace", "control", "worker"}:
            raise ValueError("Unknown socket kind")
        if (kind == "workspace" and not re.fullmatch("[0-9a-f]{32}", name)) or (kind != "workspace" and name != kind):
            raise ValueError("Invalid listener directory name")
        self.path = Path(run) / name / "host.sock"
        check_socket_path(self.path)
        if threading.current_thread() is not threading.main_thread() or threading.active_count() != 1:
            raise RuntimeError("Bind listeners before starting any threads (fchdir fallback)")
        self.host_uid, self.kind = host_uid, kind
        self.workspace_id = name if kind == "workspace" else None
        self.sock: socket.socket | None = None
        self.lock_fd: int | None = None
        self.run: CheckedDirectory | None = None
        self.directory: CheckedDirectory | None = None
        self.identity: tuple[int, int] | None = None
        try:
            self.run = CheckedDirectory(Path(run), host_uid, mode=0o711)
            self.directory = CheckedDirectory(
                self.path.parent, host_uid, mode=0o700 if kind == "control" else 0o750, gid=gid
            )
            self.run.recheck()
            self.lock_fd = os.open(
                "lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600,
                dir_fd=self.directory.fd,
            )
            info = os.fstat(self.lock_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != host_uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise UnsafePath("Unsafe socket lock")
            check_acl(self.lock_fd, {0, host_uid})
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._bind(gid)
        except BaseException:
            self.close()
            raise

    def _bind(self, gid: int) -> None:
        assert self.directory is not None
        saved_cwd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        old_umask = os.umask(0o177)
        try:
            os.fchdir(self.directory.fd)
            try:
                info = os.stat("host.sock", follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.host_uid:
                    raise UnsafePath(f"Planted socket: owner {info.st_uid}")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect("host.sock")
                    except OSError as exc:
                        if exc.errno != errno.ECONNREFUSED:
                            raise UnsafePath("Cannot prove socket is stale") from exc
                    else:
                        raise UnsafePath("Socket is already serving")
                os.unlink("host.sock")
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind("host.sock")
            info = os.stat("host.sock", follow_symlinks=False)
            self.identity = info.st_dev, info.st_ino
            os.chown("host.sock", -1, gid, follow_symlinks=False)
            os.chmod("host.sock", 0o660)
            self.sock.listen(128)
            self.sock.setblocking(False)
        finally:
            os.fchdir(saved_cwd)
            os.close(saved_cwd)
            os.umask(old_umask)
        self.recheck()

    def recheck(self) -> None:
        assert self.run is not None and self.directory is not None
        self.run.recheck()
        self.directory.recheck()
        info = os.stat("host.sock", dir_fd=self.directory.fd, follow_symlinks=False)
        if (
            (info.st_dev, info.st_ino) != self.identity
            or not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != self.host_uid
            or info.st_gid != self.directory.gid
            or stat.S_IMODE(info.st_mode) != 0o660
        ):
            raise UnsafePath("Socket inode, owner, group or mode changed")

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.directory is not None:
            if self.identity is not None:
                with contextlib.suppress(FileNotFoundError):
                    info = os.stat("host.sock", dir_fd=self.directory.fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == self.identity:
                        os.unlink("host.sock", dir_fd=self.directory.fd)
            self.directory.close()
            self.directory = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.run is not None:
            self.run.close()
            self.run = None

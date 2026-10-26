"""Checked Unix listener filesystem lifecycle."""

from __future__ import annotations

import contextlib
import ctypes
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
from .pins import AgentShare

SocketKind = Literal["workspace", "control", "worker", "runner"]


#: the longest socket path in UTF-8 bytes (``sun_path`` is 104 bytes on macOS, with room for the NUL)
SOCKET_PATH_LIMIT = 100


def check_socket_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts or len(str(path).encode("utf-8")) > SOCKET_PATH_LIMIT:
        raise UnsafePath("Socket path must be absolute and at most 100 UTF-8 bytes")


#: tier B (spec 13.3): ``<run>`` and every directory shared with the agent user are setgid ``2750`` of the person,
#: group = the agent user's group; sockets inside are ``0660``. Control stays ``0700``, the person's group.
SHARED_MODE = 0o2750


def default_run_directory(host_uid: int) -> Path:
    if sys.platform == "darwin":
        return Path("/Users/Shared/orch/run") / str(host_uid)
    if sys.platform == "linux":
        return Path("/run/orch") / str(host_uid)
    raise UnsafePath("Unix transport is unsupported on this platform")


class Listener:
    """Own the locked, pinned directory and bound socket until close()."""

    def __init__(
        self,
        run: Path,
        name: str,
        *,
        host_uid: int,
        gid: int,
        kind: SocketKind = "workspace",
        share: AgentShare | None = None,
    ):
        if kind not in {"workspace", "control", "worker", "runner"}:
            raise ValueError("Unknown socket kind")
        if (kind == "workspace" and not re.fullmatch("[0-9a-f]{32}", name)) or (kind != "workspace" and name != kind):
            raise ValueError("Invalid listener directory name")
        self.path = Path(run) / name / "host.sock"
        check_socket_path(self.path)
        if share is not None and share.host_uid != host_uid:
            raise ValueError("The agent share belongs to another host UID")
        self.host_uid, self.kind, self.share = host_uid, kind, share
        #: the only UID that may be the runner on a runner socket: the agent user in tier B, else the host UID
        self.runner_uid = host_uid if share is None else share.uid
        shared = share is not None and kind != "control"
        if shared:
            gid = share.gid  # type: ignore[union-attr]
        self.workspace_id = name if kind == "workspace" else None
        self.sock: socket.socket | None = None
        self.lock_fd: int | None = None
        self.run: CheckedDirectory | None = None
        self.directory: CheckedDirectory | None = None
        self.identity: tuple[int, int] | None = None
        try:
            if share is not None:  # made by root (tmpfiles), never by the host: it could not give the group away
                self.run = CheckedDirectory(
                    Path(run), host_uid, mode=SHARED_MODE, gid=share.gid, create=False, strict_acl=True
                )
            else:
                self.run = CheckedDirectory(Path(run), host_uid, mode=0o711)
            if shared:
                self.directory = CheckedDirectory(
                    self.path.parent, host_uid, mode=SHARED_MODE, gid=gid, strict_acl=True
                )
            else:
                self.directory = CheckedDirectory(
                    self.path.parent, host_uid, mode=0o700 if kind in ("control", "runner") else 0o750, gid=gid
                )
            self.run.recheck()
            if share is not None and self.directory.identities[: len(self.run.identities)] != self.run.identities:
                raise UnsafePath("The socket directory is not below the checked run directory")
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
        if sys.platform == "linux" and Path("/proc/self/fd").is_dir():
            self._bind_at(f"/proc/self/fd/{self.directory.fd}/host.sock", gid)
        elif sys.platform == "darwin":
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            change = getattr(lib, "pthread_fchdir_np", None)
            if change is None:
                raise UnsafePath("Binding requires pthread_fchdir_np on macOS")
            change.argtypes, change.restype = [ctypes.c_int], ctypes.c_int
            errors: list[BaseException] = []

            def bind_thread() -> None:
                try:
                    if change(self.directory.fd) != 0:
                        raise OSError(ctypes.get_errno(), "pthread_fchdir_np failed")
                    try:
                        self._bind_at("host.sock", gid)
                    finally:
                        if change(-1) != 0:
                            raise OSError(ctypes.get_errno(), "Cannot restore thread working directory")
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=bind_thread)
            thread.start()
            thread.join()
            if errors:
                raise errors[0]
        else:
            raise UnsafePath("Safe binding requires /proc/self/fd on Linux or pthread_fchdir_np on macOS")
        self.recheck()

    def _bind_at(self, address: str, gid: int) -> None:
        assert self.directory is not None
        fd = self.directory.fd
        try:
            info = os.stat("host.sock", dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.host_uid:
                raise UnsafePath(f"Planted socket: owner {info.st_uid}")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect(address)
                except OSError as exc:
                    if exc.errno != errno.ECONNREFUSED:
                        raise UnsafePath("Cannot prove socket is stale") from exc
                else:
                    raise UnsafePath("Socket is already serving")
            os.unlink("host.sock", dir_fd=fd)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(address)
        info = os.stat("host.sock", dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.host_uid:
            raise UnsafePath("Socket replaced during bind")
        self.identity = info.st_dev, info.st_ino
        os.chown("host.sock", -1, gid, dir_fd=fd, follow_symlinks=False)
        os.chmod("host.sock", 0o660, dir_fd=fd, follow_symlinks=False)
        self.sock.listen(128)
        self.sock.setblocking(False)

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
        if self.share is not None:
            _no_socket_acl(self.directory.fd)

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


def _no_socket_acl(directory_fd: int) -> None:
    """Tier B: an ACL on the socket could open it to another user while the mode bits still read ``0660``."""
    if sys.platform != "linux":
        return
    for name in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            os.getxattr(f"/proc/self/fd/{directory_fd}/host.sock", name, follow_symlinks=False)
        except OSError as exc:
            if exc.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
                continue
            raise UnsafePath("Cannot inspect the socket ACL") from exc
        raise UnsafePath("The shared socket has an ACL")

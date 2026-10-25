"""Shared Unix peer credentials."""

import array
import asyncio
import contextlib
import errno
import os
import signal
import socket
import struct
import sys
from dataclasses import dataclass, field

from . import frames


@dataclass(frozen=True)
class Credentials:
    uid: int
    gid: int
    pid: int
    #: Linux 6.5+ (SO_PEERPIDFD): the PID the kernel's pidfd of the peer names, ``-1`` when the kernel refused the pidfd
    #: or the process is gone (a denial), ``None`` ONLY when the kernel has no such option. Set by :func:`with_pidfd`.
    pidfd_pid: int | None = field(default=None, compare=False)
    #: the open pidfd (``pidfd_pid`` > 0). The caller closes it after the decision, not before: the identity check
    #: reads it again after it has asked the service manager.
    pidfd: int | None = field(default=None, compare=False)


SO_PEERPIDFD = 77


def with_pidfd(sock: socket.socket, credentials: Credentials) -> Credentials:
    """``credentials`` plus the peer's pidfd and the PID it names (a pidfd cannot be redirected to a reused PID).

    Only ``ENOPROTOOPT`` (a kernel before 6.5) leaves ``pidfd_pid`` unset, which makes the caller use the weaker start
    time check. Every other failure is a denial (``-1``): a peer that has already exited makes the kernel refuse the
    pidfd (EINVAL, later ESRCH), and that is exactly the case where its PID may be reused."""
    if sys.platform != "linux":
        return credentials
    try:
        fd = struct.unpack("i", sock.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD, 4))[0]
    except OSError as exc:
        if exc.errno == errno.ENOPROTOOPT:
            return credentials
        return Credentials(credentials.uid, credentials.gid, credentials.pid, -1)
    pid = pidfd_pid_now(fd)
    if pid < 1:
        os.close(fd)
        return Credentials(credentials.uid, credentials.gid, credentials.pid, -1)
    return Credentials(credentials.uid, credentials.gid, credentials.pid, pid, fd)


def pidfd_pid_now(fd: int) -> int:
    """The PID ``fd`` names now (``Pid:`` of its fdinfo, written by the kernel); ``-1``: gone or unreadable."""
    try:
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("Pid:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return -1


def pidfd_alive(fd: int) -> bool:
    """The process behind ``fd`` still exists (signal 0 through the pidfd where Python has it)."""
    send = getattr(signal, "pidfd_send_signal", None)
    if send is None:
        return pidfd_pid_now(fd) > 0
    try:
        send(fd, 0)
    except OSError:
        return False
    return True


def close_pidfd(credentials: Credentials) -> None:
    if credentials.pidfd is not None:
        with contextlib.suppress(OSError):
            os.close(credentials.pidfd)


def peer_credentials(sock: socket.socket) -> Credentials:
    if sys.platform == "linux":
        pid, uid, gid = struct.unpack("iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return Credentials(uid, gid, pid)
    if sys.platform == "darwin":
        # struct xucred: unsigned version, uid_t uid, short ngroups, gid_t groups[16].
        raw = sock.getsockopt(0, 1, 76)  # SOL_LOCAL, LOCAL_PEERCRED
        version, uid, groups, gid = struct.unpack_from("@IIh2xI", raw)
        if version != 0 or not 1 <= groups <= 16:
            raise OSError("Invalid LOCAL_PEERCRED")
        pid = struct.unpack("i", sock.getsockopt(0, 2, 4))[0]  # LOCAL_PEERPID
        return Credentials(uid, gid, pid)
    raise OSError("Unix peer credentials are unsupported")


def recv(sock: socket.socket, size: int = 65536) -> bytes:
    """Reject ancillary data after closing all delivered SCM_RIGHTS descriptors.

    Darwin installs rights even on truncation. 4096 bytes exceeds its supported
    rights payload, so ordinary rights messages are received in full.
    """
    data, ancillary, flags, _ = sock.recvmsg(size, 4096)
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            rights = array.array("i")
            rights.frombytes(payload[: len(payload) - len(payload) % rights.itemsize])
            for fd in rights:
                with contextlib.suppress(OSError):
                    os.close(fd)
    if ancillary or flags & socket.MSG_CTRUNC:
        raise frames.FrameError(message="Ancillary data is forbidden")
    return data


async def receive(sock: socket.socket) -> bytes:
    """One nonblocking recvmsg; only cancel when the connection is closing."""
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def readable() -> None:
        if ready.done():
            return
        try:
            data = recv(sock)
        except (BlockingIOError, InterruptedError):
            return
        except Exception as exc:
            ready.set_exception(exc)
        else:
            ready.set_result(data)

    loop.add_reader(sock.fileno(), readable)
    try:
        return await ready
    finally:
        loop.remove_reader(sock.fileno())

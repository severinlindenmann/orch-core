"""Shared Unix peer credentials."""

import array
import asyncio
import contextlib
import os
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
    #: Linux 6.5+ (SO_PEERPIDFD): the PID the kernel's pidfd of the peer names now, ``-1`` when that process has exited,
    #: ``None`` when the kernel cannot say. Only :func:`with_pidfd` sets it.
    pidfd_pid: int | None = field(default=None, compare=False)


SO_PEERPIDFD = 77


def with_pidfd(sock: socket.socket, credentials: Credentials) -> Credentials:
    """``credentials`` plus the peer's PID as its pidfd names it (a pidfd cannot be redirected to a reused PID)."""
    if sys.platform != "linux":
        return credentials
    try:
        fd = struct.unpack("i", sock.getsockopt(socket.SOL_SOCKET, SO_PEERPIDFD, 4))[0]
    except OSError:
        return credentials  # an older kernel: no pidfd, callers fall back to the start time
    try:
        pid = None
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("Pid:"):
                    pid = int(line.split()[1])
    except (OSError, ValueError):
        pid = None
    finally:
        os.close(fd)
    return Credentials(credentials.uid, credentials.gid, credentials.pid, -1 if pid is None else pid)


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

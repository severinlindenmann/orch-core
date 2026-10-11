"""Shared Unix peer credentials."""

import socket
import struct
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Credentials:
    uid: int
    gid: int
    pid: int


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

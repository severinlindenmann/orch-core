"""Strict v1 frames; serialized requests (one in flight) with interleaved events.

The socket is authenticated against a local registry pin before hello. No automatic
write retries: callers must keep their original idem for outcome recovery.
"""

import ctypes
import json
import math
import os
import socket
import struct
import threading
from collections import deque
from typing import Any

from .errors import HostError, fail

MAX_FRAME = 2_097_152
CAPS = ["ops/1", "query/1", "sub/1", "human/1", "diff/1", "outcome/1"]
OPS = {
    name: 1
    for name in (
        "host.list",
        "whoami",
        "workspace.show",
        "host.status",
        "member.list",
        "addon.list",
        "needs",
        "ticket.query",
        "sub.open",
        "sub.close",
        "outcome.get",
    )
}


def peer_uid(sock: socket.socket) -> int:
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
        raise OSError(ctypes.get_errno(), "getpeereid failed")
    return uid.value


def check_uid(sock: socket.socket, expected: int):
    if peer_uid(sock) != expected:
        raise fail("peer.denied", exit=3)


def _pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("duplicate key")
        result[k] = v
    return result


def _valid(value, depth=0):
    if depth > 32:
        raise ValueError("depth")
    if isinstance(value, int) and abs(value) > 2**53:
        raise ValueError("integer")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("number")
    if isinstance(value, str):
        value.encode("utf-8")
    if isinstance(value, dict | list):
        for v in value.values() if isinstance(value, dict) else value:
            _valid(v, depth + 1)
        if isinstance(value, dict):
            for k in value:
                _valid(k, depth + 1)


def encode(frame: dict) -> bytes:
    try:
        if not isinstance(frame, dict):
            raise ValueError("object required")
        _valid(frame)
        raw = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, UnicodeError, RecursionError):
        raise fail("frame.invalid", exit=6) from None
    if len(raw) > MAX_FRAME:
        raise fail("frame.too_large", exit=6)
    return struct.pack("!I", len(raw)) + raw


def _exact(sock, n):
    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError("host closed connection")
        data.extend(chunk)
    return bytes(data)


def receive(sock) -> dict:
    n = struct.unpack("!I", _exact(sock, 4))[0]
    if not 1 <= n <= MAX_FRAME:
        raise fail("frame.too_large" if n > MAX_FRAME else "frame.invalid", exit=6)
    try:
        frame = json.loads(_exact(sock, n).decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(frame, dict):
            raise ValueError("object required")
        _valid(frame)
        return frame
    except (ValueError, UnicodeError, RecursionError):
        raise fail("frame.invalid", exit=6) from None


class HostLink:
    def __init__(self, sock, host_uid: int, workspace_id: str | None):
        self.sock, self.lock, self.send_lock = sock, threading.Lock(), threading.Lock()
        self.events = deque(maxlen=256)
        self.serial = 0
        try:
            check_uid(sock, host_uid)
            sock.settimeout(2)
            self.send({"t": "hello", "proto": [1], "client": "orch-dashboard/0.1.0", "caps": CAPS, "ops": OPS})
            w = receive(sock)
            if w.get("t") == "fatal":
                raise HostError(w["error"])
            if w.get("t") != "welcome" or w.get("proto") != 1:
                raise fail("host.version_mismatch", exit=1)
            if (
                not isinstance(w.get("workspace"), dict)
                or not isinstance(w.get("peer"), dict)
                or not isinstance(w.get("caps"), list)
                or not all(isinstance(cap, str) for cap in w["caps"])
                or not isinstance(w.get("ops"), dict)
                or not all(type(version) is int for version in w["ops"].values())
            ):
                raise fail("frame.invalid", exit=6)
            if w.get("host_uid") != host_uid or (workspace_id and w.get("workspace", {}).get("id") != workspace_id):
                raise fail("peer.denied", exit=3)
            peer = w.get("peer", {})
            if peer.get("uid") != os.getuid() or peer.get("class") != "person" or not peer.get("person"):
                raise fail("peer.denied", exit=3)
            self.welcome = w
            sock.settimeout(30)
        except Exception:
            sock.close()
            raise

    @classmethod
    def connect(cls, path, host_uid, workspace_id):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(str(path))
            return cls(sock, host_uid, workspace_id)
        except HostError:
            sock.close()
            raise
        except Exception:
            sock.close()
            raise fail("host.unavailable") from None

    def supports(self, op: str, cap="ops/1") -> bool:
        return cap in self.welcome.get("caps", []) and self.welcome.get("ops", {}).get(op) == OPS.get(op, 1)

    def send(self, frame):
        with self.send_lock:
            self.sock.sendall(encode(frame))

    def cancel(self, request_id):
        self.send({"t": "cancel", "id": request_id})

    def call(self, op: str, args: dict | None = None, *, write=False, idem: str | None = None) -> Any:
        write = write or op not in OPS
        if write and not idem:
            raise ValueError("every write requires its logical call's idem")
        if not self.supports(op):
            raise fail("op.unsupported", exit=1)
        with self.lock:
            self.serial += 1
            rid = f"d{self.serial}"
            req = {"t": "req", "id": rid, "op": op, "args": args or {}}
            if write:
                req["opts"] = {"idem": idem}
            try:
                self.send(req)
                while True:
                    frame = receive(self.sock)
                    if frame.get("t") == "evt":
                        if len(self.events) == self.events.maxlen:
                            self.events.clear()
                            self.events.append({"t": "evt", "invalidated": "overflow"})
                        else:
                            self.events.append(frame)
                        continue
                    if frame.get("t") == "fatal":
                        self.close()
                        raise HostError(frame["error"])
                    if frame.get("t") != "res" or frame.get("id") != rid:
                        self.close()
                        raise fail("frame.invalid", exit=6)
                    if frame.get("ok") is not True:
                        raise HostError(frame["error"], frame["exit"])
                    return frame["result"]["data"]
            except (OSError, EOFError):
                self.close()
                raise fail("outcome.unknown" if write else "host.unavailable") from None
            except HostError as exc:
                if exc.error["code"].startswith("frame."):
                    try:
                        self.send({"t": "fatal", "error": exc.error})
                    except OSError:
                        pass
                    self.close()
                raise

    def outcome(self, idem):
        return self.call("outcome.get", {"idem": idem})

    def close(self):
        self.sock.close()

"""Strict v1.3.1 frames; serialized requests (one in flight) with interleaved events.

The socket is authenticated against a local registry pin before hello. No automatic
write retries: callers must keep their original idem for outcome recovery.
"""

import ctypes
import json
import os
import select
import socket
import struct
import threading
import time
from collections import deque
from typing import Any

from .errors import HostError, fail

MAX_FRAME = 2_097_152
CAPS = ["ops/1", "query/1", "sub/1", "human/1", "diff/1", "outcome/1", "control/1"]
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
        "ticket.get",
        "grant.list",
        "human.get",
        "human.pending",
        "human.prepare",
        "human.submit",
        "human.cancel",
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


def _valid(value, depth=0, count=None):
    count = [0] if count is None else count
    count[0] += 1
    if count[0] > 65536:
        raise ValueError("values")
    if type(value) is int and abs(value) > 2**53 - 1:
        raise ValueError("integer")
    if isinstance(value, float):
        raise ValueError("number")
    if isinstance(value, str):
        value.encode("utf-8")
    if isinstance(value, dict | list):
        if depth >= 32:
            raise ValueError("depth")
        for v in value.values() if isinstance(value, dict) else value:
            _valid(v, depth + 1, count)
        if isinstance(value, dict):
            for k in value:
                k.encode("utf-8")


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


def _exact(sock, n, deadline=None):
    data = bytearray()
    while len(data) < n:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("frame deadline")
            sock.settimeout(remaining)
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError("host closed connection")
        data.extend(chunk)
    return bytes(data)


def receive(sock) -> dict:
    original_timeout = sock.gettimeout()
    try:
        first = _exact(sock, 1)
        deadline = time.monotonic() + 10
        n = struct.unpack("!I", first + _exact(sock, 3, deadline))[0]
        if not 1 <= n <= MAX_FRAME:
            raise fail("frame.too_large" if n > MAX_FRAME else "frame.invalid", exit=6)
        try:
            frame = json.loads(_exact(sock, n, deadline).decode("utf-8"), object_pairs_hook=_pairs)
            if not isinstance(frame, dict):
                raise ValueError("object required")
            _valid(frame)
            return frame
        except (ValueError, UnicodeError, RecursionError):
            raise fail("frame.invalid", exit=6) from None
    finally:
        sock.settimeout(original_timeout)


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
                (not isinstance(w.get("workspace"), dict) if workspace_id else w.get("workspace") is not None)
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
            if (
                peer.get("uid") != os.getuid()
                or (workspace_id and (peer.get("class") != "person" or not peer.get("person")))
                or (not workspace_id and peer.get("class") != "control")
            ):
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

    def call(
        self, op: str, args: dict | None = None, *, write=False, idem: str | None = None, dry_run: bool | None = None
    ) -> Any:
        write = write or op == "human.submit" or op not in OPS
        if dry_run is not None and not (write or op == "human.prepare"):
            raise ValueError("dry_run is only for log writes and human.prepare")
        if op == "human.submit" and (
            not isinstance((args or {}).get("events"), list) or not 1 <= len(args["events"]) <= 16 or "event" in args
        ):
            raise ValueError("human.submit requires events (1..16)")
        if write and not dry_run and not idem:
            raise ValueError("every write requires its logical call's idem")
        cap = (
            "control/1"
            if op == "host.list"
            else "sub/1"
            if op.startswith("sub.")
            else "human/1"
            if op.startswith("human.")
            else "outcome/1"
            if op == "outcome.get"
            else "query/1"
            if op in {"ticket.query", "ticket.get", "grant.list", "needs", "workspace.show", "member.list"}
            else "ops/1"
        )
        if not self.supports(op, cap):
            raise fail("op.unsupported", exit=1)
        with self.lock:
            self.serial += 1
            rid = f"d{self.serial}"
            req = {"t": "req", "id": rid, "op": op, "args": args or {}}
            if write and not dry_run:
                req["opts"] = {"idem": idem}
            if dry_run is not None:
                req.setdefault("opts", {})["dry_run"] = dry_run
            try:
                self.send(req)
                while True:
                    frame = receive(self.sock)
                    if frame.get("t") == "evt":
                        if len(self.events) == self.events.maxlen:
                            self.events.clear()
                            self.events.append({"t": "evt", "end": "overflow", "items": []})
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

    def next_event(self, timeout=0.1):
        """One reader at a time; calls can still consume and queue interleaved events."""
        with self.lock:
            if self.events:
                return self.events.popleft()
            try:
                if self.sock.fileno() < 0:
                    raise fail("host.unavailable")
                if not select.select([self.sock], [], [], timeout)[0]:
                    return None
                frame = receive(self.sock)
                if frame.get("t") == "fatal":
                    self.close()
                    raise HostError(frame["error"])
                if frame.get("t") != "evt":
                    self.close()
                    raise fail("frame.invalid", exit=6)
                return frame
            except (OSError, EOFError, ValueError):
                self.close()
                raise fail("host.unavailable") from None

    def outcome(self, idem):
        return self.call("outcome.get", {"idem": idem})

    def close(self):
        self.sock.close()

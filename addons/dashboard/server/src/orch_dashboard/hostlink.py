"""Strict v1.3.x frames (v1.3.2 fatal conn.overloaded is a retryable HostError); one request in flight.

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
CAPS = [
    "ops/1",
    "query/1",
    "sub/1",
    "human/1",
    "diff/1",
    "outcome/1",
    "control/1",
    "read_ext/1",
    "read_ext/2",
    "settings/1",
    "activity/1",
    "quick/1",
    "records/1",
    "run/1",
    "term/1",
    "reader/1",
    "phone/1",
    "worktree/1",
    "workdir/1",
    "publish/1",
]
MARKET_READS = ("market.list", "market.installed")
MARKET_WRITES = ("market.add", "market.stage", "market.unstage", "market.uninstall", "market.refresh")
MARKET_OPS = (*MARKET_READS, *MARKET_WRITES)
OPS = {
    name: 1
    for name in (
        "host.list",
        "whoami",
        "workspace.show",
        "host.status",
        "member.list",
        "workspace.settings",
        "addon.list",
        # what turning an add-on with settings on would sign (read only): the grant body of the `addon_enable` intent
        "addon.preview",
        "needs",
        "activity.list",
        "quick.list",
        "records.status",
        "ticket.query",
        "ticket.get",
        "diff",
        "artifact.get",
        "grant.list",
        "human.get",
        "human.pending",
        "human.prepare",
        "human.submit",
        "human.cancel",
        "sub.open",
        "sub.close",
        "outcome.get",
        # M7a terminals (contract v1.6, section 12.3): reads, and the writes the routes call with write=True and an idem
        "run.sub",
        "run.list",
        "run.stop",
        "term.preview",
        "term.open",
        "term.attach",
        "attach.cancel",
        # M7b start agent (contract v1.6, section 12.3, H11c): the preview, the start (a write with an idem), and the
        # person's own runner-grant reads and forget. Never `grant.deposit`: only the click signer child (or the
        # terminal) sends it, with the secret, on its own host connection (FORBIDDEN below).
        "agent.preview",
        "agent.start",
        "grant.deposit_status",
        "grant.forget",
        # H12d-2 (section 0.8.6 item 9.11): the readers and their levels, a read of cap `reader/1`. Never
        # `reader.declare`: a declaration comes from the device itself.
        "reader.list",
        "phone.request",
        "phone.cancel",
        "phone.status",
        # Worktrees (H11d, section 12.7): the list is a read; create and remove are dashboard-direct writes (Tier A,
        # no signature: the host's own gate, column and rate limit apply) the routes call with an idem.
        "worktree.list",
        "worktree.create",
        "worktree.remove",
        # Ticket work folders (design r3 part C): what a ticket without a codebase left in its folder, and the
        # person's own delete of it (a host-side write with an idem, no log event).
        "workdir.get",
        "workdir.delete",
        # Publish (P5/P6, design-publish-addon 2.2): list is a read; the others are person writes the routes call
        # with an idem. `publish.send` answers the share link ONCE (shares.py); `publish.share` is never sent as a
        # plain op: it is the inner op of a signed hand-off (handoff.py).
        "publish.list",
        "publish.prepare",
        "publish.send",
        "publish.revoke",
        "publish.discard",
        "publish.register",
        # V3 A7 hub pages: reads only (the host's own state of the hub and of the share rules). Every hub change is a
        # command the person runs in their Terminal (hub.py); this process never sends hub.join/mode/leave/connect.
        "hub.status",
        "hub.handoff",
        "share.rules",
        "share.preview",
        # V3 A10 (design-g3 4.2): the Add-ons page. Reads: market.list, market.installed. Person writes (owner role, the
        # host refuses an agent): add, stage, unstage, uninstall, refresh. Every install is a terminal hand-off.
        *MARKET_OPS,
    )
}
RUN_OPS = {"run.sub", "run.list", "run.stop", "agent.preview", "agent.start", "grant.deposit_status", "grant.forget"}
#: ops this process never sends, whatever a caller asks: `grant.deposit` carries the runner grant's secret
#: (`auth.grant`), which never reaches the dashboard's HTTP process (D-P2-27, section 0.8.6 item 6).
FORBIDDEN = frozenset({"grant.deposit"})
WORKTREE_OPS = {"worktree.list", "worktree.create", "worktree.remove"}
WORKDIR_OPS = {"workdir.get", "workdir.delete"}
PUBLISH_OPS = {
    "publish.list",
    "publish.prepare",
    "publish.send",
    "publish.revoke",
    "publish.discard",
    "publish.register",
}
TERM_OPS = {"term.preview", "term.open", "term.attach", "attach.cancel"}

QUERY_OPS = {"ticket.query", "ticket.get", "grant.list", "needs", "workspace.show", "member.list"}


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
                raise HostError(w.get("error"))
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

    def has(self, cap: str) -> bool:
        """Does the host list `cap`? Without `read_ext/1` the optional fields of v1.4 mean unknown, not zero."""
        return cap in self.welcome.get("caps", [])

    def supports(self, op: str, cap="ops/1") -> bool:
        listed = self.welcome.get("ops", {}).get(op) == OPS.get(op, 1)
        # Real-host finding (core H6b, stream A item RH-1): the host serves the human.* ops and lists them in `ops`, but
        # `welcome.caps` has no `human/1`. `ops` decides per op (contract section 0.3), so a listed human op counts.
        # `hub/1` is likewise decided by the listed op (the host lists hub.* and share.* in `ops`, with no `hub/1` cap).
        return listed and (cap in self.welcome.get("caps", []) or cap in ("human/1", "hub/1"))

    def send(self, frame):
        with self.send_lock:
            self.sock.sendall(encode(frame))

    def cancel(self, request_id):
        self.send({"t": "cancel", "id": request_id})

    def call(
        self,
        op: str,
        args: dict | None = None,
        *,
        write=False,
        idem: str | None = None,
        dry_run: bool | None = None,
        files: dict | None = None,
    ) -> Any:
        if op in FORBIDDEN:
            raise ValueError("this process never sends grant.deposit")
        write = write or op == "human.submit" or op not in OPS
        if dry_run is not None and not (write or op == "human.prepare"):
            raise ValueError("dry_run is only for log writes and human.prepare")
        if op == "human.submit" and (
            not isinstance((args or {}).get("events"), list) or not 1 <= len(args["events"]) <= 16 or "event" in args
        ):
            raise ValueError("human.submit requires events (1..16)")
        if files is not None and op != "human.prepare":
            raise ValueError("files are only for human.prepare (the inner op's files, host contract R9)")
        if write and not dry_run and not idem:
            raise ValueError("every write requires its logical call's idem")
        cap = (
            "phone/1"
            if op.startswith("phone.")
            else "control/1"
            if op == "host.list"
            else "sub/1"
            if op.startswith("sub.")
            else "human/1"
            if op.startswith("human.")
            else "outcome/1"
            if op == "outcome.get"
            else "diff/1"
            if op in {"diff", "artifact.get"}
            else "activity/1"
            if op == "activity.list"
            else "settings/1"
            if op == "workspace.settings"
            else "quick/1"
            if op == "quick.list"
            else "records/1"
            if op in {"records.status", "records.snapshot", "records.export_check", "records.note"}
            else "run/1"
            if op in RUN_OPS
            else "term/1"
            if op in TERM_OPS
            else "worktree/1"
            if op in WORKTREE_OPS
            else "workdir/1"
            if op in WORKDIR_OPS
            else "publish/1"
            if op in PUBLISH_OPS
            else "reader/1"
            if op == "reader.list"
            else "query/1"
            if op in QUERY_OPS
            else "ops/1"
        )
        if not self.supports(op, cap):
            raise fail("op.unsupported", exit=1)
        with self.lock:
            self.serial += 1
            rid = f"d{self.serial}"
            req = {"t": "req", "id": rid, "op": op, "args": args or {}}
            if files:
                req["files"] = files
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
                        raise HostError(frame.get("error"))
                    if frame.get("t") != "res" or frame.get("id") != rid:
                        self.close()
                        raise fail("frame.invalid", exit=6)
                    if frame.get("ok") is not True:
                        raise HostError(frame.get("error"), frame.get("exit"))  # both checked by HostError
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
        """One reader at a time; calls can still consume and queue interleaved events.

        The wait for the next frame happens WITHOUT the link's lock: this link is shared with every REST call of the
        workspace, and a reader that held the lock for its whole `select` made each call wait for it (about 100 ms,
        twice per request: `addon.list` and the op itself). Only taking a frame off the socket holds the lock."""
        with self.lock:
            if self.events:
                return self.events.popleft()
            if self.sock.fileno() < 0:
                raise fail("host.unavailable")
        try:
            if not select.select([self.sock], [], [], timeout)[0]:
                return None
        except (OSError, ValueError):  # closed while waiting
            raise fail("host.unavailable") from None
        with self.lock:
            try:
                if self.events:
                    return self.events.popleft()
                # A call may have read the frame while this reader waited: only read what is still there.
                if self.sock.fileno() < 0:
                    raise fail("host.unavailable")
                if not select.select([self.sock], [], [], 0)[0]:
                    return None
                frame = receive(self.sock)
                if frame.get("t") == "fatal":
                    self.close()
                    raise HostError(frame.get("error"))
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

"""H2b test helpers: a workspace backend that serves log writes through the outcome store, and a socket client.

No imports from ``tests.`` here: ``crashhost.py`` loads this file as a sibling script module in another process.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from orch.host import frames
from orch.host.errors import HostError
from orch.host.outcomes import (
    OutcomeStore,
    Planned,
    principal_of,
    request_idem,
    result_builder,
)
from orch.host.workspace import ok_result
from orch.identity import new_ulid


@result_builder("new")
def _rebuild_new(record: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    event = events[-1]
    return ok_result({"uid": record["planned"][0]["log"], "key": event["key"]}, seq=event["seq"])


@result_builder("apply")
@result_builder("human.submit")
def _rebuild_multi(record: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    return ok_result({"uids": [p["log"] for p in record["planned"]]}, seq=events[-1]["seq"])


class OutcomeBackend:
    """``new {title}`` appends a ``ticket.created`` with a planned uid and event id: a log write, so the outcome
    store records it. ``log``, ``ask`` and ``artifact.add`` are the failure and timing shapes; ``outcome.get`` is
    §0.8. The names are real registry ops (the classification looks them up); the backend runs its own handlers."""

    caps = ["ops/1", "outcome/1"]
    ops = {"apply": 1, "human.submit": 1, "new": 1, "log": 1, "ask": 1, "artifact.add": 1, "show": 1, "outcome.get": 1}

    def __init__(self, actor: dict[str, Any] | None = None, owner: str | None = None) -> None:
        if actor is None:
            actor, owner = json.loads(os.environ["T_ACTOR"]), os.environ["T_OWNER"]
        self.actor, self.owner = actor, owner
        self.gate = threading.Event()  # artifact.add waits on it, inside the worker thread, after the append
        self.in_apply = threading.Event()
        self.busy = 0  # ask fails retryably this many more times
        self.applied = 0  # apply() calls that reached the store
        self.blobs: dict[str, str] = {}  # blob id -> digest (what H4a2's blob store will answer)
        self.blob_lookups: list[str] = []

    def kind(self, op: str):
        return {
            "new": ("unattended", "write"),
            "apply": ("unattended", "write"),  # two events: the multi-event shapes (partial, roll-forward)
            "human.submit": ("unattended", "write"),
            "log": ("unattended", "write"),
            "ask": ("unattended", "write"),
            "artifact.add": ("unattended", "write"),
            "show": ("read", "read"),
            "outcome.get": ("read", "read"),
        }.get(op)

    def principal(self, call: Any) -> str:
        grant = (call.frame.get("auth") or {}).get("grant")
        return principal_of(call.peer.uid, call.column.name, grant)

    def blob_digest(self, blob_id: str) -> str | None:
        self.blob_lookups.append(blob_id)
        return self.blobs.get(blob_id)

    async def run(self, call: Any) -> dict[str, Any]:
        op, frame = call.frame["op"], call.frame
        opts = frame.get("opts") or {}
        service = call.service
        if op == "outcome.get":
            out = await service.call(service.outcomes.lookup, self.principal(call), frame["args"]["idem"])
            return ok_result(out)
        if op == "show":
            request_idem(op, opts)  # dry_run on a read is invalid.input
            return ok_result({})
        try:
            return await service.call(self._write, call)
        except HostError:
            raise
        except Exception:
            import traceback

            traceback.print_exc()  # the transport answers `internal` and hides why; a crash host's stderr shows it
            raise

    def _write(self, call: Any) -> dict[str, Any]:
        """On the worker thread, in one piece: begin, append, commit."""
        op, frame = call.frame["op"], call.frame
        args, service = frame["args"], call.service
        idem = request_idem(op, frame.get("opts"))
        if idem is None:  # a dry run: nothing is looked up, recorded or appended
            return ok_result({"dry_run": True, "title": args.get("title")})
        outcomes: OutcomeStore = service.outcomes
        principal = self.principal(call)
        files, digest_args = outcomes.bind_files(principal, idem, frame.get("files"), blob_digest=self.blob_digest)
        count = 2 if op in {"apply", "human.submit"} else 1
        ids = [(new_ulid(), new_ulid()) for _ in range(count)]

        def apply(pending: Any) -> dict[str, Any]:
            self.applied += 1
            if op == "log":
                raise HostError("invalid.input", "no")
            if op == "ask" and self.busy > 0:
                self.busy -= 1
                raise HostError("lock.busy", "busy")
            made = None
            for uid, event_id in ids:
                made = service.store.create_ticket(
                    actor=self.actor,
                    ticket_type="feature",
                    title=args.get("title", "t"),
                    owner=self.owner,
                    uid=uid,
                    event_id=event_id,
                )
            if op == "artifact.add":
                self.in_apply.set()
                self.gate.wait(20)
            if count == 2:
                return ok_result({"uids": [uid for uid, _ in ids]}, seq=made.event["seq"])
            return ok_result({"uid": ids[0][0], "key": made.event["key"]}, seq=made.event["seq"])

        return outcomes.execute(
            principal,
            idem,
            op,
            args,
            apply,
            files=files,
            session=(frame.get("auth") or {}).get("session"),
            planned=[Planned(uid, event_id, "ticket.created", self.actor) for uid, event_id in ids],
            digest_args=digest_args,
        )


class WsClient:
    """A blocking client of one workspace socket (hello, req, cancel)."""

    def __init__(self, path: Any, wid: str, uid: int) -> None:
        import time

        from orch.host.client import CLIENT_OPS, Pin, handshake_pinned

        hello = {"t": "hello", "proto": [1], "client": "test", "caps": [], "ops": dict(CLIENT_OPS)}
        # a current client: task.done at contract 2 (H7: the host refuses an unlisted version, §0.4)
        # The host binds a workspace socket after ``register`` has answered (a slow runner showed a FileNotFoundError),
        # so wait for the socket to accept, bounded, exactly as wait_for_socket does for the control socket.
        deadline = time.monotonic() + 20
        while True:
            try:
                self.sock, self.welcome = handshake_pinned(Pin(uid, wid, path), hello)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        self.decoder = frames.Decoder()
        self.pending: list[Any] = []

    def send(self, request_id: str, op: str, args: dict[str, Any] | None = None, **extra: Any) -> None:
        self.sock.sendall(frames.encode({"t": "req", "id": request_id, "op": op, "args": args or {}, **extra}))

    def cancel(self, request_id: str) -> None:
        self.sock.sendall(frames.encode({"t": "cancel", "id": request_id}))

    def read(self, timeout: float = 10) -> dict[str, Any]:
        from orch.host._socket import recv

        self.sock.settimeout(timeout)
        while not self.pending:
            data = recv(self.sock)
            assert data, "connection closed"
            self.pending.extend(self.decoder.feed(data))
        return self.pending.pop(0)

    def call(self, op: str, args: dict[str, Any] | None = None, request_id: str = "r", **extra: Any) -> dict[str, Any]:
        self.send(request_id, op, args, **extra)
        return self.read()

    def close(self) -> None:
        self.sock.close()

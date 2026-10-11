"""Control intents (§0.7): ``<state>/host/intents/<key>`` (0600), one per ``(namespace, principal, idem)``.

Before ``host.register``, ``host.unregister`` or ``host.bootstrap`` changes anything the host writes
``{idem, binding, op, args_normalised, step: "started"}`` and fsyncs it. After the change it records the terminal
state (``committed`` with the result, or ``failed`` with the error). At host start every ``started`` intent is
finished or rolled back by :mod:`orch.host.control`, and either way it ends in a durable terminal state.

A same-key retry never runs the op again: it gets the stored result (``duplicate: true``) or the stored error, and
another binding gets ``idem.conflict``. Records live 24 h, then become tombstones ``{key, binding, state}`` for 7
days (``idem.expired``).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch import canon

from .custody import ensure_private_dir, read_json, write_private
from .errors import HostError

__all__ = ["KEEP_SECONDS", "TOMBSTONE_SECONDS", "Intent", "IntentStore", "binding_of"]

KEEP_SECONDS = 24 * 3600
TOMBSTONE_SECONDS = 7 * 24 * 3600


def binding_of(
    op: str,
    args_normalised: dict[str, Any],
    session: str | None = None,
    files: Mapping[str, str] | None = None,
) -> str:
    """``sha256(cj({op, args_normalised, files:{arg: Hash}, session}))``; control ops carry no files and no session."""
    doc = {"op": op, "args_normalised": args_normalised, "files": dict(files or {}), "session": session}
    return "sha256:" + hashlib.sha256(canon.cj_checked(doc)).hexdigest()


@dataclass
class Intent:
    key: str
    record: dict[str, Any]
    #: a ``started`` intent left by an earlier attempt that is no longer running (it died, or kept the intent on
    #: purpose after a durable change): the caller finishes it the way recovery at start does
    resumed: bool = False

    @property
    def op(self) -> str:
        return self.record["op"]

    @property
    def args(self) -> dict[str, Any]:
        return self.record["args_normalised"]

    @property
    def step(self) -> str:
        return self.record["step"]


class IntentStore:
    #: the ``step`` of a record whose change may or may not have landed (``pending`` for a log write's record)
    START = "started"
    #: the steps that end in a tombstone after 24 h
    TERMINAL = frozenset({"committed", "failed"})

    def __init__(
        self,
        state_dir: Path,
        host_uid: int,
        *,
        clock: Callable[[], float] = time.time,
        directory: Path | None = None,
    ) -> None:
        self.dir = Path(directory) if directory is not None else Path(state_dir) / "host" / "intents"
        self.host_uid = host_uid
        self.clock = clock
        self._lock = threading.Lock()
        self._running: dict[str, threading.Event] = {}

    def open(self) -> None:
        ensure_private_dir(self.dir.parent, self.host_uid)
        ensure_private_dir(self.dir, self.host_uid)

    @staticmethod
    def key(namespace: str, principal: str, idem: str) -> str:
        return hashlib.sha256(f"{namespace}\0{principal}\0{idem}".encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.dir / key

    def read(self, key: str) -> dict[str, Any] | None:
        return read_json(self._path(key), self.host_uid)

    def _write(self, key: str, record: dict[str, Any]) -> None:
        write_private(self._path(key), record)

    # -- the write path
    def begin(
        self, namespace: str, principal: str, idem: str, op: str, args_normalised: dict[str, Any]
    ) -> Intent | dict[str, Any]:
        """A new ``started`` intent (fsynced), or the stored terminal record for a replay. A concurrent duplicate
        waits for the first call. Raises ``idem.conflict`` or ``idem.expired``."""
        key = self.key(namespace, principal, idem)
        binding = binding_of(op, args_normalised)
        while True:
            with self._lock:
                running = self._running.get(key)
                if running is None:
                    record = self.read(key)
                    if record is None:
                        record = {
                            "idem": idem,
                            "namespace": namespace,
                            "principal": principal,
                            "binding": binding,
                            "op": op,
                            "args_normalised": args_normalised,
                            "step": self.START,
                            "at": int(self.clock()),
                        }
                        self._write(key, record)
                        self._running[key] = threading.Event()
                        return Intent(key, record)
                    if record.get("step") == self.START and record.get("binding") == binding:
                        self._running[key] = threading.Event()
                        return Intent(key, record, resumed=True)
                    break
            running.wait()
        if record.get("binding") != binding:
            raise HostError("idem.conflict", "this idem was used for another request")
        if record["step"] == "tombstone":
            raise HostError("idem.expired", "this idem has expired")
        return record

    def peek(self, namespace: str, principal: str, idem: str) -> dict[str, Any] | None:
        """The record for this key, if any (no lock taken on the op: a hint for read-only pre-checks)."""
        return self.read(self.key(namespace, principal, idem))

    def release(self, intent: Intent) -> None:
        """Stop running ``intent`` but keep it ``started`` (a durable change landed and must be finished later:
        by a same-key retry, or at the next host start)."""
        with self._lock:
            self._release(intent.key)

    def update(self, intent: Intent, **fields: Any) -> None:
        """Durably add facts to a ``started`` intent (for example the workspace id it is about)."""
        intent.record.update(fields)
        self._write(intent.key, intent.record)

    def complete(self, intent: Intent, result: dict[str, Any]) -> dict[str, Any]:
        return self._finish(intent, {"step": "committed", "result": result, "exit": 0})

    def fail(self, intent: Intent, error: HostError) -> dict[str, Any]:
        return self._finish(intent, {"step": "failed", "error": error.error(), "exit": error.exit})

    def abandon(self, intent: Intent) -> None:
        """A retryable failure (``lock.busy``) stores nothing: the retry runs again."""
        with self._lock:
            try:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self._path(intent.key))
            finally:
                self._release(intent.key)  # never leave the key marked running: a retry would wait for it forever

    def _finish(self, intent: Intent, fields: dict[str, Any]) -> dict[str, Any]:
        record = {**intent.record, **fields, "done_at": int(self.clock())}
        with self._lock:
            try:
                self._write(intent.key, record)
                intent.record = record
            finally:
                self._release(intent.key)  # also when the write fails (disk full): a retry must not wait forever
        return record

    def _release(self, key: str) -> None:
        event = self._running.pop(key, None)
        if event is not None:
            event.set()

    # -- lookups and recovery
    def outcome(self, namespace: str, principal: str, idem: str) -> dict[str, Any]:
        key = self.key(namespace, principal, idem)
        with self._lock:
            running = key in self._running
            record = self.read(key)
        if record is None or record.get("principal") != principal or record.get("namespace") != namespace:
            raise HostError("outcome.unknown_idem", "no outcome for this idem")
        if record["step"] == "tombstone":
            raise HostError("idem.expired", "this idem has expired")
        if running or record["step"] == self.START:
            return {"state": "pending"}
        out: dict[str, Any] = {"state": record["step"], "exit": record["exit"]}
        if "result" in record:
            out["result"] = record["result"]
        if "error" in record:
            out["error"] = record["error"]
        return out

    def started(self) -> list[Intent]:
        out = []
        for name in sorted(os.listdir(self.dir)):
            if len(name) != 64 or not all(c in "0123456789abcdef" for c in name):
                continue
            record = self.read(name)
            if record is not None and record.get("step") == self.START:
                out.append(Intent(name, record))
        return out

    def recovered(self, intent: Intent, result: dict[str, Any] | None, error: HostError | None) -> dict[str, Any]:
        """Record the terminal state found at host start."""
        if error is not None:
            return self.fail(intent, error)
        assert result is not None
        return self.complete(intent, result)

    def sweep(self) -> None:
        """Terminal records older than 24 h become tombstones; tombstones older than 7 days go."""
        now = self.clock()
        for name in os.listdir(self.dir):
            if len(name) != 64:
                continue
            with self._lock:
                if name in self._running:
                    continue
                record = self.read(name)
                if record is None:
                    continue
                done = record.get("done_at", record.get("at", now))
                if record["step"] == "tombstone":
                    if now - done > TOMBSTONE_SECONDS:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(self._path(name))
                elif record["step"] in self.TERMINAL and now - done > KEEP_SECONDS:
                    self._tombstone(name, record, now)

    def _tombstone(self, name: str, record: dict[str, Any], now: float) -> None:
        """Replace a terminal record with ``{key, binding, state}`` (caller holds the lock)."""
        self._write(
            name,
            {
                "key": name,
                "namespace": record["namespace"],
                "principal": record["principal"],
                "binding": record["binding"],
                "state": record["step"],
                "step": "tombstone",
                "done_at": int(now),
            },
        )

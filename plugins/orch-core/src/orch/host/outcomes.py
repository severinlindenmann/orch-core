"""Idempotency and outcomes for log writes (§0.7, H2b): the write classification and the outcome store.

**The API the dispatcher (H4a1) calls**, in the order a write meets it (§0.6 steps 8 and 9):

* :func:`classify` puts every op in one write category; :func:`request_idem` applies ``dry_run`` and ``idem.required``
  to a request (a dry run never has an idem; every other op called with ``dry_run`` is ``invalid.input``).
* :func:`principal_of` derives the record's principal (F4): ``g:<grant id>@<uid>`` for a verified grant, else
  ``uid:<n>``, so a grant id with a wrong secret reaches only the UID's own records.
* :meth:`OutcomeStore.bind_files` turns ``files`` into the digests of the binding. A ``{digest}`` file is only ever
  compared with the stored binding (M1); it is never resolved to bytes.
* :meth:`OutcomeStore.begin` writes the ``pending`` record (fsync) with the **planned event ids** before the append
  and returns a :class:`~orch.host.intents.Intent`, or the stored answer of a duplicate; :meth:`commit`,
  :meth:`refuse` and :meth:`partial` end it. :meth:`execute` wraps the three and is meant to run **on the workspace
  worker thread** in one piece, so a cancel, a timeout or a dropped connection after the append cannot lose the
  record (a running write is never undone, §0.2); a write whose caller already answered ``outcome.unknown`` still
  ends ``committed``, and ``outcome.get`` reports it.
* :meth:`OutcomeStore.lookup` is ``outcome.get``; :meth:`OutcomeStore.reconcile_all` runs at start.
* :func:`result_frame` and ``HostError.response`` build a replay's ``res`` with the **retry's** request id: a record
  stores a ``result`` or an ``error``, never a frame.

**Reconciliation.** A crash can leave the append in the log while the record says ``pending``. Every ``pending``
record that no call in this process is running is reconciled by its planned ``(log, event id)`` pairs, under the
store lock: all landed gives ``committed`` (the result is rebuilt from the log with the op's builder, see
:func:`result_builder`), some gives ``partial`` (``human.submit`` instead stays ``pending`` for H5a to roll forward),
none removes the record so the retry runs again (a retry that names a file by ``{digest}`` then answers
``blob.invalid``: the client uploads again).

**A refusal after an append.** :meth:`OutcomeStore.execute` does not believe the exception ``apply`` raises: it asks the
log first (the same check as reconciliation), so a retryable error after an append never deletes the record (the retry
would append again) and a refusal after event 1 of N is ``partial``, not ``failed``.

**Retention.** Records live 24 h, then become tombstones for 7 days (``idem.expired``). At most 100 000 records per
principal: past that the oldest completed ones become tombstones early. The store keeps an in-memory index of every
record (built in one pass at :meth:`OutcomeStore.open`), so the limit check and :meth:`OutcomeStore.sweep` read only the
records they change. The host calls ``sweep`` periodically, off the event loop.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

from .custody import ensure_private_dir
from .errors import HostError
from .intents import KEEP_SECONDS, TOMBSTONE_SECONDS, Intent, IntentStore, binding_of

__all__ = [
    "BUILDERS",
    "CONTROL_WRITES",
    "MAX_RECORDS",
    "NO_PARTIAL",
    "Category",
    "OutcomeStore",
    "Planned",
    "Reconciler",
    "StoreReconciler",
    "classify",
    "dry_run_supported",
    "principal_of",
    "request_idem",
    "result_builder",
    "result_frame",
]

Category = Literal["log", "control", "job", "create", "read"]

MAX_RECORDS = 100_000
#: how long a duplicate waits for the running call with its key before it answers ``outcome.unknown`` (retryable). The
#: workspace has one worker thread, so a wait that long was never going to be woken by that thread anyway.
WAIT_SECONDS = 5.0
LOG = logging.getLogger(__name__)
CONTROL_WRITES = frozenset({"host.register", "host.unregister", "host.bootstrap"})
#: ops that are log writes though no registry operation carries them
_HUMAN_WRITES = frozenset({"human.submit"})
#: device-signed person events through the relay entry (column R, §11A.4): a log write with an idem, no dry run
_RELAY_WRITES = frozenset({"relay.submit"})
#: host-side writes with an idem and an outcome record but no log event (H11e: the private-remote mark); a record
#: with nothing planned is removed by reconciliation, and the op is idempotent, so a retry after a crash runs again
HOST_WRITES = frozenset(
    {"records.remote_private", "records.note", "reader.declare", "phone.request", "phone.cancel", "workdir.delete"}
)
#: every other host op (§0.7 table): reads and side-effect-free ops, whose ``opts.idem`` is ignored
_READS = frozenset(
    {
        "human.prepare", "human.get", "human.pending", "human.cancel",
        "blob.put", "blob.drop", "sub.open", "sub.close",
        "outcome.get", "host.outcome", "job.next", "job.heartbeat",
        "host.list", "host.status", "host.restart", "whoami",
        "ticket.query", "ticket.get", "needs", "diff", "artifact.get",
        "workspace.show", "member.list", "session.list", "grant.list",
        "activity.list", "workspace.settings", "ticket.text",
        "records.snapshot", "records.export_check", "records.status", "reader.list",
        "phone.requests", "phone.status", "gate.input", "workdir.get",
        "run.list", "run.sub",
    }
)  # fmt: skip
#: a multi-event write that is never ``partial``: it rolls forward (§0.8.1, H5a; ``relay.submit`` through the
#: ``relay_fields`` hook, §11A.4)
NO_PARTIAL = frozenset({"human.submit", "relay.submit"})
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


# --- classification --------------------------------------------------------------------------------------------


def classify(op: str) -> Category:
    """The write category of ``op`` (§0.7). An op nobody serves is ``unknown_command``."""
    if op in CONTROL_WRITES:
        return "control"
    if op in _HUMAN_WRITES or op in _RELAY_WRITES or op in HOST_WRITES:
        return "log"
    from .review import HOST_PERSON_OPS

    if op in HOST_PERSON_OPS:  # move, policy.set, settings.set: person log writes, served through human.* only
        return "log"
    if op == "job.done":
        return "job"
    if op == "host.wsk_create":
        return "create"
    if op in _READS:
        return "read"
    from orch import ops

    try:
        who = ops.get(op).declaration["who"]
    except KeyError:
        raise HostError("unknown_command", f"{op} is not served") from None
    return "read" if who == "read" else "log"


def dry_run_supported(op: str) -> bool:
    """Only the log-write registry ops and ``human.prepare`` take ``opts.dry_run`` (§0.7)."""
    if op in _HUMAN_WRITES or op in _RELAY_WRITES or op in HOST_WRITES:
        return False
    return op == "human.prepare" or classify(op) == "log"


def request_idem(op: str, opts: Mapping[str, Any] | None) -> str | None:
    """§0.6 steps 8 (``dry_run`` support) and 9 (``idem.required``) for one request.

    Returns the idem to use, or ``None`` when the request has none: a dry run (whose ``opts.idem`` is ignored, never
    required, looked up or stored) and every op that is not a log or control write.
    """
    opts = opts or {}
    if opts.get("dry_run"):
        if not dry_run_supported(op):
            raise HostError("invalid.input", f"dry_run is not supported by {op}")
        return None
    if classify(op) in {"log", "control"}:
        idem = opts.get("idem")
        if idem is None:
            raise HostError("idem.required", f"{op} needs opts.idem")
        return str(idem)
    return None


def principal_of(uid: int, column: str, grant: str | None = None) -> str:
    """``g:<grant id>@<uid>`` for the verified grant column (``grant`` is the verified ``auth.grant``), else
    ``uid:<n>`` (F4): a grant id never makes a principal by itself. Raises for column R (its principal is
    ``r:<device_id>``, from the relay identity alone, §11A.4), for any column it does not know and for a UID that is
    not a kernel UID."""
    if column not in {"P", "G", "N", "W", "C"} or type(uid) is not int or uid < 0:
        raise ValueError(f"no UID principal for column {column!r}")
    if column == "G":
        from orch.identity import parse_grant

        return f"g:{parse_grant(grant).grant_id}@{uid}"
    return f"uid:{uid}"


def result_frame(request_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """A stored ``result`` in a fresh ``res`` carrying the **current** request id."""
    return {"t": "res", "id": request_id, "ok": True, "result": result}


# --- reconciliation seam ----------------------------------------------------------------------------------------

Builder = Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]
#: op name -> the function that rebuilds its ``result`` from the record and the logged events (H4a1 registers them)
BUILDERS: dict[str, Builder] = {}


class Planned(NamedTuple):
    """An event a write will append, planned before the append: the log, the event id and what the event must be.

    ``type`` (and ``actor``, for an event nobody signs) are checked against the logged event, as the store's own retry
    check does: an event that merely carries the planned id is not trusted."""

    log: str
    id: str
    type: str
    actor: dict[str, Any] | None = None


def result_builder(op: str) -> Callable[[Builder], Builder]:
    """Register ``fn(record, events) -> result`` as the builder of ``op``'s result for reconciliation."""

    def register(fn: Builder) -> Builder:
        BUILDERS[op] = fn
        return fn

    return register


def _default_builder(record: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    from .workspace import ok_result

    return ok_result({}, seq=max(int(e["seq"]) for e in events))


class Reconciler(Protocol):
    def locked(self) -> AbstractContextManager[Any]: ...

    def find(self, log: str, event_id: str) -> dict[str, Any] | None:
        """The logged event with this id in the verified log, or ``None``."""

    def rebuild(self, record: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]: ...


class StoreReconciler:
    """The reconciler over a workspace :class:`~orch.store.Store` (call it on the store's worker thread)."""

    def __init__(self, store_of: Callable[[], Any]) -> None:
        self.store_of = store_of

    def _store(self) -> Any:
        store = self.store_of()
        if store is None:
            raise HostError("workspace.unavailable", "the workspace store is not open")
        return store

    def locked(self) -> AbstractContextManager[Any]:
        return self._store().locked()

    def find(self, log: str, event_id: str) -> dict[str, Any] | None:
        return self._store().logged_event(log, event_id)

    def rebuild(self, record: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        return BUILDERS.get(record["op"], _default_builder)(record, events)


def _matches(planned: dict[str, Any], event: dict[str, Any]) -> bool:
    return event.get("type") == planned.get("type") and (
        planned.get("actor") is None or event.get("actor") == planned["actor"]
    )


# --- the store --------------------------------------------------------------------------------------------------

_Entry = tuple[str, str, float]  # principal, step, done_at (or created at)


class OutcomeStore(IntentStore):
    """``<state>/hosts/<ws>/outcomes/<key>`` (0600), keyed by ``(workspace id, principal, idem)``.

    The methods block (locks, fsync, the store lock) and belong on the workspace worker thread, never on the event
    loop. A call that is running holds its key in memory; a duplicate of it waits (bounded) and then gets its result.
    """

    START = "pending"
    TERMINAL = frozenset({"committed", "failed", "partial"})

    def __init__(
        self,
        state_dir: Path,
        workspace_id: str,
        host_uid: int,
        *,
        clock: Callable[[], float] | None = None,
        reconciler: Reconciler | None = None,
        max_records: int = MAX_RECORDS,
        wait_seconds: float = WAIT_SECONDS,
    ) -> None:
        import time

        super().__init__(
            state_dir,
            host_uid,
            clock=clock or time.time,
            directory=Path(state_dir) / "hosts" / workspace_id / "outcomes",
        )
        self.workspace_id = workspace_id
        self.namespace = workspace_id
        self.reconciler = reconciler
        self.max_records = max_records
        self.wait_seconds = wait_seconds
        #: ``fn(record) -> fields | "keep"`` for a pending ``human.submit`` record some of whose events landed (H5a)
        self.roll_forward: Callable[[dict[str, Any]], dict[str, Any] | str] | None = None
        #: the same for a pending ``human.prepare`` record that names its request (``request_id``)
        self.prepared: Callable[[dict[str, Any]], dict[str, Any] | str] | None = None
        #: the same for a pending ``relay.submit`` record some of whose events landed (§11A.4): registered at open by
        #: the relay entry (R5, ``relay/submit.py``). Unset, such a record stays ``pending`` (never ``partial``, never
        #: handled by the human roll-forward, never run again)
        self.relay_fields: Callable[[dict[str, Any]], dict[str, Any] | str] | None = None
        #: key -> (principal, step, done_at); every mutation holds ``_lock``
        self._idx: dict[str, _Entry] = {}
        self._counts: dict[str, int] = {}

    # -- the index (one pass at open; kept current by every write)
    @staticmethod
    def _entry(record: dict[str, Any]) -> _Entry:
        return (
            str(record.get("principal", "")),
            str(record.get("step")),
            float(record.get("done_at", record.get("at", 0))),
        )

    def _write(self, key: str, record: dict[str, Any]) -> None:
        super()._write(key, record)
        self._set(key, self._entry(record))

    def _set(self, key: str, entry: _Entry) -> None:
        self._drop(key)
        self._idx[key] = entry
        if entry[1] != "tombstone":
            self._counts[entry[0]] = self._counts.get(entry[0], 0) + 1

    def _drop(self, key: str) -> None:
        old = self._idx.pop(key, None)
        if old is not None and old[1] != "tombstone":
            self._counts[old[0]] = max(0, self._counts.get(old[0], 0) - 1)

    def open(self) -> None:
        """Create the directories and read every record once: the index, the counts and the ``pending`` records that
        :meth:`reconcile_all` and :meth:`sweep` then work from."""
        ensure_private_dir(self.dir.parent.parent, self.host_uid)
        ensure_private_dir(self.dir.parent, self.host_uid)
        ensure_private_dir(self.dir, self.host_uid)
        entries: dict[str, _Entry] = {}
        for name in os.listdir(self.dir):
            record = self.read(name) if len(name) == 64 else None
            if record is not None:
                entries[name] = self._entry(record)
        with self._lock:
            self._idx, self._counts = {}, {}
            for name, entry in entries.items():
                self._set(name, entry)

    def count(self, principal: str) -> int:
        return self._counts.get(principal, 0)

    def abandon(self, intent: Intent) -> None:
        with self._lock:
            try:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self._path(intent.key))
                self._drop(intent.key)
            finally:
                self._release(intent.key)

    def _admit(self, principal: str) -> None:
        """Called with the lock held, before a record is created: at the limit the oldest completed records become
        tombstones early (about 1 % at a time, chosen from the index), and with none to spare the write is
        ``quota.host``."""
        if self.count(principal) < self.max_records:
            return
        done = sorted(
            (entry[2], name)
            for name, entry in self._idx.items()
            if entry[0] == principal and entry[1] in self.TERMINAL and name not in self._running
        )
        want = self.count(principal) - self.max_records + max(1, self.max_records // 100)
        for _, name in done[: max(want, 1)]:
            record = self.read(name)
            if record is not None:
                self._tombstone(name, record, self.clock())
        if self.count(principal) >= self.max_records:
            raise HostError("quota.host", "too many outcome records for this principal")

    # -- files and the binding (§0.3, §0.10, M1)
    def bind_files(
        self,
        principal: str,
        idem: str | None,
        specs: Mapping[str, Mapping[str, Any]] | None,
        *,
        blob_digest: Callable[[str], str | None] | None = None,
    ) -> tuple[dict[str, str], frozenset[str]]:
        """The digests of a request's ``files`` for the binding, and the args named by ``{digest}``.

        ``{b64}`` is hashed here, ``{blob}`` through ``blob_digest(blob id)`` (H4a2; ``None``: unknown or foreign,
        ``blob.invalid``). ``{digest}`` is accepted only when ``(principal, idem)`` already has a record (a tombstone
        counts), and is taken as it is: never looked up in blobs, artifacts or anything else. Pass the second value
        to :meth:`begin`, which refuses a ``{digest}`` retry that has to run again.
        """
        digests: dict[str, str] = {}
        by_digest: set[str] = set()
        for arg, spec in sorted((specs or {}).items()):
            if set(spec) == {"b64"}:
                try:
                    raw = base64.b64decode(spec["b64"], validate=True)
                except (binascii.Error, ValueError, TypeError):
                    raise HostError("blob.invalid", f"files.{arg} is not valid base64") from None
                digests[arg] = "sha256:" + hashlib.sha256(raw).hexdigest()
            elif set(spec) == {"blob"}:
                digest = blob_digest(spec["blob"]) if blob_digest is not None else None
                if digest is None:
                    raise HostError("blob.invalid", f"files.{arg} names no blob of yours")
                digests[arg] = digest
            elif set(spec) == {"digest"}:
                digest = spec["digest"]
                if not isinstance(digest, str) or not _HASH.fullmatch(digest):
                    raise HostError("blob.invalid", f"files.{arg}.digest is not a sha256 digest")
                if idem is None or self.read(self.key(self.namespace, principal, idem)) is None:
                    raise HostError("blob.invalid", f"files.{arg}: a digest names a file only on a retry")
                digests[arg] = digest
                by_digest.add(arg)
            else:
                raise HostError("blob.invalid", f"files.{arg} must be one of b64, blob, digest")
        return digests, frozenset(by_digest)

    # -- the write path
    def begin(
        self,
        principal: str,
        idem: str,
        op: str,
        args: dict[str, Any],
        *,
        planned: Sequence[Planned],
        files: Mapping[str, str] | None = None,
        session: str | None = None,
        digest_args: frozenset[str] = frozenset(),
        deferred: bool = False,
    ) -> Intent | dict[str, Any]:
        """A new ``pending`` record (fsynced before the caller appends), or the stored answer of a duplicate.

        ``args`` are the normalised args, ``planned`` every event the call will append, in order (never empty: a
        crash after an append with nothing planned could not be told from one before it, and the retry would append
        again). ``deferred``: the call cannot know its events yet (a registry handler plans them itself, H4a1); the
        record starts with none, and :meth:`plan` adds each batch **before** it is appended. A record with nothing
        planned never had an append, so reconciliation removes it. A duplicate of a running call waits for it, at most
        ``wait_seconds``. Raises ``idem.conflict``
        (another binding), ``idem.expired`` (a tombstone), the stored error of a ``failed`` or ``partial`` record,
        ``blob.invalid`` (a ``{digest}`` file where the op would have to run again), ``outcome.unknown`` (an earlier
        attempt that cannot be reconciled yet, or a running one that did not finish in time) and ``quota.host``.
        """
        if not planned and not deferred:
            raise ValueError("a write plans at least one event")
        key = self.key(self.namespace, principal, idem)
        binding = binding_of(op, args, session, files)
        while True:
            stale = None
            with self._lock:
                running = self._running.get(key)
                if running is None:
                    record = self.read(key)
                    if record is None:
                        if digest_args:
                            raise HostError("blob.invalid", "the op has to run again: upload the file again")
                        self._admit(principal)
                        record = {
                            "idem": idem,
                            "namespace": self.namespace,
                            "principal": principal,
                            "binding": binding,
                            "op": op,
                            "args_normalised": args,
                            "files": dict(files or {}),
                            "session": session,
                            "planned": [{k: v for k, v in p._asdict().items() if v is not None} for p in planned],
                            "step": self.START,
                            "at": int(self.clock()),
                        }
                        self._write(key, record)
                        self._running[key] = threading.Event()
                        return Intent(key, record)
                    if record.get("principal") != principal or record.get("namespace") != self.namespace:
                        raise HostError("internal", "an outcome record does not belong to its key")
                    if record.get("binding") != binding:
                        raise HostError("idem.conflict", "this idem was used for another request")
                    if record["step"] == "tombstone":
                        raise HostError("idem.expired", "this idem has expired")
                    if record["step"] != self.START:
                        return self._replay(record)
                    self._running[key] = threading.Event()  # a stale pending record: this call reconciles it
                    stale = record
            if stale is None:
                if not running.wait(self.wait_seconds):
                    raise HostError("outcome.unknown", f"{op} with this idem is still running; retry or outcome.get")
                continue
            settled = self._settle(key, stale)
            if settled is not None and settled["step"] == self.START:
                raise HostError("outcome.unknown", f"an earlier attempt of {op} cannot be reconciled yet; retry")

    @staticmethod
    def _replay(record: dict[str, Any]) -> dict[str, Any]:
        if record["step"] == "committed":
            return {**record["result"], "duplicate": True}
        raise HostError.stored(record["error"])

    def plan(self, intent: Intent, planned: Sequence[Planned], key: str | None = None) -> None:
        """Add events to a running ``pending`` record (fsynced) **before** they are appended: the deferred plan of
        :meth:`begin`. An event already listed (by id) is not added twice; ``key`` is the ticket key, kept for the
        result builder."""
        known = {p.get("id") for p in intent.record.get("planned") or []}
        new = [{k: v for k, v in p._asdict().items() if v is not None} for p in planned if p.id not in known]
        if not new and (key is None or intent.record.get("key") == key):
            return
        record = {**intent.record, "planned": [*(intent.record.get("planned") or []), *new]}
        if key is not None:
            record["key"] = key
        with self._lock:
            if self._running.get(intent.key) is None:
                raise HostError("internal", "the outcome record is no longer running")
            self._write(intent.key, record)
            intent.record = record

    def note(self, intent: Intent, **fields: Any) -> None:
        """Add fields to a running ``pending`` record (fsynced), e.g. the id of the request a prepare is about to
        store."""
        record = {**intent.record, **fields}
        with self._lock:
            if self._running.get(intent.key) is None:
                raise HostError("internal", "the outcome record is no longer running")
            self._write(intent.key, record)
            intent.record = record

    def commit(self, intent: Intent, result: dict[str, Any]) -> dict[str, Any]:
        """The change landed: store the (redacted) ``result``; the record is ``committed``."""
        return self._finish(intent, {"step": "committed", "result": result, "exit": 0})

    def refuse(self, intent: Intent, error: HostError) -> dict[str, Any] | None:
        """A refusal of the op **before any planned event landed** (:meth:`execute` checks). A non-retryable one is
        stored (``failed``); a retryable one stores nothing, so the retry runs again."""
        if error.retryable:
            self.abandon(intent)
            return None
        return self.fail(intent, error)

    def partial(self, intent: Intent, seqs: Sequence[int], error: HostError | None = None) -> dict[str, Any]:
        """A multi-event registry call whose first events landed (``seqs``): the stored error, as P1's ``apply``."""
        error = error or HostError(
            "invalid.input", f"an earlier attempt of this call was interrupted after {len(seqs)} event(s)"
        )
        return self._finish(
            intent, {"step": "partial", "error": error.error(), "exit": error.exit, "seqs": [int(s) for s in seqs]}
        )

    def release(self, intent: Intent) -> None:
        """Stop running ``intent`` but keep it ``pending``: the next lookup or duplicate reconciles it from the log."""
        super().release(intent)

    def execute(
        self,
        principal: str,
        idem: str,
        op: str,
        args: dict[str, Any],
        apply: Callable[[Intent], dict[str, Any]],
        *,
        planned: Sequence[Planned] | None,
        files: Mapping[str, str] | None = None,
        session: str | None = None,
        digest_args: frozenset[str] = frozenset(),
        before: Callable[[], None] | None = None,
        redactor: Any = None,
    ) -> dict[str, Any]:
        """``begin``, ``apply`` (which appends the planned events and returns the result) and the end of the record.

        A refusal from ``apply`` (:class:`HostError`, or the store's, as one) is not believed until the log has been
        asked: nothing landed, the refusal is stored (``failed``) or, if retryable, forgotten; some landed,
        the record is ``partial`` (``human.submit``: stays ``pending``); all landed, it is ``committed`` and the rebuilt
        result is returned. Any other failure leaves the record ``pending``: the log says what landed. Returns the
        result (a duplicate's has ``duplicate: true``).

        ``planned`` ``None`` is a deferred plan (:meth:`begin`): ``apply`` calls :meth:`plan` before each append.
        ``before`` runs after a new record is made and before ``apply`` (the workspace-state checks of §0.6 step 10,
        which come after the replay of step 9); if it raises, the record is removed and nothing ran.

        ``redactor`` (``obj(value)`` and ``error(HostError)``, D-P2-10): every error and every result rebuilt from the
        log passes through it **before** the record stores it.
        """
        from orch.store import StoreError

        begun = self.begin(
            principal,
            idem,
            op,
            args,
            files=files,
            session=session,
            planned=planned or (),
            digest_args=digest_args,
            deferred=planned is None,
        )
        if isinstance(begun, dict):
            return begun
        if before is not None:
            try:
                before()
            except BaseException:
                self.abandon(begun)
                raise
        clean = redactor.error if redactor is not None else (lambda e: e)
        try:
            result = apply(begun)
        except HostError as exc:
            return self._refused(begun, clean(exc), redactor)
        except StoreError as exc:  # the model's refusal (or store.busy): its disposition comes from the host table
            return self._refused(begun, clean(HostError(exc.code, exc.detail or exc.code)), redactor)
        except BaseException:
            self.release(begun)
            raise
        return self.commit(begun, result)["result"]

    def _refused(self, intent: Intent, error: HostError, redactor: Any = None) -> dict[str, Any]:
        """``apply`` raised ``error``: ask the log what landed, then end the record accordingly (raise or return)."""
        try:
            with self.reconciler.locked() if self.reconciler is not None else contextlib.nullcontext():
                found = self._find(intent.record)
                fields = self._fields(intent.record, found) if found is not None else "keep"
        except BaseException:
            self.release(intent)  # the log cannot say: keep the record, the next call reconciles it
            raise error from None
        if fields == "none":
            self.refuse(intent, error)
            raise error
        if fields == "keep":
            self.release(intent)
            raise error
        assert isinstance(fields, dict)
        if redactor is not None:
            fields = {**fields, **{k: redactor.obj(fields[k]) for k in ("result", "error") if k in fields}}
        record = self._finish(intent, fields)
        if record["step"] == "committed":
            return record["result"]
        raise HostError.stored(record["error"])

    # -- reconciliation
    def _find(self, record: dict[str, Any]) -> list[dict[str, Any] | None] | None:
        """For each planned event the matching logged event or ``None``; ``None`` when there is no reconciler. Call
        inside ``reconciler.locked()``. Store errors propagate (the log cannot be read)."""
        reconciler = self.reconciler
        if reconciler is None:
            return None
        found = []
        for planned in record.get("planned") or []:
            event = reconciler.find(planned["log"], planned["id"])
            found.append(event if event is not None and _matches(planned, event) else None)
        return found

    def _fields(self, record: dict[str, Any], found: list[dict[str, Any] | None]) -> dict[str, Any] | str:
        """The terminal fields the log's answer gives, ``"none"`` (nothing landed) or ``"keep"`` (stay pending)."""
        if record["op"] == "human.prepare" and record.get("request_id"):
            return self.prepared(record) if self.prepared is not None else "keep"
        landed = [e for e in found if e is not None]
        if found and len(landed) == len(found):
            try:
                result = self.reconciler.rebuild(record, landed)  # type: ignore[union-attr]
            except HostError as exc:  # a builder's own verdict (relay.submit: event.duplicate_id, §11A.4 R2)
                return {"step": "failed", "error": exc.error(), "exit": exc.exit}
            except Exception:  # noqa: BLE001 - a builder bug: the write landed, so it must not run again
                LOG.exception("cannot rebuild the result of %s from the log", record["op"])
                error = HostError("internal", f"{record['op']} landed but its result could not be rebuilt")
                return {"step": "failed", "error": error.error(), "exit": error.exit}
            return {"step": "committed", "result": result, "exit": 0}
        if landed and record["op"] in NO_PARTIAL:  # rolled forward (H5a, §11A.4), never partial
            hook = self.relay_fields if record["op"] in _RELAY_WRITES else self.roll_forward
            return hook(record) if hook is not None else "keep"
        if landed:
            error = HostError(
                "invalid.input", f"an earlier attempt of this call was interrupted after {len(landed)} event(s)"
            )
            return {
                "step": "partial",
                "error": error.error(),
                "exit": error.exit,
                "seqs": [int(e["seq"]) for e in landed],
            }
        return "none"

    def _settle(self, key: str, record: dict[str, Any]) -> dict[str, Any] | None:
        """Reconcile a stale ``pending`` record whose key this call holds, then give the key back. Returns the record
        as it is now (``pending`` if the log could not say), ``None`` if it was removed."""
        try:
            return self._reconcile(key, record)
        finally:
            with self._lock:
                self._release(key)

    def _reconcile(self, key: str, record: dict[str, Any]) -> dict[str, Any] | None:
        from orch.store import StoreError

        try:
            with self.reconciler.locked() if self.reconciler is not None else contextlib.nullcontext():
                found = self._find(record)
                fields = self._fields(record, found) if found is not None else "keep"
                if fields == "keep":
                    return record
                with self._lock:
                    if fields == "none":
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(self._path(key))
                        self._drop(key)
                        return None
                    settled = {**record, **fields, "done_at": int(self.clock())}
                    self._write(key, settled)
                    return settled
        except (HostError, StoreError, OSError):
            return record  # the log cannot be read now (unavailable store...): still pending

    def reconcile_all(self) -> list[tuple[str, str]]:
        """At start: reconcile every ``pending`` record no call is running (from the index: only those records are
        read). Returns (op, outcome) for each, where outcome is the new step or ``removed``."""
        with self._lock:
            names = [n for n, e in self._idx.items() if e[1] == self.START and n not in self._running]
        out = []
        for name in sorted(names):
            with self._lock:
                if name in self._running:
                    continue
                record = self.read(name)
                if record is None or record.get("step") != self.START:
                    continue
                self._running[name] = threading.Event()
            settled = self._settle(name, record)
            out.append((record["op"], "removed" if settled is None else settled["step"]))
        return out

    # -- retention
    def sweep(self) -> None:
        """Terminal records older than 24 h become tombstones, tombstones older than 7 days go. Works from the index
        and reads only the records it changes, a batch at a time (the lock is not held across batches). The host
        calls this periodically, off the event loop."""
        now = self.clock()
        with self._lock:
            due = [
                (name, entry[1] == "tombstone")
                for name, entry in self._idx.items()
                if name not in self._running
                and (
                    (entry[1] == "tombstone" and now - entry[2] > TOMBSTONE_SECONDS)
                    or (entry[1] in self.TERMINAL and now - entry[2] > KEEP_SECONDS)
                )
            ]
        for start in range(0, len(due), 200):
            with self._lock:
                for name, is_tombstone in due[start : start + 200]:
                    if name in self._running or name not in self._idx:
                        continue
                    if is_tombstone:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(self._path(name))
                        self._drop(name)
                    else:
                        record = self.read(name)
                        if record is not None and record.get("step") in self.TERMINAL:
                            self._tombstone(name, record, now)

    def settle(self, key: str, outcome: Mapping[str, Any]) -> None:
        """Start-up recovery of a ``human.submit`` rolled forward (§0.8.1): its ``pending`` record gets the request's
        terminal outcome (``committed`` with the result, or ``failed`` with the error), never ``partial``."""
        with self._lock:
            if key in self._running:
                return
            record = self.read(key)
            if record is None or record.get("step") != self.START:
                return
            fields: dict[str, Any] = {"step": outcome["state"], "exit": outcome["exit"], "done_at": int(self.clock())}
            for name in ("result", "error"):
                if name in outcome:
                    fields[name] = outcome[name]
            self._write(key, {**record, **fields})

    # -- outcome.get
    def lookup(self, principal: str, idem: str) -> dict[str, Any]:
        """``outcome.get`` for ``principal``: ``{state, result?, error?, exit}``, never a frame. No record of this
        principal is ``outcome.unknown_idem``; a tombstone is ``idem.expired``."""
        key = self.key(self.namespace, principal, idem)
        stale = None
        with self._lock:
            running = key in self._running
            record = self.read(key)
            if record is None or record.get("principal") != principal or record.get("namespace") != self.namespace:
                raise HostError("outcome.unknown_idem", "no outcome for this idem")
            if record.get("step") == self.START and not running:
                self._running[key] = threading.Event()  # claimed only after the checks above
                stale = record
        if stale is not None:
            record = self._settle(key, stale)
            if record is None:
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



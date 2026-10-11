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

**Retention.** Records live 24 h, then become tombstones for 7 days (``idem.expired``). At most 100 000 records per
principal: past that the oldest completed ones become tombstones early.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, Protocol

from .custody import ensure_private_dir
from .errors import HostError
from .intents import Intent, IntentStore, binding_of

__all__ = [
    "BUILDERS",
    "CONTROL_WRITES",
    "MAX_RECORDS",
    "NO_PARTIAL",
    "Category",
    "OutcomeStore",
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
CONTROL_WRITES = frozenset({"host.register", "host.unregister", "host.bootstrap"})
#: ops that are log writes though no registry operation carries them
_HUMAN_WRITES = frozenset({"human.submit"})
#: every other host op (§0.7 table): reads and side-effect-free ops, whose ``opts.idem`` is ignored
_READS = frozenset(
    {
        "human.prepare", "human.get", "human.pending", "human.cancel",
        "blob.put", "blob.drop", "sub.open", "sub.close",
        "outcome.get", "host.outcome", "job.next", "job.heartbeat",
        "host.list", "host.status", "host.restart", "whoami",
        "ticket.query", "ticket.get", "needs", "diff", "artifact.get",
        "workspace.show", "member.list", "session.list", "grant.list",
    }
)  # fmt: skip
#: a multi-event write that is never ``partial``: it rolls forward (§0.8.1, H5a)
NO_PARTIAL = frozenset({"human.submit"})
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


# --- classification --------------------------------------------------------------------------------------------


def classify(op: str) -> Category:
    """The write category of ``op`` (§0.7). An op nobody serves is ``unknown_command``."""
    if op in CONTROL_WRITES:
        return "control"
    if op in _HUMAN_WRITES:
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
    return op == "human.prepare" or (op not in _HUMAN_WRITES and classify(op) == "log")


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
    ``uid:<n>`` (F4): a grant id never makes a principal by itself."""
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


# --- the store --------------------------------------------------------------------------------------------------


class OutcomeStore(IntentStore):
    """``<state>/hosts/<ws>/outcomes/<key>`` (0600), keyed by ``(workspace id, principal, idem)``.

    The methods block (locks, fsync, the store lock) and belong on the workspace worker thread. A call that is running
    holds its key in memory; a duplicate of it waits and then gets its result.
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
        self._counts: dict[str, int] = {}
        self._count_lock = threading.Lock()

    def open(self) -> None:
        ensure_private_dir(self.dir.parent.parent, self.host_uid)
        ensure_private_dir(self.dir.parent, self.host_uid)
        ensure_private_dir(self.dir, self.host_uid)
        counts: dict[str, int] = {}
        for name in os.listdir(self.dir):
            record = self.read(name) if len(name) == 64 else None
            if record is not None and record.get("step") != "tombstone":
                principal = record.get("principal", "")
                counts[principal] = counts.get(principal, 0) + 1
        with self._count_lock:
            self._counts = counts

    # -- counting (quota)
    def _bump(self, principal: str, by: int) -> None:
        with self._count_lock:
            self._counts[principal] = max(0, self._counts.get(principal, 0) + by)

    def count(self, principal: str) -> int:
        with self._count_lock:
            return self._counts.get(principal, 0)

    def _tombstone(self, name: str, record: dict[str, Any], now: float) -> None:
        super()._tombstone(name, record, now)
        self._bump(record["principal"], -1)

    def abandon(self, intent: Intent) -> None:
        super().abandon(intent)
        self._bump(intent.record["principal"], -1)

    def _admit(self, principal: str) -> None:
        """Called with the lock held, before a record is created: at the limit the oldest completed records become
        tombstones early (about 1 % at a time), and with none to spare the write is ``quota.host``."""
        if self.count(principal) < self.max_records:
            return
        done = []
        for name in os.listdir(self.dir):
            if len(name) != 64 or name in self._running:
                continue
            record = self.read(name)
            if record and record.get("principal") == principal and record.get("step") in self.TERMINAL:
                done.append((record.get("done_at", 0), name, record))
        done.sort(key=lambda item: item[:2])
        want = self.count(principal) - self.max_records + max(1, self.max_records // 100)
        for _, name, record in done[: max(want, 1)]:
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
        files: Mapping[str, str] | None = None,
        session: str | None = None,
        planned: Sequence[tuple[str, str]] = (),
        digest_args: frozenset[str] = frozenset(),
    ) -> Intent | dict[str, Any]:
        """A new ``pending`` record (fsynced before the caller appends), or the stored answer of a duplicate.

        ``args`` are the normalised args, ``planned`` the ``(log, event id)`` of every event the call will append, in
        order. A duplicate of a running call waits for it. Raises ``idem.conflict`` (another binding),
        ``idem.expired`` (a tombstone), the stored error of a ``failed`` or ``partial`` record, ``blob.invalid`` (a
        ``{digest}`` file where the op would have to run again), ``outcome.unknown`` (an earlier attempt that cannot
        be reconciled yet) and ``quota.host``.
        """
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
                            "planned": [{"log": log, "id": event_id} for log, event_id in planned],
                            "step": self.START,
                            "at": int(self.clock()),
                        }
                        self._write(key, record)
                        self._bump(principal, 1)
                        self._running[key] = threading.Event()
                        return Intent(key, record)
                    if record.get("binding") != binding:
                        raise HostError("idem.conflict", "this idem was used for another request")
                    if record["step"] == "tombstone":
                        raise HostError("idem.expired", "this idem has expired")
                    if record["step"] != self.START:
                        return self._replay(record)
                    self._running[key] = threading.Event()  # a stale pending record: this call reconciles it
                    stale = record
            if stale is None:
                running.wait()
                continue
            settled = self._settle(key, stale)
            if settled is not None and settled["step"] == self.START:
                raise HostError("outcome.unknown", f"an earlier attempt of {op} cannot be reconciled yet; retry")

    @staticmethod
    def _replay(record: dict[str, Any]) -> dict[str, Any]:
        if record["step"] == "committed":
            return {**record["result"], "duplicate": True}
        raise HostError.stored(record["error"])

    def commit(self, intent: Intent, result: dict[str, Any]) -> dict[str, Any]:
        """The change landed: store the (redacted) ``result``; the record is ``committed``."""
        return self._finish(intent, {"step": "committed", "result": result, "exit": 0})

    def refuse(self, intent: Intent, error: HostError) -> dict[str, Any] | None:
        """A refusal of the op. A non-retryable one is stored (``failed``); a retryable one stores nothing, so the
        retry runs again (``store.busy``, ``timeout`` before any append)."""
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
        files: Mapping[str, str] | None = None,
        session: str | None = None,
        planned: Sequence[tuple[str, str]] = (),
        digest_args: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """``begin``, ``apply`` (which appends the planned events and returns the result) and the end of the record.

        A :class:`HostError` (or the store's refusal, as one) from ``apply`` is stored or, if retryable,
        forgotten. Any other failure leaves the record ``pending``: the log says what landed.
        Returns the result (a duplicate's has ``duplicate: true``).
        """
        begun = self.begin(
            principal, idem, op, args, files=files, session=session, planned=planned, digest_args=digest_args
        )
        if isinstance(begun, dict):
            return begun
        from orch.store import StoreError

        try:
            result = apply(begun)
        except HostError as exc:
            self.refuse(begun, exc)
            raise
        except StoreError as exc:  # the model's refusal (or store.busy): stored, or forgotten if retryable
            error = HostError(exc.code, exc.detail or exc.code)
            self.refuse(begun, error)
            raise error from None
        except BaseException:
            self.release(begun)
            raise
        return self.commit(begun, result)["result"]

    # -- reconciliation
    def _settle(self, key: str, record: dict[str, Any]) -> dict[str, Any] | None:
        """Reconcile a stale ``pending`` record whose key this call holds, then give the key back. Returns the record
        as it is now (``pending`` if the log could not say), ``None`` if it was removed."""
        try:
            return self._reconcile(key, record)
        finally:
            with self._lock:
                self._release(key)

    def _reconcile(self, key: str, record: dict[str, Any]) -> dict[str, Any] | None:
        reconciler = self.reconciler
        planned = record.get("planned") or []
        if reconciler is None:
            return record
        try:
            with reconciler.locked():
                events = [reconciler.find(p["log"], p["id"]) for p in planned]
                landed = [e for e in events if e is not None]
                fields: dict[str, Any] | None = None
                if planned and len(landed) == len(planned):
                    fields = {"step": "committed", "result": reconciler.rebuild(record, landed), "exit": 0}
                elif landed and record["op"] not in NO_PARTIAL:
                    error = HostError(
                        "invalid.input", f"an earlier attempt of this call was interrupted after {len(landed)} event(s)"
                    )
                    fields = {
                        "step": "partial",
                        "error": error.error(),
                        "exit": error.exit,
                        "seqs": [int(e["seq"]) for e in landed],
                    }
                elif landed:
                    return record  # human.submit: H5a rolls it forward
                if fields is None:
                    with self._lock:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(self._path(key))
                    self._bump(record["principal"], -1)
                    return None
                settled = {**record, **fields, "done_at": int(self.clock())}
                with self._lock:
                    self._write(key, settled)
                return settled
        except HostError:
            return record  # the log cannot be read now (unavailable store...): still pending
        except Exception:  # noqa: BLE001 - a store that cannot answer leaves the record for the next try
            return record

    def _stale(self) -> Iterator[tuple[str, dict[str, Any]]]:
        for name in sorted(os.listdir(self.dir)):
            if len(name) != 64:
                continue
            with self._lock:
                if name in self._running:
                    continue
                record = self.read(name)
                if record is None or record.get("step") != self.START:
                    continue
                self._running[name] = threading.Event()
            yield name, record

    def reconcile_all(self) -> list[tuple[str, str]]:
        """At start: reconcile every ``pending`` record no call is running. Returns (op, outcome) for each, where
        outcome is the new step or ``removed``."""
        out = []
        for name, record in self._stale():
            settled = self._settle(name, record)
            out.append((record["op"], "removed" if settled is None else settled["step"]))
        return out

    # -- outcome.get
    def lookup(self, principal: str, idem: str) -> dict[str, Any]:
        """``outcome.get`` for ``principal``: ``{state, result?, error?, exit}``, never a frame. No record of this
        principal is ``outcome.unknown_idem``; a tombstone is ``idem.expired``."""
        key = self.key(self.namespace, principal, idem)
        stale = None
        with self._lock:
            running = key in self._running
            record = self.read(key)
            if record is not None and record.get("step") == self.START and not running:
                self._running[key] = threading.Event()
                stale = record
        if record is None or record.get("principal") != principal or record.get("namespace") != self.namespace:
            raise HostError("outcome.unknown_idem", "no outcome for this idem")
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

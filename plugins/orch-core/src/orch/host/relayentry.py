"""The relay entry's host side (orch-v2-host §11A.4, R2): the :class:`orch.relay.bridge.HostPort` that the bridge calls.

A tiny adapter, kept in ``orch.host`` because ``orch.host`` is import-banned in ``orch.relay`` (11A.2). Every method
runs on the workspace's worker thread (the bridge reaches it through ``RelayWorker.submit``; recovery at open calls it
there directly). It builds relay calls exactly as H12a's R5 interface says: ``Call(service, …)`` for the identity's own
workspace, ``Call.relay = identity``, ``peer = identity.peer()``, ``columns`` = the relay recompute (steps 1 to 3 and
the scopes again, inside the op's own job), then :meth:`Call.authorize`; it appends only through
:func:`orch.host.human.append_relay`.

``relay.submit`` (O5, orch-52 15 Oct 2026, ruling A): ONE store-lock span (``store.locked()`` re-reads the store
first) holding, in order, the outcome record by ``(ws, r:<device>, idem = event id)`` (a known idem replays its
outcome), the log dedup (O2: an event id already in the log is a committed duplicate, ``duplicate: true``, when its
signed digest and actor equal the submitted event's, otherwise ``event.duplicate_id``; never by signature bytes),
and ONLY THEN, for a never-seen event, the ULID window against the HOST clock (``event.expired``); then the target
and key, ``judge_events``, ``outcomes.plan`` (fsynced), the append and a log check. Every core helper of those steps
is reached through ONE seam, :mod:`orch.host.relayadmit` (H12e's helpers, #474, and #482's
``device_standing``). A model exception that is not a refusal (an unjudgeable event) is ``validation.event``, a definite
refusal, never a stuck write (review I3).

Errors: a :class:`HostError` becomes a sealed :class:`orch.relay.framing.Refusal` with its code (``retryable`` is the
bridge's, derived from the retry table; ``next_key`` from ``detail``); a :class:`orch.store.StoreError` (``store.busy``
from the lock, and any other store refusal) too: it never escapes as a non-refusal (R2-6).

Not wired here (R5): registering :func:`register` at open (the ``relay_fields`` hook), starting the worker.
``WorkspaceService.open`` is unchanged. The result builder of a reconciled ``relay.submit`` is
:mod:`orch.host.relaybuild`, registered once at import of :mod:`orch.host.outcomes` (review M1).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from orch.relay import submit as relay_submit
from orch.relay.framing import REASONS as READER_REASONS
from orch.relay.framing import Identity, Refusal
from orch.store import StoreError
from orch.store.pins import HostPins, pair

from . import relayadmit
from .auth import Verified
from .columns import Column, resolve_relay
from .errors import HostError
from .human import append_relay
from .outcomes import Planned
from .relaybuild import rebuild_relay_submit, same_event
from .relayid import RelayIdentity
from .workspace import Call

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = ["RelayEntry", "rebuild_relay_submit", "register", "same_event"]


def _refusal(exc: HostError | StoreError) -> Refusal:
    """The sealed form of a host or store error: its code and, for ``ticket.key_not_next``, the advisory
    ``next_key`` (H12e: ``HostError.detail``); for the reader codes (H12d-3) the ``reason`` and, for
    ``reader_too_old``, ``required`` and ``declared``. ``retryable`` is not carried: the bridge derives it (ruling
    C)."""
    fields: dict[str, Any] = {}
    detail = getattr(exc, "detail", None)
    nk = detail.get("next_key") if isinstance(detail, dict) else None
    if exc.code == "ticket.key_not_next" and isinstance(nk, str):
        fields["next_key"] = nk
    if exc.code in READER_REASONS and isinstance(detail, dict):  # H12d-3: the closed reason (bridge re-checks it)
        for k in ("reason", "required", "declared"):
            if k in detail:
                fields[k] = detail[k]
    return Refusal(exc.code, **fields)


def _ident(identity: Identity) -> RelayIdentity:
    return RelayIdentity(
        identity.workspace_id,
        identity.device_id,
        identity.person_id,
        identity.cert_digest,
        identity.epoch,
        identity.list_hash,
        identity.rd,
    )


class RelayEntry:
    """``HostPort`` over one :class:`WorkspaceService` and its socket backend (``RegistryBackend``: the same ops)."""

    def __init__(self, service: WorkspaceService, backend: Any) -> None:
        self._service, self._backend = service, backend  # private: orch.relay never reaches the service (R2-13)

    def _call(
        self, identity: Identity, op: str, args: dict[str, Any], recheck: Callable[[], bool], idem: str | None
    ) -> Call:
        ident = _ident(identity)
        frame: dict[str, Any] = {"op": op, "args": args, "opts": {} if idem is None else {"idem": idem}}

        def columns() -> tuple[Column, str | None, Verified]:
            return resolve_relay(bool(recheck())), ident.person_id, Verified("absent")

        call = Call(
            self._service, None, ident.peer(), frame, Column("N", "no-person"), None, Verified("absent"),  # type: ignore[arg-type]
            ident.principal, columns=columns, relay=ident,
        )  # fmt: skip
        return call

    # -- HostPort -------------------------------------------------------------------------------------------------

    def standing(self, device_id: str, person_id: str) -> str:
        """11A.4 step 3 on fresh state (``store.refresh()`` first): ``revoked`` (F1 or HostPins evidence for exactly
        this pair), ``not_member`` unless the person is a current member AND the F1 device standing is ``ok`` (a
        removed device, a removed member's device or another person's device: review I2), else ``ok``."""
        store = self._service.store
        if store is None:
            return "unavailable"
        try:
            with store.locked():
                store.refresh()
                state = store.state
                if (device_id, person_id) in state.revocation_evidence():
                    return "revoked"
                held = HostPins(self._service.state_dir, self._service.workspace_id).revocations()
                if any(pair(rec) == (device_id, person_id) for rec in held):
                    return "revoked"
                if person_id not in state.workspace.members:
                    return "not_member"
                if relayadmit.device_standing(store, person_id, device_id) != "ok":
                    return "not_member"
                return "ok"
        except Exception:  # noqa: BLE001 - any doubt: the store cannot say now (silence; the device resends)
            return "unavailable"

    def read(self, identity: Identity, op: str, args: dict[str, Any], recheck: Callable[[], bool]) -> dict:
        try:
            return self._backend.serve(self._call(identity, op, args, recheck, None))
        except (HostError, StoreError) as exc:
            raise _refusal(exc) from None

    def lookup(self, identity: Identity, idem: str) -> dict[str, Any]:
        try:
            return self._service.outcomes.lookup(identity.principal, idem)
        except (HostError, StoreError) as exc:
            raise _refusal(exc) from None

    def submit(
        self, identity: Identity, idem: str, op: str, log: str, events: list[dict], recheck: Callable[[], bool]
    ) -> dict[str, Any]:
        """``relay.submit`` after the bridge's lock-free checks: ONE ``store.locked()`` span (orch-52 final, 15 Oct
        2026) holding, in order: the refresh (``locked`` re-reads), the identity (:meth:`Call.authorize` with the
        recheck), the outcome record (replay or conflict of ``(ws, r:<device>, event id)``), then :meth:`_apply`.
        ``store.busy`` (the lock) and every other store or host error is a sealed refusal, never a raw exception."""
        service = self._service
        # the binding is the event identity (signed content and actor), never the signature bytes: an identical
        # event signed again is the same request (orch-52 final); the signed events are noted on the record
        args = {"log": log, "events": [{k: v for k, v in e.items() if k != "sig"} for e in events]}
        try:
            store = service.store
            if store is None or service.state == "unavailable":
                raise HostError("workspace.unavailable", "the workspace cannot be served")
            if service.state == "degraded":
                raise HostError("workspace.degraded", "the workspace is read-only")

            def apply(intent: Any) -> dict[str, Any]:
                return self._apply(intent, identity, log, events)

            with store.locked():  # the one span; the store is re-read first
                self._call(identity, "relay.submit", args, recheck, idem).authorize()
                return service.outcomes.execute(identity.principal, idem, "relay.submit", args, apply, planned=None)
        except (HostError, StoreError) as exc:
            raise _refusal(exc) from None

    def _apply(self, intent: Any, identity: Identity, log: str, events: list[dict]) -> dict[str, Any]:
        """Inside the span, after the outcome record: the log dedup, THEN the window for a never-seen event (ruling A,
        host clock), the target and key, the judgement, the plan, the append and the log check."""
        service = self._service
        store = service.store
        service.outcomes.note(intent, signed_events=events)  # fsynced: what relay_fields rolls forward
        ws = service.workspace_id
        ev = events[0]
        seq = relayadmit.known_or_fresh(store, log, ev, service.clock())
        if seq is not None:  # an identical landed event: committed, duplicate (before anything is judged)
            return relay_submit.result(log, [seq], duplicate=True)
        relayadmit.check_target(store, log, ev)
        try:
            relayadmit.judge_events(store, log, events)
        except (HostError, StoreError):
            raise
        except Exception:  # noqa: BLE001 - an event the model cannot judge: a definite refusal, never a stuck write
            raise HostError("validation.event", "the event cannot be judged") from None
        service.outcomes.plan(intent, [Planned(log, e["id"], e["type"], e["actor"]) for e in events])  # fsynced
        landed, error = append_relay(
            service, log, events, [{} for _ in events], person_id=identity.person_id, device_id=identity.device_id
        )
        if error is not None:
            raise error
        logged = store.logged_event(log, ev["id"])  # the log check before the outcome is classified
        if logged is None or not same_event(ws, log, logged, ev):
            raise HostError("internal", "the appended event is not in the log as submitted")
        return relay_submit.result(log, landed)


def register(service: WorkspaceService) -> None:
    """R5 calls this at open, after ``human.recover``: the ``relay_fields`` roll-forward hook of this service (per
    service). The result builder is not registered here: :mod:`orch.host.relaybuild` is, once, at import."""

    def append(log: str, events: list[dict], extras: list[dict], person: str, device: str) -> tuple[list[int], Any]:
        return append_relay(service, log, events, extras, person_id=person, device_id=device)

    service.outcomes.relay_fields = relay_submit.make_relay_fields(append)

"""The relay entry's host side (orch-v2-host §11A.4, R2): the :class:`orch.relay.bridge.HostPort` that the bridge calls.

A tiny adapter, kept in ``orch.host`` because ``orch.host`` is import-banned in ``orch.relay`` (11A.2). Every method
runs on the workspace's worker thread (the bridge reaches it through ``RelayWorker.submit``; recovery at open calls it
there directly). It builds relay calls exactly as H12a's R5 interface says: ``Call(service, …)`` for the identity's own
workspace, ``Call.relay = identity``, ``peer = identity.peer()``, ``columns`` = the relay recompute (steps 1 to 3 and
the scopes again, inside the op's own job), then :meth:`Call.authorize`; it appends only through
:func:`orch.host.human.append_relay`.

``relay.submit`` (O5, orch-52 15 Oct 2026): the outcome record, the step-9b duplicate check (O2), ``HumanOps._judge``,
``outcomes.plan`` (fsynced) and the append run in ONE store-lock span (``store.locked()`` re-reads the store first).
O2: an event id already in the log is a committed duplicate (``duplicate: true``) when its signed content (the bytes
the device signs, F1 §5.3) and its actor equal the submitted event's; otherwise ``event.duplicate_id``. Dedup is by
event identity, not by signature bytes (ECDSA signatures are randomised).

Not wired here (R5): registering :func:`register` at open, starting the worker. ``WorkspaceService.open`` is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from orch import canon
from orch.relay import submit as relay_submit
from orch.relay.framing import Identity, Refusal
from orch.store.pins import HostPins, pair

from .auth import Verified
from .columns import Column, resolve_relay
from .errors import HostError
from .human import append_relay
from .outcomes import BUILDERS, Planned
from .relayid import RelayIdentity
from .workspace import Call

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = ["RelayEntry", "register"]


def _refusal(exc: HostError) -> Refusal:
    fields: dict[str, Any] = {"retryable": bool(exc.retryable)}
    nk = getattr(exc, "next_key", None)
    if isinstance(nk, str):
        fields["next_key"] = nk  # advisory (ticket.key_not_next, orch-52 final)
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
        self.service, self.backend = service, backend

    def _call(
        self, identity: Identity, op: str, args: dict[str, Any], recheck: Callable[[], bool], idem: str | None
    ) -> Call:
        ident = _ident(identity)
        frame: dict[str, Any] = {"op": op, "args": args, "opts": {} if idem is None else {"idem": idem}}

        def columns() -> tuple[Column, str | None, Verified]:
            return resolve_relay(bool(recheck())), ident.person_id, Verified("absent")

        call = Call(
            self.service, None, ident.peer(), frame, Column("N", "no-person"), None, Verified("absent"),  # type: ignore[arg-type]
            ident.principal, columns=columns, relay=ident,
        )  # fmt: skip
        return call

    # -- HostPort -------------------------------------------------------------------------------------------------

    def standing(self, device_id: str, person_id: str) -> str:
        store = self.service.store
        if store is None:
            return "unavailable"
        try:
            with store.locked():
                state = store.state
                if (device_id, person_id) in state.revocation_evidence():
                    return "revoked"
                held = HostPins(self.service.state_dir, self.service.workspace_id).revocations()
                if any(pair(rec) == (device_id, person_id) for rec in held):
                    return "revoked"
                if state.device_person(device_id) != person_id or person_id not in state.workspace.members:
                    return "not_member"
                return "ok"
        except Exception:  # noqa: BLE001 - any doubt: the store cannot say now (silence; the device resends)
            return "unavailable"

    def read(self, identity: Identity, op: str, args: dict[str, Any], recheck: Callable[[], bool]) -> dict:
        try:
            return self.backend.serve(self._call(identity, op, args, recheck, None))
        except HostError as exc:
            raise _refusal(exc) from None

    def lookup(self, identity: Identity, idem: str) -> dict[str, Any]:
        try:
            return self.service.outcomes.lookup(identity.principal, idem)
        except HostError as exc:
            raise _refusal(exc) from None

    def submit(
        self, identity: Identity, idem: str, op: str, log: str, events: list[dict], recheck: Callable[[], bool]
    ) -> dict[str, Any]:
        """``relay.submit`` after the bridge's lock-free checks: ONE ``store.locked()`` span (orch-52 final, 15 Oct
        2026) holding, in order: the refresh (``locked`` re-reads), the identity (:meth:`Call.authorize` with the
        recheck), the outcome record (replay or conflict of ``(ws, r:<device>, event id)``), the dedup by event
        identity, the log, the key, :func:`judge_events`, ``outcomes.plan`` (fsynced), ``append_relay`` and a log check
        before the outcome is classified."""
        service = self.service
        args = {"log": log, "events": events}
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
        except HostError as exc:
            raise _refusal(exc) from None

    def _apply(self, intent: Any, identity: Identity, log: str, events: list[dict]) -> dict[str, Any]:
        service = self.service
        store = service.store
        ws = service.workspace_id
        ev = events[0]
        found = store.logged_event(log, ev["id"])
        if found is not None:  # dedup by event identity (step 9b), before anything is judged
            if not same_event(ws, log, found, ev):
                raise HostError("event.duplicate_id", "an event with this id and other content is in the log")
            return relay_submit.result(log, [int(found["seq"])], duplicate=True)
        exists = store.log_head(log) is not None or store.ticket(log, heal=False) is not None
        if ev["type"] == "ticket.created":
            if exists:  # ticket.exists means only that this uid exists
                raise HostError("ticket.exists", "a ticket with this uid exists")
            want = store._next_key()  # noqa: SLF001 - TODO(orch-52): the public next-key helper
            if ev.get("key") != want:
                err = HostError("ticket.key_not_next", "the key is not the next free key")
                err.next_key = want  # type: ignore[attr-defined]
                raise err
        elif not exists:
            raise HostError("not_found", "no such ticket")
        judge_events(store, log, events)
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


def same_event(workspace_id: str, log: str, a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Event identity (orch-52 final): the digest of the signed content (the event without ``sig`` and the host-stamped
    fields, F1 §5.3) and the actor; never the signature bytes (ECDSA signatures are randomised, high-s accepted)."""
    try:
        return a.get("actor") == b.get("actor") and canon.person_signing_bytes(
            workspace_id, log, a
        ) == canon.person_signing_bytes(workspace_id, log, b)
    except Exception:  # noqa: BLE001 - unbuildable bytes are not the same event
        return False


def judge_events(store: Any, log: str, events: list[dict[str, Any]]) -> None:
    """All events together with ``model.preview`` under the store lock (signatures verified), as
    ``HumanOps._judge`` does for the person path, without a request record. TODO(orch-52): replace with core's
    ``judge_events`` helper when it lands (#465)."""
    from orch.model import Refusal as ModelRefusal
    from orch.model import preview
    from orch.store import StoreError

    state = store.state
    stamp = store.peek_stamp(log)
    seq, prev, at = stamp["seq"], stamp["prev"], stamp["at"]
    for ev in events:
        probe = {**ev, "seq": seq, "prev": prev, "at": at}
        if "ws_seq" in stamp:
            probe["ws_seq"] = stamp["ws_seq"]
        r = preview(state, probe, log=log)
        if isinstance(r, ModelRefusal):
            exc = StoreError.from_refusal(r)
            raise HostError(exc.code, exc.detail or exc.code)
        state, seq, prev = r, seq + 1, canon.event_head(probe)


def _rebuild_for(workspace_id: str) -> Callable[[dict[str, Any], list[dict[str, Any]]], dict[str, Any]]:
    """The result builder of a reconciled ``relay.submit``: crash reconciliation matches event identity (orch-52
    final, O6): a logged event with the planned id but other signed content or actor is ``event.duplicate_id``
    (stored ``failed``), never a false ``committed``."""

    def rebuild(record: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        args = record.get("args_normalised") or {}
        log = str(args.get("log", ""))
        submitted = {e.get("id"): e for e in args.get("events") or [] if isinstance(e, dict)}
        for e in events:
            mine = submitted.get(e.get("id"))
            if mine is None or not same_event(workspace_id, log, e, mine):
                raise HostError("event.duplicate_id", "an event with this id and other content is in the log")
        return relay_submit.result(log, [int(e["seq"]) for e in events])

    return rebuild


def register(service: WorkspaceService) -> None:
    """R5 calls this at open, after ``human.recover``: the ``relay_fields`` roll-forward hook and the result builder
    of a reconciled ``relay.submit``."""

    def append(log: str, events: list[dict], extras: list[dict], person: str, device: str) -> tuple[list[int], Any]:
        return append_relay(service, log, events, extras, person_id=person, device_id=device)

    BUILDERS["relay.submit"] = _rebuild_for(service.workspace_id)
    service.outcomes.relay_fields = relay_submit.make_relay_fields(append)

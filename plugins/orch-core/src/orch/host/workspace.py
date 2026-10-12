"""One registered workspace inside the host (D-P2-1): its worker thread, its store, its state and its socket gate.

The worker thread is the single owner of the mutable :class:`~orch.store.Store`; everything that touches the store
runs on it (:meth:`WorkspaceService.call`). The state is one of

* ``ok``;
* ``degraded``: reads only (an unacknowledged invalid event, a frozen workspace); a write answers
  ``workspace.degraded``;
* ``unavailable``: missing root, a broken or mismatched genesis, unsafe modes; every op answers
  ``workspace.unavailable`` with the cause.

:class:`WorkspaceGate` is the request gate on the workspace socket. It applies §0.6 in order:

1. and 2. (framing, envelope) are the transport's;
3. the op: ``unknown_command`` for an op the backend does not serve, then ``op.unsupported`` when the client's
   ``hello.ops`` names another contract version;
4. drain (``host.restarting``);
5. ``grant.secret_in_args``, then an unverified grant on a write that column G would allow (``grant.required`` or
   ``grant.expired``);
6. the column (``op.not_allowed``, ``human_only``);
7. ``session.admit``: the agent rate (``rate.limited``) and the in-flight quota (``quota.host``).

Steps 3 to 6 run synchronously on the event loop, before ``admit``. Step 5's grant check and step 6 use the
workspace's :class:`~orch.host.auth.AuthView` snapshot (no blocking store access on the loop). When that snapshot is
stale (the workspace log changed since it was built) the gate does not answer from it; in every case the worker thread
then refreshes the store and **recomputes the column before the op runs**, and that answer is the one that counts
(F8: rechecked on every request). Steps 8 to 11 are the backend's (:class:`orch.host.dispatch.RegistryBackend`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from . import frames
from .auth import AuthView, Verified, build_auth, secret_in_request
from .columns import Column, OpKind, refusal, resolve
from .custody import check_tree, check_workspace_files
from .errors import HostError
from .journal import Batch, Cursors, Journal
from .outcomes import OutcomeStore, StoreReconciler
from .registry import Entry
from .requests import RequestStore
from .transport import Peer, Session

if TYPE_CHECKING:
    from .service import Host

__all__ = ["Call", "OpBackend", "StatusBackend", "WorkspaceGate", "WorkspaceService", "ok_result", "person_ok"]

State = Literal["ok", "degraded", "unavailable"]
LOG = logging.getLogger(__name__)
Phase = Literal["read", "write", "wait"]


def person_ok(workspace: Any, person: str, now: float) -> bool:
    """F8: ``person`` is a member with a device that is not removed, revoked or expired and carries ``decide``."""
    from orch.identity.certs import person_scope_ok

    if person not in workspace.members:
        return False
    for device in workspace.devices.values():
        if device.person != person or device.removed or device.revoked:
            continue
        if device.expires_ms is not None and device.expires_ms <= now * 1000:
            continue
        if person_scope_ok({"scopes_max": list(device.scopes)}):
            return True
    return False


def deny_append(log: str, event: dict[str, Any]) -> None:
    """The store's append check outside a write's ``apply`` (reads, dry runs, person reads): no caller event may land
    without an outcome record listing it (§0.7). The host's own healing events are exempt in the store."""
    from orch.store import StoreError

    raise StoreError("internal", f"{event.get('type')}: an append outside a write")


def ok_result(data: Any, *, seq: int = 0, duplicate: bool = False) -> dict[str, Any]:
    return {
        "data": data,
        "key": None,
        "seq": seq,
        "cursor": 0,
        "hints": [],
        "lines": [],
        "duplicate": duplicate,
        "exit": 0,
        "head": None,
    }


class WorkspaceService:
    def __init__(self, entry: Entry, state_dir: Path, host_uid: int, *, clock: Callable[[], float] = time.time):
        self.entry = entry
        self.state_dir = Path(state_dir)
        self.host_uid = host_uid
        self.clock = clock
        self.executor = ThreadPoolExecutor(1, thread_name_prefix=f"orch-ws-{entry.workspace_id[:8]}")
        self.store: Any = None
        self.outcomes = OutcomeStore(
            self.state_dir,
            entry.workspace_id,
            host_uid,
            clock=clock,
            reconciler=StoreReconciler(lambda: self.store),
        )
        self.state: State = "unavailable"
        self.cause = "not opened yet"
        #: the authentication snapshot (:mod:`orch.host.auth`), rebuilt on the worker thread after every refresh
        self.auth: AuthView | None = None
        #: the handlers' capabilities (D-P2-9): built once per workspace on the worker thread, closed with it
        self.caps: Any = None
        #: set while ``unregister`` drains this workspace: new requests answer ``workspace.unavailable``
        self.closing = False
        #: the subscription journal (:mod:`orch.host.journal`, H6a), built at open; worker thread only
        self.journal: Journal | None = None
        #: where the worker hands journal batches (the host's bus, on the event loop); ``None``: nobody listens
        self.publish: Callable[[Batch], None] | None = None
        #: the host's bus (:class:`orch.host.bus.Bus`): subscriptions and parked waits; set by the host
        self.bus: Any = None
        #: cursor authentication, the host's (set by the host; a private key otherwise)
        self.cursors = Cursors(os.urandom(32))
        #: why the journal could not be built ("" while it is fine): the workspace stays degraded
        self.journal_failed = ""
        #: human requests (§0.8.1, H5a): ``<state>/hosts/<ws>/requests/``
        self.requests: RequestStore | None = RequestStore(self.state_dir, entry.workspace_id, host_uid, clock=clock)
        self.outcomes.roll_forward = self._roll_forward

    @property
    def workspace_id(self) -> str:
        return self.entry.workspace_id

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        return self.executor.submit(fn, *args)

    async def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.wrap_future(self.submit(fn, *args))

    # -- on the worker thread
    def _unavailable(self, cause: str) -> None:
        self.state, self.cause = "unavailable", cause[:300]
        if self.store is not None:
            self.store.close()
            self.store = None

    def open(self) -> None:
        from orch.custody import FileBackend
        from orch.store import BackendSigner, Store, StoreError
        from orch.store.pins import HostPins

        wid = self.entry.workspace_id
        try:
            if os.path.realpath(self.entry.root) != self.entry.root:
                raise HostError("custody.unsafe_mode", f"{self.entry.root} now resolves elsewhere")
            check_tree(Path(self.entry.root), self.host_uid)
            check_workspace_files(self.state_dir, wid, Path(self.entry.root), self.host_uid)
            pin = HostPins(self.state_dir, wid).genesis()
            if pin is None:
                raise HostError("trust.genesis_mismatch", "no genesis pin for this workspace")
            keys = self.state_dir / "hosts" / wid / "keys"
            backend = FileBackend(keys) if keys.is_dir() else None
            signer = BackendSigner(backend, "wsk") if backend is not None and backend.exists("wsk") else None
            store = Store.open(
                self.entry.root,
                expected_workspace_id=wid,
                expected_genesis=pin,
                host=signer,
                host_state_dir=self.state_dir,
                clock=self.clock,
                load="lazy",
            )
        except (HostError, StoreError, OSError, ValueError, KeyError) as exc:
            self._unavailable(f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
            return
        if self.store is not None:
            self.store.close()
        self.store = store
        self._judge()
        self._snapshot()
        if self.state != "unavailable":
            try:
                self.outcomes.open()
                if self.requests is not None:
                    from .human import recover

                    self.requests.open()
                    recover(self)  # a crash between two appends of a human submission rolls forward (§0.8.1)
                self.outcomes.reconcile_all()  # a crash between an append and its record (§0.7)
                self.outcomes.sweep()
            except (HostError, OSError, ValueError) as exc:
                self._unavailable(f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
        if self.state != "unavailable":
            self._build_journal(audit=False)

    # -- the subscription journal (H6a, D-P2-12, F11)
    def _build_journal(self, *, audit: bool) -> None:
        """(Re)build the journal from the logs and hook it into the store. A rebuild after a foreign append or a
        restore marks the workspace ``degraded`` while it runs, writes one audit line, and its batch ends every
        subscription with ``reset`` (the bus does that on a reset batch).

        A build that fails leaves no journal: the workspace stays ``degraded`` (:attr:`journal_failed`), every
        subscription ends with ``reset``, ``sub.open`` answers ``workspace.unavailable``, and the next try is the 60 s
        check (no retry on every op)."""
        store = self.store
        if store is None:
            return
        journal = self.journal or Journal(self.entry.workspace_id, self.entry.root, self.cursors)
        if audit:
            self.state, self.cause = "degraded", "the subscription journal is being rebuilt"
            LOG.warning(
                "audit: workspace %s: foreign append or restore (%s); rebuilding the journal, ending subscriptions",
                self.entry.workspace_id,
                journal.cause or self.journal_failed or "start",
            )
        store.before_append, store.on_commit = None, None
        try:
            batch = journal.build(store)
        except Exception as exc:  # noqa: BLE001 - whatever failed, there is no journal to serve from
            self.journal = None
            self.journal_failed = f"journal: {getattr(exc, 'code', type(exc).__name__)}: {exc}"[:300]
            LOG.error("audit: workspace %s: the journal cannot be built: %s", self.entry.workspace_id, exc)
            if self.publish is not None:  # end every subscription on the old feed
                self.publish(Batch(self.entry.workspace_id, "", 0, (), {}, reset=True))
            if self.store is not None:
                self._judge()
            return
        self.journal, self.journal_failed = journal, ""
        store.before_append, store.on_commit = journal.check, journal.committed
        if self.store is not None:
            self._judge()
        if self.publish is not None:
            self.publish(batch)

    def sub_cursor(self, principal: str) -> str:
        """The ``sub_cursor`` of a snapshot read (§0.9), on the worker thread right where the read takes its state:
        the journal head after a foreign-append check, so every event the state shows is in the journal.
        ``ticket.query``, ``ticket.get`` and ``needs`` (H6b) return it; ``sub.open {after}`` with it loses and repeats
        nothing."""
        self.sync(check=True)
        if self.journal is None:
            raise HostError("workspace.unavailable", "the subscription journal is not available")
        return self.journal.cursor(principal)

    def sync(self, *, check: bool = False, retry: bool = False) -> None:
        """After an op (worker thread): look for a foreign append (cheaply; every log with ``check``, the 60 s check
        and ``sub.open``), rebuild a stale journal, else hand its new entries to the bus with a fresh authentication
        snapshot. A journal that failed to build is retried only with ``retry`` (the 60 s check)."""
        store = self.store
        if store is None:
            return
        if self.journal is None:
            if retry and self.journal_failed and self.state != "unavailable":
                self._build_journal(audit=True)
                self._snapshot()
            return
        journal = self.journal
        from orch.store import StoreError

        try:
            if check:
                with store.locked():  # every log's signature, against the files as the lock finds them
                    journal.check(full=True)
            else:
                journal.check()  # O(1): the store's own counter and one stat
        except (StoreError, OSError) as exc:
            LOG.warning("journal check of %s failed: %s", self.entry.workspace_id, exc)
            return
        if journal.stale:
            self._build_journal(audit=True)
            self._snapshot()
            return
        batch = journal.take()
        if batch is None:
            return
        self._snapshot()  # delivery revalidates against this: a revoke or removal it carries counts at once
        if self.publish is not None:
            self.publish(batch)

    def _roll_forward(self, record: dict[str, Any]) -> dict[str, Any] | str:
        from .human import outcome_fields

        return outcome_fields(self, record)

    def _judge(self) -> None:
        ws = self.store.state.workspace
        if ws.genesis is None or ws.workspace_id != self.entry.workspace_id:
            self._unavailable("the chain is broken at the genesis")
        elif ws.frozen or any(not item.acknowledged for item in ws.invalid):
            self.state, self.cause = "degraded", "an unacknowledged invalid event"
        elif self.journal_failed:
            self.state, self.cause = "degraded", self.journal_failed
        else:
            self.state, self.cause = "ok", ""

    def _snapshot(self) -> None:
        from orch.store import StoreError

        if self.store is None:
            self.auth = None
            return
        try:
            self.auth = build_auth(self.store, self.entry.root)
        except (StoreError, OSError, ValueError) as exc:
            self._unavailable(f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
            self.auth = None

    def refresh(self) -> None:
        """Re-read the store, judge the state and rebuild the authentication snapshot (worker thread)."""
        from orch.store import StoreError

        if self.store is None:
            return
        try:
            self.store.refresh()
        except StoreError as exc:
            self._unavailable(f"{exc.code}: {exc}")
            self.auth = None
            return
        self._judge()
        self._snapshot()

    def person_valid(self, person: str) -> bool:
        self.refresh()
        return self.auth is not None and self.auth.person_ok(person, self.clock())

    def verify_grant(self, grant: str | None) -> Verified:
        """``auth.grant`` checked against the freshly refreshed store (worker thread): §0.5, F4."""
        self.refresh()
        if grant is None:
            return Verified("absent")
        if self.auth is None:
            return Verified("malformed" if self._malformed(grant) else "unverified")
        return self.auth.verify(grant, self.clock())

    @staticmethod
    def _malformed(grant: str) -> bool:
        from orch.identity import Refused, parse_grant

        try:
            parse_grant(grant)
        except Refused:
            return True
        return False

    def capabilities(self) -> Any:
        """The handlers' :class:`~orch.ops.caps.Capabilities` (host mode), built once (worker thread)."""
        if self.caps is None:
            from orch.ops.caps import Capabilities

            # The workspace root only: the host's state directory (keys, pins, outcome records) is never a handler's
            # file root, and no ORCH_STATE_DIR or ORCH_WORKSPACE reaches a handler (the store is injected).
            self.caps = Capabilities.host((self.entry.root,), env={}, clock=self.clock)
        return self.caps

    def close(self) -> None:
        def shut() -> None:
            rec = getattr(self, "session_records", None)
            if rec is not None:
                rec.flush()  # the throttled session records (host/sessions.py)
            if self.store is not None:
                self.store.close()
                self.store = None
            if self.caps is not None:
                self.caps.close()
                self.caps = None

        try:
            self.submit(shut).result(timeout=30)
        except RuntimeError:
            pass  # already shut down
        self.executor.shutdown(wait=True)


@dataclass
class Call:
    service: WorkspaceService
    session: Session
    peer: Peer
    frame: dict[str, Any]
    column: Column
    viewer: str | None
    #: the verified grant (column G), else ``None``: an unverified grant is no identity
    verified: Verified = Verified("absent")
    #: the principal of this request (§0.5): ``g:<grant id>@<uid>`` for a verified grant, else ``uid:<n>``
    principal: str = ""
    #: for a backend with ``state_checks``: computes the authoritative column on the worker thread, inside the op's
    #: own job (so ``opts.timeout_ms`` covers it, and one connection's writes stay in one FIFO); the backend calls
    #: :meth:`authorize` first
    columns: Callable[[], tuple[Column, str | None, Verified]] | None = None
    kind: OpKind | None = None
    #: the transport's handle of this request (``sub.open``: no ``evt`` of the subscription before its ``res``)
    request: frames.Request | None = None

    def authorize(self) -> None:
        """Recompute the column from the refreshed store and refuse outside it (§0.6 steps 5 and 6, F8)."""
        if self.columns is None:
            return
        self.column, self.viewer, self.verified = self.columns()
        self.principal = principal(self.peer.uid, self.verified)
        code = refusal(self.column, self.kind) if self.kind is not None else None
        if code is not None:
            raise HostError(code, f"{self.frame['op']} is not allowed here")


class OpBackend(Protocol):
    """**Seam (H4a1):** the workspace ops. ``kind`` is (matrix category, phase) or ``None`` for an unknown op."""

    caps: list[str]
    ops: dict[str, int]

    def kind(self, op: str) -> tuple[OpKind, Phase] | None: ...

    async def run(self, call: Call) -> dict[str, Any]: ...


def principal(uid: int, verified: Verified) -> str:
    """§0.5, F4: ``g:<grant id>@<uid>`` only for a verified grant; a grant id alone never makes a principal."""
    return f"g:{verified.grant.id}@{uid}" if verified.grant is not None else f"uid:{uid}"


class StatusBackend:
    """H2a's only workspace op: ``host.status`` (§0.8). Class N gets only ``version`` and ``tier``."""

    caps = ["ops/1"]
    ops = {"host.status": 1}

    def __init__(self, started: Callable[[], float] | None = None) -> None:
        self.started = started

    def kind(self, op: str) -> tuple[OpKind, Phase] | None:
        return ("read", "read") if op == "host.status" else None

    async def run(self, call: Call) -> dict[str, Any]:
        from orch.cli.main import VERSION_LINE

        data: dict[str, Any] = {"version": VERSION_LINE[:64], "tier": call.service.entry.tier}
        if call.column.name in {"P", "G"}:
            data["state"] = call.service.state
            if self.started is not None:
                data["uptime_s"] = int(time.monotonic() - self.started())
        return ok_result(data)


class WorkspaceGate:
    """The transport :class:`~orch.host.transport.Handler` of one workspace socket (§0.6 steps 3 to 7, then work)."""

    def __init__(self, host: Host, workspace_id: str, backend: OpBackend) -> None:
        self.host, self.workspace_id, self.backend = host, workspace_id, backend

    def __call__(self, session: Session, frame: dict[str, Any], request: frames.Request) -> Any:
        op = frame["op"]

        def refuse(error: HostError) -> None:
            session.respond(request, error.response(request.id))

        kind = self.backend.kind(op)
        if kind is None:  # step 3
            refuse(HostError("unknown_command", f"{op} is not served"))
            return None
        served = self.backend.ops.get(op, 1)
        wanted = ((session.hello or {}).get("ops") or {}).get(op, 1)
        if wanted != served:
            refuse(HostError("op.unsupported", f"{op} is served at version {served}, not {wanted}"))
            return None
        if self.host.draining:  # step 4
            refuse(self.host.restarting())
            return None
        if secret_in_request(frame):  # step 5
            refuse(HostError("grant.secret_in_args", "a grant secret goes in auth.grant only; nothing was run"))
            return None
        code = self._early(session.peer, frame, kind[0])  # steps 5 and 6 from the snapshot
        if code is not None:
            refuse(HostError(code, f"{op} is not allowed here"))
            return None
        if not session.admit(request):  # step 7
            return None
        return self._work(session, frame, request, *kind)

    def _early(self, peer: Peer, frame: dict[str, Any], kind: OpKind) -> str | None:
        """The column's refusal from the snapshot, or ``None`` (allowed, or the snapshot cannot say: stale)."""
        service = self.host.services.get(self.workspace_id)
        auth = service.auth if service is not None else None
        if service is None or auth is None or service.closing or not auth.fresh(service.entry.root):
            return None
        now = service.clock()
        verified = auth.verify((frame.get("auth") or {}).get("grant"), now)
        mapped = self.host.registry.mapped_person(peer.uid, self.workspace_id) if peer.kind == "person" else None
        valid = mapped is not None and not self.host.pins.pinned(peer.uid) and auth.person_ok(mapped, now)
        return refusal(resolve(peer.kind, verified.state, valid), kind)

    def _column(self, service: WorkspaceService, peer: Peer, frame: dict[str, Any]) -> tuple[Column, str | None, Any]:
        """The authoritative column, viewer and grant, from the freshly refreshed store (worker thread, F8)."""
        verified = service.verify_grant((frame.get("auth") or {}).get("grant"))
        mapped = self.host.registry.mapped_person(peer.uid, self.workspace_id) if peer.kind == "person" else None
        now = service.clock()
        auth = service.auth
        valid = mapped is not None and not self.host.pins.pinned(peer.uid) and auth is not None
        valid = valid and auth.person_ok(mapped, now)  # type: ignore[union-attr]
        column = resolve(peer.kind, verified.state, valid)
        viewer = verified.person if column.name == "G" else (mapped if column.name == "P" else None)
        return column, viewer, verified if column.name == "G" else Verified(verified.state)

    async def _work(self, session: Session, frame: dict[str, Any], request: frames.Request, kind: Any, phase: Any):
        try:
            service = self.host.services.get(self.workspace_id)
            if service is None or service.state == "unavailable" or service.closing:
                cause = (
                    "not registered"
                    if service is None
                    else ("being unregistered" if service.closing else service.cause)
                )
                raise HostError("workspace.unavailable", f"the workspace cannot be served: {cause}")
            deferred = bool(getattr(self.backend, "state_checks", False))
            if deferred:  # the backend authorizes inside its own worker job (Call.authorize)
                column, viewer, verified = Column("N", "no-person"), None, Verified("absent")
            else:
                column, viewer, verified = await service.call(self._column, service, session.peer, frame)
                code = refusal(column, kind)
                if code is not None:
                    raise HostError(code, f"{frame['op']} is not allowed here")
                if service.state == "unavailable":  # an H2a backend: step 10 here
                    raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
                if phase == "write" and service.state == "degraded":
                    raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
            if phase == "wait" and self.host.draining:
                raise self.host.restarting()  # admitted before the drain, but a wait never parks during one
            if not session.state.start(request, phase):
                return
            call = Call(
                service, session, session.peer, frame, column, viewer, verified, principal(session.peer.uid, verified)
            )
            call.request = request
            if deferred:
                call.columns, call.kind = (lambda: self._column(service, session.peer, frame)), kind
            result = await self.backend.run(call)
            session.respond(request, {"t": "res", "id": request.id, "ok": True, "result": result})
        except HostError as exc:
            session.respond(request, exc.response(request.id))
        finally:
            bus = getattr(self.host, "bus", None)
            if bus is not None:  # evts held back for this answer (a write's own events, a new subscription) may go
                bus.kick()

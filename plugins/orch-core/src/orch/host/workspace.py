"""One registered workspace inside the host (D-P2-1): its worker thread, its store, its state and its socket gate.

The worker thread is the single owner of the mutable :class:`~orch.store.Store`; everything that touches the store
runs on it (:meth:`WorkspaceService.call`). The state is one of

* ``ok``;
* ``degraded``: reads only (an unacknowledged invalid event, a frozen workspace); a write answers
  ``workspace.degraded``;
* ``unavailable``: missing root, a broken or mismatched genesis, unsafe modes; every op answers
  ``workspace.unavailable`` with the cause.

:class:`WorkspaceGate` is the H2a request gate on the workspace socket: drain (``host.restarting``), the effective
column (§0.5), the workspace state and the drain bookkeeping. **Seam (H4a1):** the ops themselves come from an
:class:`OpBackend`; H2a ships only ``host.status``. H4a1 replaces the backend with the registry dispatcher (policy
matrix, quotas, schemas, idempotency) and moves the column check ahead of ``admit`` (§0.6 step 6 before step 7).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from . import frames
from .columns import Column, GrantState, OpKind, refusal, resolve
from .custody import check_tree, check_workspace_files
from .errors import HostError
from .outcomes import OutcomeStore, StoreReconciler
from .registry import Entry
from .transport import Peer, Session

if TYPE_CHECKING:
    from .service import Host

__all__ = ["Call", "OpBackend", "StatusBackend", "WorkspaceGate", "WorkspaceService", "ok_result", "person_ok"]

State = Literal["ok", "degraded", "unavailable"]
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
        if self.state != "unavailable":
            try:
                self.outcomes.open()
                self.outcomes.reconcile_all()  # a crash between an append and its record (§0.7)
                self.outcomes.sweep()
            except (HostError, OSError, ValueError) as exc:
                self._unavailable(f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")

    def _judge(self) -> None:
        ws = self.store.state.workspace
        if ws.genesis is None or ws.workspace_id != self.entry.workspace_id:
            self._unavailable("the chain is broken at the genesis")
        elif ws.frozen or any(not item.acknowledged for item in ws.invalid):
            self.state, self.cause = "degraded", "an unacknowledged invalid event"
        else:
            self.state, self.cause = "ok", ""

    def refresh(self) -> None:
        from orch.store import StoreError

        if self.store is None:
            return
        try:
            self.store.refresh()
        except StoreError as exc:
            self._unavailable(f"{exc.code}: {exc}")
            return
        self._judge()

    def person_valid(self, person: str) -> bool:
        self.refresh()
        return self.store is not None and person_ok(self.store.state.workspace, person, self.clock())

    def verify_grant(self, grant: str | None) -> tuple[GrantState, str | None]:
        """``auth.grant`` → (state, person). **Seam (H4a1):** the full principal derivation (§0.5, F4)."""
        from orch.identity import Refused, parse_grant, secret_matches

        if grant is None:
            return "absent", None
        try:
            token = parse_grant(grant)
        except Refused:
            return "malformed", None
        if self.store is None:
            return "unverified", None
        want = self.store.grant_secret_hash(token.grant_id)
        view = self.store.state.workspace.grants.get(token.grant_id)
        if want is None or view is None or not secret_matches(token.secret, want) or not view.active:
            return "unverified", None
        member = self.store.state.workspace.members.get(view.person)
        if member is None or member.role == "viewer":
            return "unverified", None
        return "verified", view.person

    def close(self) -> None:
        def shut() -> None:
            if self.store is not None:
                self.store.close()
                self.store = None

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


class OpBackend(Protocol):
    """**Seam (H4a1):** the workspace ops. ``kind`` is (matrix category, phase) or ``None`` for an unknown op."""

    caps: list[str]
    ops: dict[str, int]

    def kind(self, op: str) -> tuple[OpKind, Phase] | None: ...

    async def run(self, call: Call) -> dict[str, Any]: ...


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
    """The transport :class:`~orch.host.transport.Handler` of one workspace socket."""

    def __init__(self, host: Host, workspace_id: str, backend: OpBackend) -> None:
        self.host, self.workspace_id, self.backend = host, workspace_id, backend

    def __call__(self, session: Session, frame: dict[str, Any], request: frames.Request) -> Any:
        kind = self.backend.kind(frame["op"])
        if kind is None:
            session.respond(request, HostError("unknown_command", f"{frame['op']} is not served").response(request.id))
            return None
        if self.host.draining:
            session.respond(request, self.host.restarting().response(request.id))
            return None
        # TODO(H4a1): the column check (§0.6 step 6) belongs here, before admit (step 7); it runs in _work today.
        if not session.admit(request):
            return None
        return self._work(session, frame, request, *kind)

    def _column(self, service: WorkspaceService, peer: Peer, frame: dict[str, Any]) -> tuple[Column, str | None]:
        grant_state, grant_person = service.verify_grant((frame.get("auth") or {}).get("grant"))
        mapped = self.host.registry.mapped_person(peer.uid, self.workspace_id) if peer.kind == "person" else None
        valid = mapped is not None and not self.host.pins.pinned(peer.uid) and service.person_valid(mapped)
        column = resolve(peer.kind, grant_state, valid)
        viewer = grant_person if column.name == "G" else (mapped if column.name == "P" else None)
        return column, viewer

    async def _work(self, session: Session, frame: dict[str, Any], request: frames.Request, kind: Any, phase: Any):
        try:
            service = self.host.services.get(self.workspace_id)
            if service is None or service.state == "unavailable":
                cause = service.cause if service is not None else "not registered"
                raise HostError("workspace.unavailable", f"the workspace cannot be served: {cause}")
            column, viewer = await service.call(self._column, service, session.peer, frame)
            code = refusal(column, kind)
            if code is not None:
                raise HostError(code, f"{frame['op']} is not allowed here")
            if service.state == "unavailable":
                raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
            if phase == "write" and service.state == "degraded":
                raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
            if phase == "wait" and self.host.draining:
                raise self.host.restarting()  # admitted before the drain, but a wait never parks during one
            if not session.state.start(request, phase):
                return
            result = await self.backend.run(Call(service, session, session.peer, frame, column, viewer))
            session.respond(request, {"t": "res", "id": request.id, "ok": True, "result": result})
        except HostError as exc:
            session.respond(request, exc.response(request.id))

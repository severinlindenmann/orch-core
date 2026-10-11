"""The host process (D-P2-1..D-P2-4): one host per host UID serves every registered workspace.

Start-up, in order:

1. the state directory passes the D-P2-17 checks and ``<state>/host`` is private (0700);
2. ``<state>/host/host.lock`` is taken exclusively: a second host on the same state directory gets ``host.busy``;
3. the registry is loaded, and every ``started`` control intent is finished or rolled back (§0.7);
4. stale ``wsk_create`` keys are swept; the control socket and one socket per registered workspace start.

Drain (``host.restart``, D-P2-4): new requests answer ``host.restarting`` with ``retry_after_ms``; writes in flight
finish (no deadline inside a commit); parked ``wait``s answer ``host.restarting``; subscriptions end with
``end: "host.restarting"`` (**seam, H6a**: :attr:`Host.subscriptions_end`); then the listeners close and
:meth:`Host.run` returns, so the service manager starts the new release.

**Seams.** The workspace ops come from ``backend`` (H4a1, default :class:`~orch.host.workspace.StatusBackend`);
the worker socket and its PID check are H10; OS users, groups and the root-owned run directory are H3c (in dev and
tier A the host's primary group stands in for the missing ``orch-<ws>`` group).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import grp
import hashlib
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from orch import canon

from . import frames
from .control import CONTROL_OPS, ControlGate, ControlOps
from .custody import check_tree, ensure_private_dir
from .errors import HostError
from .intents import IntentStore
from .jobs import CompletionStore
from .listener import Listener, default_run_directory
from .ownership import LOCK_WAIT_SECONDS
from .pins import AgentPins
from .registry import Entry, Registry, host_dir
from .transport import WRITE_TIMEOUT_SECONDS, HostLimits, Server
from .workspace import OpBackend, StatusBackend, WorkspaceGate, WorkspaceService

__all__ = ["RETRY_AFTER_MS", "Host"]

RETRY_AFTER_MS = 1000
HOST_NAME = "orch-host"


def _digest(ops: dict[str, int]) -> str:
    return "sha256:" + hashlib.sha256(canon.cj_checked(dict(sorted(ops.items())))).hexdigest()


class Host:
    def __init__(
        self,
        state_dir: Path,
        run_dir: Path | None = None,
        *,
        host_uid: int | None = None,
        backend: Callable[[], OpBackend] | None = None,
        pins: AgentPins | None = None,
        clock: Callable[[], float] = time.time,
        lock_wait: float = LOCK_WAIT_SECONDS,
        limits: HostLimits | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.host_uid = os.getuid() if host_uid is None else host_uid
        self.run_dir = Path(run_dir) if run_dir is not None else default_run_directory(self.host_uid)
        self.backend_factory = backend or (lambda: StatusBackend(lambda: self.started_at))
        self.pins = pins or AgentPins()
        self.clock = clock
        self.lock_wait = lock_wait
        self.limits = limits or HostLimits()
        self.registry = Registry(self.state_dir, self.host_uid, person_check=self._person_check, pins=self.pins)
        self.intents = IntentStore(self.state_dir, self.host_uid, clock=clock)
        self.jobs = CompletionStore(self.state_dir, self.host_uid, clock=clock)
        self.control = ControlOps(self)
        self.services: dict[str, WorkspaceService] = {}
        self.servers: dict[str, tuple[Server, asyncio.Task[None]]] = {}
        self.draining = False
        self.started_at = time.monotonic()
        self.subscriptions_end: Callable[[str], None] = lambda reason: None  # H6a ends subscriptions here
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock_fd: int | None = None
        self._stopped: asyncio.Event | None = None
        self._drain: asyncio.Task[None] | None = None
        self._bootstrapping: set[str] = set()
        self._guard = threading.Lock()
        self.recovered: list[tuple[str, str]] = []

    # -- identity and sockets
    def new_workspace_id(self) -> str:
        return secrets.token_hex(16)

    def socket_path(self, workspace_id: str) -> Path:
        return self.run_dir / workspace_id / "host.sock"

    @property
    def control_path(self) -> Path:
        return self.run_dir / "control" / "host.sock"

    def restarting(self) -> HostError:
        return HostError("host.restarting", "the host is restarting", retry_after_ms=RETRY_AFTER_MS)

    def _limits(self) -> dict[str, int]:
        return {
            "frame": frames.MAX_FRAME,
            "inflight": frames.MAX_INFLIGHT,
            "timeout_ms": 30_000,
            "blob_bytes": 0,
            "inline_bytes": 524_288,
        }

    def _tier(self) -> str:
        return "B" if any(e.tier == "B" for e in self.registry.entries()) else "A"

    # -- the person mapping check (F8), on the workspace's worker thread
    def _person_check(self, workspace_id: str, person: str) -> bool:
        # TODO(H4a1): this blocks the event loop (up to 10 s) when the transport classifies a connection; move
        # the per-connection check to an executor or a cache refreshed by the workspace worker.
        service = self.services.get(workspace_id)
        if service is None or service.store is None:
            return False
        try:
            return bool(service.submit(service.person_valid, person).result(timeout=10))
        except Exception:  # noqa: BLE001 - an unanswerable check fails closed (no-person)
            return False

    # -- start and stop
    def _take_host_lock(self) -> None:
        path = host_dir(self.state_dir) / "host.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                other = os.pread(fd, 32, 0).decode("ascii", "replace").strip() or "unknown"
            finally:
                os.close(fd)
            raise HostError("host.busy", f"another host serves {self.state_dir} (PID {other})") from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        self._lock_fd = fd

    def _prepare(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        check_tree(self.state_dir, self.host_uid)
        ensure_private_dir(host_dir(self.state_dir), self.host_uid)
        ensure_private_dir(self.state_dir / "hosts", self.host_uid)
        self._take_host_lock()
        try:
            self.registry.load()
            self.intents.open()
            self.jobs.open()
            self.recovered = self.control.recover()
            self.control.sweep_keys()
            self.intents.sweep()
            self.jobs.sweep()
        except BaseException:
            self._release_host_lock()
            raise

    def _release_host_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stopped = asyncio.Event()
        await asyncio.to_thread(self._prepare)
        try:
            listener = await asyncio.to_thread(
                Listener, self.run_dir, "control", host_uid=self.host_uid, gid=os.getgid(), kind="control"
            )
        except BlockingIOError:
            self._release_host_lock()
            raise HostError("host.busy", f"another host holds {self.run_dir}/control") from None
        except BaseException:
            self._release_host_lock()
            raise
        welcome = {
            "host": HOST_NAME,
            "workspace": None,
            "tier": self._tier(),
            "caps": ["control/1", "outcome/1"],
            "ops": dict(CONTROL_OPS),
            "registry": _digest(CONTROL_OPS),
            "limits": self._limits(),
        }
        server = Server(listener, self.registry, self.limits, ControlGate(self), welcome)
        self.servers["control"] = (server, asyncio.create_task(server.serve()))
        for entry in self.registry.entries():
            await self.serve(entry)

    def _gid(self, entry: Entry) -> int:
        try:
            return grp.getgrnam(entry.group).gr_gid
        except KeyError:
            if entry.tier == "A":
                # Dev and tier A: no agent UIDs and no orch-<ws> group yet (H3c creates it). Known limit: the
                # primary group (staff on macOS) can traverse the 0750 socket directory, so any local user can
                # take accept and handshake slots; unmapped UIDs are still refused before a byte is read (§7).
                return os.getgid()
            raise HostError("workspace.unavailable", f"group {entry.group} is missing; run the setup helper") from None

    async def serve(self, entry: Entry) -> None:
        """Open the workspace on its worker thread and start its socket. A failure leaves it ``unavailable``."""
        service = WorkspaceService(entry, self.state_dir, self.host_uid, clock=self.clock)
        await service.call(service.open)
        self.services[entry.workspace_id] = service  # an unavailable workspace is still bound: ops name the cause
        try:
            gid = self._gid(entry)
            listener = await asyncio.to_thread(
                Listener, self.run_dir, entry.workspace_id, host_uid=self.host_uid, gid=gid
            )
        except (HostError, OSError, ValueError) as exc:
            service.state, service.cause = "unavailable", f"socket: {exc}"[:300]  # and no socket at all
            return
        backend = self.backend_factory()
        welcome = {
            "host": HOST_NAME,
            "workspace": {"id": entry.workspace_id, "prefix": entry.prefix[:10] or "?"},
            "tier": entry.tier,
            "caps": list(backend.caps),
            "ops": dict(backend.ops),
            "registry": _digest(backend.ops),
            "limits": self._limits(),
        }
        server = Server(listener, self.registry, self.limits, WorkspaceGate(self, entry.workspace_id, backend), welcome)
        self.servers[entry.workspace_id] = (server, asyncio.create_task(server.serve()))

    async def unserve(self, workspace_id: str) -> None:
        pair = self.servers.pop(workspace_id, None)
        if pair is not None:
            pair[1].cancel()
            await asyncio.gather(pair[1], return_exceptions=True)
        service = self.services.pop(workspace_id, None)
        if service is not None:
            await asyncio.to_thread(service.close)

    def _on_loop(self, coro: Any) -> Any:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def serve_from_thread(self, entry: Entry) -> None:
        if self.servers:  # during start-up recovery nothing serves yet: start() serves every entry afterwards
            self._on_loop(self.serve(entry))

    def unserve_from_thread(self, workspace_id: str) -> None:
        self._on_loop(self.unserve(workspace_id))

    @contextlib.contextmanager
    def bootstrap_guard(self, workspace_id: str) -> Iterator[None]:
        with self._guard:
            if workspace_id in self._bootstrapping:
                raise HostError("host.busy", f"workspace {workspace_id} is being bootstrapped")
            self._bootstrapping.add(workspace_id)
        try:
            yield
        finally:
            with self._guard:
                self._bootstrapping.discard(workspace_id)

    def bootstrapping(self, workspace_id: str) -> bool:
        with self._guard:
            return workspace_id in self._bootstrapping

    # -- drain
    def begin_drain(self) -> None:
        if self._drain is None:
            self.draining = True
            self._drain = asyncio.get_running_loop().create_task(self.drain())

    async def drain(self) -> None:
        """Answer parked waits with ``host.restarting``, let every other request in flight finish, then stop."""
        self.draining = True
        self.subscriptions_end("host.restarting")
        current = asyncio.current_task()
        while True:
            pending = []
            for server, _ in list(self.servers.values()):
                for session in list(server.sessions):
                    for request, task in list(session.tasks.items()):
                        if task is current or task.done():
                            continue
                        if request.phase == "wait":
                            session.respond(request, self.restarting().response(request.id))
                            task.cancel()
                        else:
                            pending.append(task)
            if not pending:
                break
            await asyncio.wait(pending)
        await self._flush()
        assert self._stopped is not None
        self._stopped.set()

    async def _flush(self) -> None:
        """Wait until every queued answer has been written, bounded by the transport's write deadline."""
        deadline = time.monotonic() + WRITE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            sessions = [s for server, _ in self.servers.values() for s in server.sessions if not s.closed]
            if all(not s.answering and s.queue.empty() and not s.writing for s in sessions):
                return
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        for name in list(self.servers):
            pair = self.servers.pop(name)
            pair[1].cancel()
            await asyncio.gather(pair[1], return_exceptions=True)
        for workspace_id in list(self.services):
            service = self.services.pop(workspace_id)
            await asyncio.to_thread(service.close)
        self._release_host_lock()

    async def wait_stopped(self) -> None:
        assert self._stopped is not None
        await self._stopped.wait()

    async def run(self) -> None:
        """Serve until a drain completes; the service manager restarts the process."""
        await self.start()
        try:
            await self.wait_stopped()
        finally:
            await self.close()

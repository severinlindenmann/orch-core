"""The host process (D-P2-1..D-P2-4): one host per host UID serves every registered workspace.

Start-up, in order:

1. the state directory passes the D-P2-17 checks and ``<state>/host`` is private (0700);
2. ``<state>/host/host.lock`` is taken exclusively: a second host on the same state directory gets ``host.busy``;
3. the registry is loaded, and every ``started`` control intent is finished or rolled back (§0.7);
4. stale ``wsk_create`` keys are swept; the control socket and one socket per registered workspace start.

Drain (``host.restart``, D-P2-4): new requests answer ``host.restarting`` with ``retry_after_ms``; writes in flight
finish (no deadline inside a commit); parked ``wait``s answer ``host.restarting``; subscriptions end with
``end: "host.restarting"`` (:attr:`Host.subscriptions_end`, the bus of H6a); then the listeners close and
:meth:`Host.run` returns, so the service manager starts the new release. ``orch host enable`` waits twice
START_WAIT for the drain, then forces the restart through the service manager (``kickstart -k`` / ``systemctl
restart``): crash-equivalent, a request in flight may be interrupted and is recovered at start.

**Live updates (H6a).** Each workspace's worker keeps its subscription journal (:mod:`orch.host.journal`) and hands
new entries to the host's :class:`~orch.host.bus.Bus` on the event loop. Every :data:`JOURNAL_CHECK_SECONDS` the host
also looks for foreign appends (F11) on every workspace's worker.

**Seams.** The workspace ops come from ``backend`` (default :class:`~orch.host.dispatch.RegistryBackend`, H4a1);
the worker socket and its PID check are H10; OS users, groups and the root-owned run directory are H3c (in dev and
tier A the host's primary group stands in for the missing ``orch-<ws>`` group).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import grp
import hashlib
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from orch import canon
from orch import layout as layout_mod
from orch.layout import LayoutError

from . import frames
from ._unix import UnsafePath
from .blobs import BLOB_MAX, Quota
from .bus import Bus
from .control import CONTROL_OPS, ControlGate, ControlOps
from .custody import check_tree, ensure_private_dir, root_key
from .errors import HostError
from .intents import IntentStore
from .jobs import CompletionStore
from .journal import Cursors, cursor_key
from .listener import Listener, default_run_directory
from .ownership import LOCK_WAIT_SECONDS
from .pins import AgentPins, AgentShare
from .registry import Entry, Registry, host_dir
from .transport import WRITE_TIMEOUT_SECONDS, HostLimits, Server
from .workspace import OpBackend, WorkspaceGate, WorkspaceService


def _release_id() -> str:
    """The digest of this host's code (B5b): the CLI warns when it differs from its own."""
    from .release import code_digest

    try:
        return code_digest()
    except OSError:
        return "unknown"


LOG = logging.getLogger(__name__)
#: how often retention runs in a host that stays up (§0.7)
SWEEP_SECONDS = 3600.0
#: how often every workspace's logs are compared with its journal (F11, D-P2-12)
JOURNAL_CHECK_SECONDS = 60.0

__all__ = ["RETRY_AFTER_MS", "Host"]

RETRY_AFTER_MS = 1000
HOST_NAME = "orch-host"


def _digest(ops: dict[str, int]) -> str:
    return "sha256:" + hashlib.sha256(canon.cj_checked(dict(sorted(ops.items())))).hexdigest()


#: ``<root>/.state/host-served``: a host serves this workspace (written when it starts serving, removed at
#: unregister). The direct-mode records tool refuses while it exists, whatever state directory it was given (H11e,
#: Opus #433 finding 3). A host that stops without unregistering leaves it: the workspace is still registered.
SERVED_MARKER = "host-served"


def _state_fd(project: str | os.PathLike[str]) -> int | None:
    return layout_mod.open_state_fd(project)


def is_served(project: str | os.PathLike[str]) -> bool:
    """A host serves the workspace of ``project``: its marker is under the store's ``.state``. Fails closed: absent only
    when shown absent in both candidate places (``orch.layout.state_marker_present``); a layout that cannot be placed
    counts as served, so a direct-mode tool never opens a store it cannot place."""
    return layout_mod.state_marker_present(project, SERVED_MARKER)


def served_marker(root: str, on: bool, state_dir: Path) -> None:
    """Write or remove the marker beneath the workspace's ``.state`` (no symlink followed; failures are ignored: the
    marker is a second fence next to the registry, never a reason to stop serving)."""
    import json as _json

    del state_dir  # kept in the signature for the callers; never written into the workspace
    try:
        dfd = _state_fd(root)
    except LayoutError:
        return
    if dfd is None:
        return
    try:
        if on:
            data = _json.dumps({"v": 1}).encode()  # presence is the signal; no path or UID in a workspace file
            tmp = f".{SERVED_MARKER}.{secrets.token_hex(8)}"
            excl = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(tmp, excl, 0o600, dir_fd=dfd)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            try:
                os.replace(tmp, SERVED_MARKER, src_dir_fd=dfd, dst_dir_fd=dfd)
            except OSError:
                os.unlink(tmp, dir_fd=dfd)
                raise
        else:
            os.unlink(SERVED_MARKER, dir_fd=dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


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
        sweep_interval: float = SWEEP_SECONDS,
        journal_check_interval: float = JOURNAL_CHECK_SECONDS,
        runner_identity: Callable[[Any], bool] | None = None,
        dev_runner: bool = False,
        dev: Any = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.host_uid = os.getuid() if host_uid is None else host_uid
        self.run_dir = Path(run_dir) if run_dir is not None else default_run_directory(self.host_uid)
        if dev_runner:  # checked before anything is opened or created
            from orch.runner.devmode import is_service_run_dir

            if is_service_run_dir(self.run_dir):
                raise HostError("invalid.input", f"--dev-runner is for a dev run directory only, never {self.run_dir}")
        #: §11A.17: the passed start-up proof of ``--dev-sim-convenience`` (:class:`orch.host.devmode.DevHost`), fixed
        #: for the life of the process; ``None`` on every normal host
        self.dev = dev
        #: V3-D38: the workspace id a pending dev host (:attr:`dev` pending) admitted as its one genesis in this process
        self.pending_claim: str | None = None
        self.pending_lock = threading.Lock()
        #: §11A.17.3 D3: registered workspaces a normal host skipped at start (an ``SBX`` genesis), as alarms
        self.dev_alarms: list[dict[str, Any]] = []
        self._dev_logger: asyncio.Task[None] | None = None
        self.backend_factory = backend or self._registry_backend
        self.pins = pins or AgentPins()
        self.clock = clock
        self.lock_wait = lock_wait
        self.sweep_interval = sweep_interval
        self.journal_check_interval = journal_check_interval
        self._sweeper: asyncio.Task[None] | None = None
        self._checker: asyncio.Task[None] | None = None
        self.limits = limits or HostLimits()
        self.registry = Registry(self.state_dir, self.host_uid, person_check=self._person_check, pins=self.pins)
        self.intents = IntentStore(self.state_dir, self.host_uid, clock=clock)
        self.jobs = CompletionStore(self.state_dir, self.host_uid, clock=clock)
        self.control = ControlOps(self)
        self.services: dict[str, WorkspaceService] = {}
        self.servers: dict[str, tuple[Server, asyncio.Task[None]]] = {}
        self._serving: set[str] = set()  # workspace ids with a serve() begun (one server and one lock per workspace)
        self.draining = False
        self.started_at = time.monotonic()
        self.bus = Bus(self)
        #: cursor authentication (§0.9): its key is read from the state directory at start (cursors survive a restart)
        self.cursors: Cursors = Cursors(secrets.token_bytes(32))
        #: the blob quotas every workspace shares (§0.10, F9)
        self.blob_quota = Quota(clock=self.clock)
        #: the git reader children every workspace shares: at most four at once, one per principal (F10, H3b1)
        from orch.gitread.client import ReaderPool

        self.git_pool = ReaderPool()
        self.subscriptions_end: Callable[[str], None] = self.bus.end_all
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock_fd: int | None = None
        self._stopped: asyncio.Event | None = None
        self._drain: asyncio.Task[None] | None = None
        self._bootstrapping: set[str] = set()
        self._bootstrapping_roots: set[tuple[int, int] | str] = set()
        self._guard = threading.Lock()
        self.recovered: list[tuple[str, str]] = []
        #: H11a: the runner connection, its command stream and the run records (``<run>/runner/host.sock``)
        from .runners import RunnerHub
        from .runners import runner_identity as default_identity

        self.runners = RunnerHub(self)
        self.runner_identity = runner_identity or default_identity
        #: a caller-supplied identity check (tests, dev tools) is never replaced by the tier B system check in
        #: :meth:`_find_share`; production passes none
        self._custom_identity = runner_identity is not None
        #: tier B (spec 13.3), found at start from the root-owned pin: the agent user the run directory is shared with
        self.share: AgentShare | None = None
        #: why the pins could not be trusted (``None``: they could). Then nothing is shared with any agent: no runner
        #: socket, workspaces unavailable. A host whose run directory is the shared one fails to start (tier A modes)
        self.share_error: str | None = None
        #: dev and tests only (``orch host run --foreground --dev-runner``): also admit a runner of this UID that
        #: ``orch runner run --foreground`` announced in this run directory (:mod:`orch.runner.devmode`)
        self.dev_runner = dev_runner
        if dev_runner:
            from orch.runner import devmode

            service_identity, run, uid = self.runner_identity, self.run_dir, self.host_uid

            def dev_identity(credentials: Any) -> bool:
                return bool(service_identity(credentials)) or devmode.verify(run, uid, credentials)

            self.runner_identity = dev_identity
            LOG.warning(
                "DEV RUNNER MODE: this host also admits a foreground runner of UID %s announced in %s "
                "(dev and tests only; never use it for a real setup)",
                uid,
                devmode.path_for(run, uid),
            )

    # -- identity and sockets
    def new_workspace_id(self) -> str:
        return secrets.token_hex(16)

    def socket_path(self, workspace_id: str) -> Path:
        return self.run_dir / workspace_id / "host.sock"

    @property
    def control_path(self) -> Path:
        return self.run_dir / "control" / "host.sock"

    @property
    def term_base(self) -> Path:
        """Where the runner's terminal directory is: the runner's own run directory in tier B (spec 13.3), else
        ``<run>``. The runner announces its socket in ``runner.hello``; only this path is accepted."""
        return self.run_dir if self.share is None else self.share.run_dir

    @property
    def runner_path(self) -> Path:
        return self.run_dir / "runner" / "host.sock"

    def restarting(self) -> HostError:
        return HostError("host.restarting", "the host is restarting", retry_after_ms=RETRY_AFTER_MS)

    def _limits(self) -> dict[str, int]:
        return {
            "frame": frames.MAX_FRAME,
            "inflight": frames.MAX_INFLIGHT,
            "timeout_ms": 30_000,
            "blob_bytes": BLOB_MAX,
            "inline_bytes": 524_288,
        }

    def _tier(self) -> str:
        return "B" if any(e.tier == "B" for e in self.registry.entries()) else "A"

    def _registry_backend(self) -> OpBackend:
        from .dispatch import RegistryBackend

        return RegistryBackend(lambda: self.started_at)

    # -- the person mapping check (F8)
    def _person_check(self, workspace_id: str, person: str) -> bool:
        """Non-blocking: the workspace's authentication snapshot, rebuilt by its worker thread after every refresh.
        The transport calls this on the event loop when it classifies a connection (``welcome.peer.person``); every
        request then rechecks the mapping on the worker thread against the refreshed store (F8)."""
        service = self.services.get(workspace_id)
        if service is None:
            return False
        auth = service.auth
        if auth is not None and auth.fresh(service.entry.store_dir):
            return auth.person_ok(person, self.clock())
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # a control thread (host.list): ask the worker thread, against the refreshed store
            try:
                return bool(service.submit(service.person_valid, person).result(timeout=10))
            except Exception:  # noqa: BLE001 - an unanswerable check fails closed (no-person)
                return False
        # On the event loop with a stale snapshot: never block. The answer only fills welcome.peer.person; every
        # request rechecks the mapping on the worker thread.
        return auth is not None and auth.person_ok(person, self.clock())

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
            self.cursors = Cursors(cursor_key(host_dir(self.state_dir)))
            self.registry.load()
            self._check_dev_registry()
            self.intents.open()
            self.jobs.open()
            self.runners.load()
            self.recovered = self.control.recover()
            self.control.sweep_keys()
            self.intents.sweep()
            self.jobs.sweep()
        except BaseException:
            self._release_host_lock()
            raise

    def _check_dev_registry(self) -> None:
        """§11A.17.2 check 4 and 17.3 D3: a dev host refuses to start with a non-sandbox workspace registered; a
        normal host skips every ``SBX`` workspace of its registry (an edited registry bypasses ``admit``) and raises
        the alarm ``sandbox_workspace_registered`` for each, in ``host.list`` and ``orch host status``."""
        from .devmode import check_registry

        skipped = check_registry(self.dev, self.state_dir, self.registry.entries(), self.host_uid)
        self.dev_alarms = [{"code": "sandbox_workspace_registered", "workspace_id": wid} for wid in skipped]

    def skipped(self, workspace_id: str) -> bool:
        return any(a["workspace_id"] == workspace_id for a in self.dev_alarms)

    def _release_host_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stopped = asyncio.Event()
        await asyncio.to_thread(self._prepare)
        try:
            await self._find_share()
            listener = await asyncio.to_thread(
                Listener,
                self.run_dir,
                "control",
                host_uid=self.host_uid,
                gid=os.getgid(),
                kind="control",
                share=self.share,
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
            "release": _release_id(),
            "tier": self._tier(),
            "caps": ["control/1", "outcome/1"],
            "ops": dict(CONTROL_OPS),
            "registry": _digest(CONTROL_OPS),
            "limits": self._limits(),
        }
        server = Server(listener, self.registry, self.limits, ControlGate(self), welcome)
        self.servers["control"] = (server, asyncio.create_task(server.serve()))
        await self._serve_runner()
        self.bus.start()
        for entry in self.registry.entries():
            if self.skipped(entry.workspace_id):
                continue  # §11A.17.3 D3: a sandbox workspace on a normal host is never served
            await self.serve(entry)
        self._sweeper = asyncio.create_task(self._sweep_loop())
        self._checker = asyncio.create_task(self._check_loop())
        if self.dev is not None:
            self._dev_logger = asyncio.create_task(self._dev_log_loop())

    async def _find_share(self) -> None:
        """Tier B is read from the root-owned pin, never from a flag. A pin that cannot be trusted shares NOTHING (no
        runner socket, no workspace socket) and the host serves with tier A modes: on a machine whose run directory is
        the shared one (setgid) that fails the directory check, so the start fails closed."""
        try:
            self.share = await asyncio.to_thread(self.pins.share_for, self.host_uid)
        except UnsafePath as exc:
            self.share, self.share_error = None, str(exc)[:300]
            LOG.warning("no socket is shared with an agent user: %s", exc)
        if self.share is not None and not self._custom_identity:
            from .runners import system_runner_identity

            share, pins = self.share, self.pins
            self.runner_identity = lambda credentials: system_runner_identity(share, pins, credentials)
            LOG.info("tier B: %s may use the runner socket (unit %s)", share.user, share.unit)

    async def _serve_runner(self) -> None:
        """The runner socket (H11a). A failure leaves the host without a runner (``runner.unavailable``), never down."""
        from .runners import RUNNER_CAPS, RUNNER_OPS

        if self.share_error is not None:
            return
        try:
            listener = await asyncio.to_thread(
                Listener,
                self.run_dir,
                "runner",
                host_uid=self.host_uid,
                gid=os.getgid(),
                kind="runner",
                share=self.share,
            )
        except (OSError, ValueError) as exc:
            LOG.warning("the runner socket is not served: %s", exc)
            return
        welcome = {
            "host": HOST_NAME,
            "workspace": None,
            "tier": self._tier(),
            "caps": list(RUNNER_CAPS),
            "ops": dict(RUNNER_OPS),
            "registry": _digest(RUNNER_OPS),
            "limits": self._limits(),
        }
        server = Server(
            listener, self.registry, self.limits, self.runners.gate, welcome, runner_identity=self.runner_identity
        )
        self.servers["runner"] = (server, asyncio.create_task(server.serve()))

    def _gid(self, entry: Entry) -> int:
        if self.share is not None:  # tier B: one agent group for every shared socket (spec 13.3), not orch-<ws>
            return self.share.gid
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
        wid = entry.workspace_id
        if wid in self._serving:  # a register that landed during start-up is also in start()'s walk: serve once
            return
        self._serving.add(wid)
        try:
            await self._serve(entry)
        except BaseException:
            self._serving.discard(wid)
            raise

    async def _serve(self, entry: Entry) -> None:
        service = WorkspaceService(entry, self.state_dir, self.host_uid, clock=self.clock)
        service.bus, service.publish = self.bus, self.bus.publisher(entry.workspace_id)
        service.cursors = self.cursors
        service.blobs.quota = self.blob_quota  # §0.10: per UID across workspaces, and per host
        service.git_pool = self.git_pool
        service.runners = self.runners  # H11a: run.list / run.sub
        service.dev = self.dev  # §11A.17: the dev gate reads it; None on a normal host
        backend = self.backend_factory()
        await service.call(service.open)
        if self.dev is not None:
            from .devmode import dev_gate

            service.dev_ok = await service.call(lambda: dev_gate(service) is not None)
        self.services[entry.workspace_id] = service  # an unavailable workspace is still bound: ops name the cause
        await asyncio.to_thread(served_marker, entry.root, True, self.state_dir)
        try:
            if self.share_error is not None:
                raise HostError("workspace.unavailable", self.share_error)
            gid = self._gid(entry)
            listener = await asyncio.to_thread(
                Listener, self.run_dir, entry.workspace_id, host_uid=self.host_uid, gid=gid, share=self.share
            )
        except (HostError, OSError, ValueError) as exc:
            service.state, service.cause = "unavailable", f"socket: {exc}"[:300]  # and no socket at all
            return
        welcome = {
            "host": HOST_NAME,
            "workspace": {"id": entry.workspace_id, "prefix": entry.prefix[:10] or "?"},
            "release": _release_id(),
            "tier": entry.tier,
            "caps": list(backend.caps),
            "ops": dict(backend.ops),
            "registry": _digest(backend.ops),
            "limits": self._limits(),
        }
        server = Server(listener, self.registry, self.limits, WorkspaceGate(self, entry.workspace_id, backend), welcome)
        self.servers[entry.workspace_id] = (server, asyncio.create_task(server.serve()))

    async def unserve(self, workspace_id: str) -> None:
        """Stop serving one workspace (``unregister``). Its socket first drains like ``host.restart``: new requests
        answer ``workspace.unavailable``, parked waits end, requests in flight (a write on the worker thread above all)
        finish and their answers are written; only then are the connections closed."""
        service = self.services.get(workspace_id)
        if service is not None:
            service.closing = True
        self.bus.end_all("closed", workspace_id)
        pair = self.servers.get(workspace_id)
        if pair is not None:
            gone = HostError("workspace.unavailable", "the workspace was unregistered")
            await self._settle([pair[0]], lambda: gone)
            await self._flush([pair[0]])
        self._serving.discard(workspace_id)
        pair = self.servers.pop(workspace_id, None)
        if pair is not None:
            pair[1].cancel()
            await asyncio.gather(pair[1], return_exceptions=True)
        service = self.services.pop(workspace_id, None)
        if service is not None:
            await asyncio.to_thread(service.close)
            await asyncio.to_thread(served_marker, service.entry.root, False, self.state_dir)

    def _on_loop(self, coro: Any) -> Any:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def serve_from_thread(self, entry: Entry) -> None:
        if self.servers:  # during start-up recovery nothing serves yet: start() serves every entry afterwards
            self._on_loop(self.serve(entry))

    def unserve_from_thread(self, workspace_id: str) -> None:
        self._on_loop(self.unserve(workspace_id))

    @contextlib.contextmanager
    def bootstrap_guard(self, workspace_id: str, root: str | None = None) -> Iterator[None]:
        """One bootstrap per workspace id **and** per root at a time (N5): two bootstraps of one empty root under two
        ids would otherwise both pass the emptiness check, and the loser's clean-up could remove the winner's files.
        The root is keyed by its identity (:func:`~orch.host.custody.root_key`), never by its spelling."""
        root = None if root is None else root_key(root)  # type: ignore[assignment]
        with self._guard:
            if workspace_id in self._bootstrapping:
                raise HostError("host.busy", f"workspace {workspace_id} is being bootstrapped")
            if root is not None and root in self._bootstrapping_roots:
                raise HostError("host.busy", f"{root} is being bootstrapped")
            self._bootstrapping.add(workspace_id)
            if root is not None:
                self._bootstrapping_roots.add(root)
        try:
            yield
        finally:
            with self._guard:
                self._bootstrapping.discard(workspace_id)
                if root is not None:
                    self._bootstrapping_roots.discard(root)

    def bootstrapping(self, workspace_id: str) -> bool:
        with self._guard:
            return workspace_id in self._bootstrapping

    def bootstrapping_root(self, root: str) -> bool:
        key = root_key(root)
        with self._guard:
            return key in self._bootstrapping_roots

    # -- drain
    def begin_drain(self) -> None:
        if self._drain is None:
            self.draining = True
            self._drain = asyncio.get_running_loop().create_task(self.drain())

    async def drain(self) -> None:
        """Answer parked waits with ``host.restarting``, let every other request in flight finish, then stop."""
        self.draining = True
        self.subscriptions_end("host.restarting")
        self.runners.end_all("host.restarting")
        await self._settle(None, self.restarting)
        await self._flush()
        assert self._stopped is not None
        self._stopped.set()

    async def sweep_once(self) -> None:
        """Retention (§0.7): records past 24 h become tombstones, old tombstones and job records go. Every file
        operation runs off the event loop (the workspace worker thread, or a helper thread)."""
        for fn in (self.intents.sweep, self.jobs.sweep):
            try:
                await asyncio.to_thread(fn)
            except Exception:  # noqa: BLE001 - retention must never stop the host
                LOG.exception("retention sweep failed")
        for service in list(self.services.values()):
            if service.state != "unavailable":
                try:
                    await service.call(service.outcomes.sweep)
                except Exception:  # noqa: BLE001
                    LOG.exception("outcome sweep failed")
                if service.requests is not None:
                    try:
                        await service.call(service.requests.sweep)
                    except Exception:  # noqa: BLE001
                        LOG.exception("request sweep failed")
                try:
                    await service.call(service.blobs.sweep)
                except Exception:  # noqa: BLE001
                    LOG.exception("blob sweep failed")

    async def check_journals(self) -> None:
        """F11: compare every workspace's logs with its journal on its worker thread (a foreign append rebuilds the
        journal and ends its subscriptions with ``reset``)."""
        for service in list(self.services.values()):
            if service.state != "unavailable" and not service.closing:
                try:
                    await service.call(lambda s=service: s.sync(check=True, retry=True))
                except Exception:  # noqa: BLE001 - the check must never stop the host
                    LOG.exception("journal check failed")

    async def _check_loop(self) -> None:
        while True:
            await asyncio.sleep(self.journal_check_interval)
            await self.check_journals()

    async def _dev_log_loop(self) -> None:
        """§11A.17.2 "Visible everywhere": the dev host's line at start and at every hour of uptime."""
        from .devmode import LOG as DEV_LOG
        from .devmode import status_line

        while True:
            DEV_LOG.warning("%s", status_line(self.dev))
            await asyncio.sleep(SWEEP_SECONDS)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_interval)
            await self.sweep_once()

    async def _settle(self, servers: list[Server] | None, wait_error: Callable[[], HostError]) -> None:
        """Answer parked waits with ``wait_error()`` and let every other request in flight finish (``servers``:
        those listeners, ``None``: all)."""
        current = asyncio.current_task()
        while True:
            pending = []
            for server in servers if servers is not None else [pair[0] for pair in list(self.servers.values())]:
                for session in list(server.sessions):
                    for request, task in list(session.tasks.items()):
                        if task is current or task.done():
                            continue
                        if request.phase == "wait":
                            session.respond(request, wait_error().response(request.id))
                            task.cancel()
                        else:
                            pending.append(task)
            if not pending:
                return
            await asyncio.wait(pending)

    async def _flush(self, servers: list[Server] | None = None) -> None:
        """Wait until every queued answer has been written, bounded by the transport's write deadline."""
        deadline = time.monotonic() + WRITE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            chosen = servers if servers is not None else [pair[0] for pair in self.servers.values()]
            sessions = [s for server in chosen for s in server.sessions if not s.closed]
            if all(not s.answering and s.queue.empty() and not s.writing for s in sessions):
                return
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        for task in (self._sweeper, self._checker, self._dev_logger):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._sweeper = self._checker = self._dev_logger = None
        await self.bus.stop()
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

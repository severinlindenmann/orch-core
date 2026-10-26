"""Unix session I/O and supervision; share HostLimits across all listeners."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import resource
import socket
from collections import deque
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, Protocol

from . import frames
from ._socket import Credentials as Credentials
from ._socket import close_pidfd, receive, with_pidfd
from ._socket import peer_credentials as peer_credentials
from .client import Pin as Pin
from .client import connect_pinned as connect_pinned
from .client import handshake_pinned as handshake_pinned
from .listener import (
    Listener as Listener,
)
from .listener import (
    check_socket_path as check_socket_path,
)
from .listener import (
    default_run_directory as default_run_directory,
)
from .peer import Peer as Peer

IDLE_SECONDS = 600
RECHECK_SECONDS = 60
WRITE_TIMEOUT_SECONDS = 10
WRITE_QUEUE_BYTES = 4 * 1024 * 1024
WRITE_QUEUE_FRAMES = 64
BYTES_PER_MINUTE = 64 * 1024 * 1024
TRANSIENT_ACCEPT_ERRORS = {errno.EMFILE, errno.ENFILE, errno.ECONNABORTED, errno.EINTR, errno.ENOBUFS, errno.ENOMEM}
LOG = logging.getLogger(__name__)


class PeerRegistry(Protocol):
    """H2a resolves kernel UIDs, including tier A, never client-supplied identity.

    H4a1 must refresh person membership/device/decide on every request (F8).
    worker_uid is global, and must never also acquire a workspace/control role.
    """

    worker_uid: int | None

    def lookup(self, uid: int, workspace_id: str) -> Peer | None: ...


class Bucket:
    def __init__(self, rate: float, burst: int, clock: Callable[[], float] = monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens, self.updated = float(burst), clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


class HostLimits:
    """Host-wide UID quotas. Keep this object across listener and worker restarts.

    Connection attempts default to 20/s with a burst of 40 per kernel UID.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        connection_rate: tuple[float, int] | None = (20, 40),
        max_connections: int = 128,
    ):
        self.clock, self.connection_rate = clock, connection_rate
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        ceiling = 128 if soft == resource.RLIM_INFINITY else max(1, soft // 4)
        if max_connections < 1:
            raise ValueError("Host connection cap must be positive")
        self.max_connections = min(max_connections, ceiling)
        self.connections: dict[int, int] = {}
        self.workers: set[int] = set()
        self.runners: set[int] = set()
        self.agents: dict[int, Bucket] = {}
        self.attempts: dict[int, Bucket] = {}

    def attempt(self, uid: int) -> None:
        if self.connection_rate is not None:
            bucket = self.attempts.setdefault(uid, Bucket(*self.connection_rate, self.clock))
            if not bucket.take():
                raise frames.FrameError("quota.host")

    def acquire(self, peer: Peer, *, count_attempt: bool = True) -> None:
        if count_attempt:
            self.attempt(peer.uid)
        if peer.kind == "worker" and peer.uid in self.workers:
            raise frames.FrameError("peer.denied")
        if peer.kind == "runner" and peer.uid in self.runners:  # one runner connection per runner UID (D-P2-25)
            raise frames.FrameError("peer.denied")
        if self.connections.get(peer.uid, 0) >= 32 or sum(self.connections.values()) >= self.max_connections:
            raise frames.FrameError("quota.host")
        self.connections[peer.uid] = self.connections.get(peer.uid, 0) + 1
        if peer.kind == "worker":
            self.workers.add(peer.uid)
        if peer.kind == "runner":
            self.runners.add(peer.uid)

    def release(self, peer: Peer) -> None:
        count = self.connections[peer.uid] - 1
        if count:
            self.connections[peer.uid] = count
        else:
            del self.connections[peer.uid]
        if peer.kind == "worker":
            self.workers.remove(peer.uid)
        if peer.kind == "runner":
            self.runners.discard(peer.uid)

    def request_allowed(self, peer: Peer) -> bool:
        return peer.kind != "agent" or self.agents.setdefault(peer.uid, Bucket(20, 100, self.clock)).take()


class ByteBudget:
    """A conservative rolling minute, bounded to 61 one-second counters."""

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.samples: deque[tuple[int, int]] = deque()
        self.total = 0

    def add(self, size: int) -> None:
        now = int(self.clock())
        while self.samples and self.samples[0][0] < now - 60:
            self.total -= self.samples.popleft()[1]
        if self.total + size > BYTES_PER_MINUTE:
            raise frames.FrameError("conn.overloaded", "Connection byte limit exceeded")
        if self.samples and self.samples[-1][0] == now:
            size += self.samples.pop()[1]
        self.samples.append((now, size))
        self.total = sum(count for _, count in self.samples)


class Handler(Protocol):
    """Perform dispatch checks and session.admit synchronously, then return work.

    This preserves H1a error precedence and bounds tasks before scheduling them.
    Work calls state.start with its operation kind and session.respond once; the
    transport calls state.stopped when work exits. Write ordering, op timeouts,
    permissions and request-level person revalidation belong to H4a1.
    """

    def __call__(self, session: Session, frame: dict[str, Any], request: frames.Request) -> Awaitable[None] | None: ...


class Session:
    def __init__(
        self,
        sock: socket.socket,
        peer: Peer,
        credentials: Credentials,
        server: Server,
        *,
        accepted_at: float | None = None,
    ):
        self.sock, self.peer, self.credentials, self.server = sock, peer, credentials, server
        self.state = frames.Connection()
        self.decoder = frames.Decoder(clock=server.clock)
        self.subscriptions = 0  # H6 owns subscription lifetimes.
        #: called once the connection is closed (the bus frees its subscriptions here)
        self.on_close: list[Callable[[], Any]] = []
        self.hello: dict[str, Any] | None = None
        self.queue: asyncio.Queue[tuple[bytes, Callable[[], Any] | None]] = asyncio.Queue(WRITE_QUEUE_FRAMES)
        self.queued_bytes = 0  # Includes the frame currently being written.
        self.input_budget, self.output_budget = ByteBudget(server.clock), ByteBudget(server.clock)
        self.accepted = self.last_activity = server.clock() if accepted_at is None else accepted_at
        self.tasks: dict[frames.Request, asyncio.Task[None]] = {}
        self.answering: set[frames.Request] = set()
        self.closed = False
        self.failure: frames.FrameError | None = None
        self.writing = False

    def admit(self, request: frames.Request) -> bool:
        """H4 calls this at precedence step 7, after op, drain and permission checks."""
        if not self.server.limits.request_allowed(self.peer):
            self.respond(
                request,
                {
                    "t": "res",
                    "id": request.id,
                    "ok": False,
                    "exit": 9,
                    "error": {
                        "code": "rate.limited",
                        "message": "Agent request rate exceeded",
                        "retryable": True,
                        "retry_after_ms": 50,
                    },
                },
            )
            return False
        return self.state.admit(request)

    def send(self, frame: dict[str, Any], done: Callable[[], Any] | None = None) -> None:
        if self.closed:
            return
        frames.validate_envelope(frame, sender="host")
        data = frames.encode(frame)
        self.output_budget.add(len(data))
        if self.queue.full() or self.queued_bytes + len(data) > WRITE_QUEUE_BYTES:
            raise frames.FrameError("conn.overloaded", "Slow reader exceeded the write queue")
        self.queue.put_nowait((data, done))
        self.queued_bytes += len(data)

    def respond(self, request: frames.Request, frame: dict[str, Any]) -> None:
        if request.answered or request in self.answering or self.closed:
            return
        if frame.get("t") != "res" or frame.get("id") != request.id:
            raise ValueError("Response does not match its request")
        try:
            self.send(frame, lambda: self._responded(request))
        except frames.FrameError as exc:
            if exc.code != "frame.too_large":
                raise
            response = frames.FrameError("result.too_large", request_id=request.id).response()
            response["error"]["hint"] = "Use a smaller limit; pass next_page as page to fetch subsequent results."
            self.send(response, lambda: self._responded(request))
        self.answering.add(request)

    def _responded(self, request: frames.Request) -> None:
        self.answering.discard(request)
        self.state.responded(request)

    async def _write(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            data, done = await self.queue.get()
            self.writing = True
            try:
                # 3.11 wait_for can swallow cancellation when its send task has
                # just finished, leaving this writer parked on queue.get().
                async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
                    await loop.sock_sendall(self.sock, data)
            except TimeoutError as exc:
                raise frames.FrameError("conn.overloaded", "Slow reader write deadline exceeded") from exc
            self.writing = False
            self.queued_bytes -= len(data)
            self.last_activity = self.server.clock()
            if done is not None:
                done()

    async def _work(self, request: frames.Request, work: Awaitable[None]) -> None:
        try:
            await work
            self._internal(request)  # No-op if work already answered or queued its response.
        except frames.FrameError as exc:
            if exc.fate == "close":
                self.failure = exc
            else:
                self.respond(request, exc.response())
        except asyncio.CancelledError:
            raise
        except Exception:
            self._internal(request)

    def _internal(self, request: frames.Request) -> None:
        self.respond(
            request,
            {
                "t": "res",
                "id": request.id,
                "ok": False,
                "exit": 1,
                "error": {"code": "internal", "message": "Request handler failed", "retryable": False},
            },
        )

    def _receive(self, frame: dict[str, Any]) -> None:
        try:
            result = self.state.receive(frame)
        except frames.FrameError as exc:
            if exc.fate == "close":
                raise
            self.respond(self.state.request(exc.request_id), exc.response())
            return
        if frame["t"] == "hello":
            if self.server.clock() - self.accepted >= frames.HELLO_TIMEOUT_SECONDS:
                raise frames.FrameError("handshake.invalid")
            shared = set(frame["proto"]) & set(self.server.protocols)
            if not shared:
                raise frames.FrameError("host.version_mismatch")
            self.hello = frame
            welcome = self.server.welcome(self.peer, max(shared))
            self.send(welcome, lambda: self.state.welcomed() if self.state.phase == "welcome" else None)
        elif frame["t"] == "cancel":
            if result is not None:
                request = self.state.request(frame["id"])
                self.respond(request, frames.FrameError(result, request_id=request.id).response())
                if request.phase != "write" and request in self.tasks:
                    self.tasks[request].cancel()
        else:
            assert isinstance(result, frames.Request)
            try:
                work = self.server.handler(self, frame, result)
            except frames.FrameError as exc:
                if exc.fate == "close":
                    raise
                self.respond(result, exc.response())
                return
            except Exception:
                self._internal(result)
                return
            if work is not None:
                if result.phase not in {"queued", "read", "wait", "write"}:
                    if hasattr(work, "close"):
                        work.close()
                    self._internal(result)
                    return
                task = asyncio.create_task(self._work(result, work))
                self.tasks[result] = task

                def finished(task: asyncio.Task[None], request: frames.Request = result) -> None:
                    # Cancellation may happen before _work even starts.
                    self.state.stopped(request)
                    self.tasks.pop(request, None)
                    if hasattr(work, "close"):
                        work.close()
                    if not task.cancelled() and task.exception() is not None:
                        exc = task.exception()
                        self.failure = exc if isinstance(exc, frames.FrameError) else frames.FrameError("internal")
                    elif task.cancelled():
                        try:
                            self._internal(request)
                        except frames.FrameError as exc:
                            self.failure = exc

                task.add_done_callback(finished)
            else:
                if result not in self.answering and not result.answered:
                    # A synchronous refusal must answer; it cannot reserve an ID forever.
                    self._internal(result)
                self.state.stopped(result)

    async def _deadlines(self) -> None:
        while True:
            if self.failure is not None:
                raise self.failure
            now = self.server.clock()
            if self.state.phase == "hello" and now - self.accepted >= frames.HELLO_TIMEOUT_SECONDS:
                raise frames.FrameError("handshake.invalid")
            self.decoder.check_deadline()
            if now - self.last_activity >= IDLE_SECONDS and not self.state.inflight and not self.subscriptions:
                return
            await asyncio.sleep(0.1)

    async def _read(self) -> None:
        while True:
            data = await receive(self.sock)
            if not data:
                self.decoder.finish()
                return
            self.last_activity = self.server.clock()
            self.input_budget.add(len(data))
            for frame in self.decoder.feed(data):
                if isinstance(frame, frames.FrameError):
                    raise frame
                self._receive(frame)

    async def run(self) -> None:
        reader, writer = asyncio.create_task(self._read()), asyncio.create_task(self._write())
        timer = asyncio.create_task(self._deadlines())
        error = None
        try:
            done, _ = await asyncio.wait({reader, writer, timer}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except frames.FrameError as exc:
            error = exc
        except Exception:
            error = frames.FrameError("internal")
        finally:
            self.closed = True
            reader.cancel()
            writer.cancel()
            timer.cancel()
            try:
                await asyncio.gather(reader, writer, timer, return_exceptions=True)
                if error is not None:
                    # A timed-out write may have sent a partial frame. Sending a fatal
                    # after it would corrupt framing: close directly in that case.
                    if not self.writing:
                        await _fatal(self.sock, error)
            finally:
                # End the connection even if cancellation interrupts the fatal
                # send, and before waiting for request handlers to finish.
                with contextlib.suppress(OSError):
                    self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
                tasks = list(self.tasks.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for hook in self.on_close:
                    try:
                        hook()
                    except Exception:  # noqa: BLE001 - a hook never keeps a connection from closing
                        LOG.exception("connection close hook failed")


async def _fatal(sock: socket.socket, error: frames.FrameError) -> None:
    with contextlib.suppress(OSError, TimeoutError):
        async with asyncio.timeout(0.2):
            await asyncio.get_running_loop().sock_sendall(sock, frames.encode(error.response()))


class Server:
    """One listener. Cancelling serve() closes its listener and all connections.

    worker_identity must compare the kernel PID AND process start time to the
    service manager's MainPID/start time (H10); absent that hook workers are denied.
    welcome supplies H2a/H4 metadata; transport overwrites identity fields.
    """

    def __init__(
        self,
        listener: Listener,
        registry: PeerRegistry,
        limits: HostLimits,
        handler: Handler,
        welcome: dict[str, Any],
        *,
        protocols: tuple[int, ...] = (1,),
        worker_identity: Callable[[Credentials], bool] | None = None,
        runner_identity: Callable[[Credentials], bool] | None = None,
        clock: Callable[[], float] = monotonic,
    ):
        self.listener, self.registry, self.limits, self.handler = listener, registry, limits, handler
        self.metadata, self.protocols, self.worker_identity, self.clock = welcome, protocols, worker_identity, clock
        #: H11a: the runner socket's main-PID check (the runner unit's service-manager main PID); absent: denied
        self.runner_identity = runner_identity
        self.sessions: set[Session] = set()
        self.tasks: set[asyncio.Task[None]] = set()

    def classify(self, credentials: Credentials, *, runner_ok: bool = False) -> Peer:
        uid = credentials.uid
        worker = uid == self.registry.worker_uid
        kind = self.listener.kind
        if kind == "runner":
            # tier A: the runner runs as the host UID; tier B: as the pinned agent user (``listener.runner_uid``).
            # Only the runner unit's main PID gets the class (D-P2-25)
            if not worker and uid == self.listener.runner_uid and runner_ok:
                return Peer(uid, "runner")
        elif kind == "worker":
            if worker and self.worker_identity is not None and self.worker_identity(credentials):
                return Peer(uid, "worker")
        elif not worker:
            if kind == "control" and uid == self.listener.host_uid:
                return Peer(uid, "control")
            if kind == "workspace":
                peer = self.registry.lookup(uid, self.listener.workspace_id)
                if peer is not None and peer.uid == uid and peer.kind in {"person", "agent"}:
                    return peer
        raise frames.FrameError("peer.denied")

    def welcome(self, peer: Peer, proto: int) -> dict[str, Any]:
        value = self.metadata | {
            "t": "welcome",
            "proto": proto,
            "host_uid": self.listener.host_uid,
            "peer": {"uid": peer.uid, "class": peer.kind, "person": peer.person},
        }
        if self.listener.kind != "workspace":
            value["workspace"] = None
        elif value.get("workspace", {}).get("id") != self.listener.workspace_id:
            raise ValueError("Welcome workspace must match listener")
        return value

    def _session_done(self, session: Session, task: asyncio.Task[None]) -> None:
        # Also runs if the task was cancelled before entering Session.run.
        session.sock.close()
        self.sessions.discard(session)
        self.tasks.discard(task)
        self.limits.release(session.peer)
        if not task.cancelled() and task.exception() is not None:
            asyncio.get_running_loop().call_exception_handler(
                {
                    "message": "Unix transport session failed",
                    "exception": task.exception(),
                }
            )

    async def _accept(self) -> None:
        loop = asyncio.get_running_loop()
        assert self.listener.sock is not None
        while True:
            try:
                sock, _ = await loop.sock_accept(self.listener.sock)
            except OSError as exc:
                if exc.errno not in TRANSIENT_ACCEPT_ERRORS:
                    raise
                LOG.warning("Transient Unix accept failure: %s", exc)
                await asyncio.sleep(0.1)
                continue
            accepted_at = self.clock()
            sock.setblocking(False)
            acquired = None
            handed_off = False
            try:
                credentials = peer_credentials(sock)
                self.limits.attempt(credentials.uid)
                runner_ok = False
                if self.listener.kind == "runner" and self.runner_identity is not None:
                    credentials = with_pidfd(sock, credentials)
                    try:  # the service manager is asked off the event loop; the pidfd stays open until decided
                        runner_ok = bool(await asyncio.to_thread(self.runner_identity, credentials))
                    except Exception:  # noqa: BLE001 - an unanswerable check denies
                        runner_ok = False
                    finally:
                        close_pidfd(credentials)
                peer = self.classify(credentials, runner_ok=runner_ok)
                self.limits.acquire(peer, count_attempt=False)
                acquired = peer
                session = Session(sock, peer, credentials, self, accepted_at=accepted_at)
                self.sessions.add(session)
                task = asyncio.create_task(session.run())
                self.tasks.add(task)
                task.add_done_callback(lambda task, session=session: self._session_done(session, task))
                handed_off = True
            except frames.FrameError as exc:
                await _fatal(sock, exc)
            except OSError:
                pass
            finally:
                if not handed_off:
                    sock.close()
                    if acquired is not None:
                        self.limits.release(acquired)

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(RECHECK_SECONDS)
            try:
                self.listener.recheck()
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.EACCES, errno.EPERM}:
                    raise
                LOG.warning("Unix listener recheck deferred: %s", exc)

    async def serve(self) -> None:
        accept, monitor = asyncio.create_task(self._accept()), asyncio.create_task(self._monitor())
        try:
            done, _ = await asyncio.wait({accept, monitor}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            accept.cancel()
            monitor.cancel()
            await asyncio.gather(accept, monitor, return_exceptions=True)
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
            self.listener.close()

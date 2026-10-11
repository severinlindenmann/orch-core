"""Unix transport for contract v1.3.1; dispatch and durable registries are injected.

Construct/bind listeners before starting threads: Python has no bindat(), so both
platforms use §2's fchdir fallback with a saved cwd fd. serve() uses raw nonblocking
sockets, ensuring kernel credentials are checked before any recv (including hello).
A HostLimits instance must be shared by all listeners in one host.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import os
import re
import socket
import stat
import struct
import sys
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal, Protocol

from . import frames
from ._unix import CheckedDirectory, UnsafePath, check_acl

SocketKind = Literal["workspace", "control", "worker"]
IDLE_SECONDS = 600
RECHECK_SECONDS = 60
WRITE_TIMEOUT_SECONDS = 10
WRITE_QUEUE_BYTES = 4 * 1024 * 1024
WRITE_QUEUE_FRAMES = 64
BYTES_PER_MINUTE = 64 * 1024 * 1024


@dataclass(frozen=True)
class Credentials:
    uid: int
    gid: int
    pid: int


def peer_credentials(sock: socket.socket) -> Credentials:
    if sys.platform == "linux":
        pid, uid, gid = struct.unpack("iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return Credentials(uid, gid, pid)
    if sys.platform == "darwin":
        # struct xucred: unsigned version, uid_t uid, short ngroups, gid_t groups[16].
        raw = sock.getsockopt(0, 1, 76)  # SOL_LOCAL, LOCAL_PEERCRED
        version, uid, groups, gid = struct.unpack_from("@IIh2xI", raw)
        if version != 0 or not 1 <= groups <= 16:
            raise OSError("Invalid LOCAL_PEERCRED")
        pid = struct.unpack("i", sock.getsockopt(0, 2, 4))[0]  # LOCAL_PEERPID
        return Credentials(uid, gid, pid)
    raise OSError("Unix peer credentials are unsupported")


@dataclass(frozen=True)
class Peer:
    uid: int
    kind: Literal["person", "agent", "worker", "control"]
    person: str | None = None


class PeerRegistry(Protocol):
    """H2a resolves kernel UIDs, including tier A, never client-supplied identity.

    H4a1 must refresh person membership/device/decide on every request (F8).
    worker_uid is global, and must never also acquire a workspace/control role.
    """

    worker_uid: int | None

    def lookup(self, uid: int, workspace_id: str) -> Peer | None: ...


@dataclass(frozen=True)
class Pin:
    """Trusted registry entry, NOT values read from workspace config.json.

    Person clients obtain this from their own registry. Agent clients obtain it
    from the root-owned setup-helper pin file; H2a/H3c supply its storage format.
    workspace_id is None for a control/worker endpoint.
    """

    host_uid: int
    workspace_id: str | None
    socket_path: Path

    def check_welcome(self, welcome: dict[str, Any]) -> None:
        frames.validate_envelope(welcome, sender="host")
        workspace = welcome.get("workspace")
        actual = workspace.get("id") if isinstance(workspace, dict) else None
        if welcome.get("t") != "welcome" or welcome.get("host_uid") != self.host_uid or actual != self.workspace_id:
            raise UnsafePath("Server welcome does not match the trusted pin")


def connect_pinned(pin: Pin, *, timeout: float = 2) -> socket.socket:
    """Return a connected socket only after checking UID; sends no bytes.

    Send hello next, then call pin.check_welcome BEFORE sending any request or
    secret. The workspace claim necessarily arrives after hello, never before it.
    """
    check_socket_path(pin.socket_path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(pin.socket_path))
        if peer_credentials(sock).uid != pin.host_uid:
            raise UnsafePath("Server UID does not match the trusted pin")
        return sock
    except BaseException:
        sock.close()
        raise


def handshake_pinned(pin: Pin, hello: dict[str, Any], *, timeout: float = 2) -> tuple[socket.socket, dict[str, Any]]:
    """Verify the complete pin before returning a socket usable for requests.

    Only a validated, credential-free hello is sent before the workspace check.
    The deadline covers the whole welcome, including a trickled length prefix.
    """
    frames.validate_envelope(hello, sender="client")
    if hello["t"] != "hello":
        raise frames.FrameError("handshake.invalid")
    sock = connect_pinned(pin, timeout=timeout)
    try:
        deadline = monotonic() + timeout
        sock.sendall(frames.encode(hello))
        decoder = frames.Decoder()
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Welcome deadline exceeded")
            sock.settimeout(remaining)
            data = sock.recv(65536)
            if not data:
                decoder.finish()
                raise ConnectionError("Server closed before welcome")
            received = decoder.feed(data)
            if received:
                welcome = received[0]
                if isinstance(welcome, frames.FrameError):
                    raise welcome
                if len(received) != 1 or decoder.pending:
                    raise frames.FrameError(message="Unexpected frame before any request")
                pin.check_welcome(welcome)
                if welcome["proto"] not in hello["proto"]:
                    raise frames.FrameError("host.version_mismatch")
                sock.settimeout(timeout)
                return sock, welcome
    except BaseException:
        sock.close()
        raise


def check_socket_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts or len(str(path).encode("utf-8")) > 100:
        raise UnsafePath("Socket path must be absolute and at most 100 UTF-8 bytes")


def default_run_directory(host_uid: int) -> Path:
    if sys.platform == "darwin":
        return Path("/Users/Shared/orch/run") / str(host_uid)
    if sys.platform == "linux":
        return Path("/run/orch") / str(host_uid)
    raise UnsafePath("Unix transport is unsupported on this platform")


class Listener:
    """Own the locked, pinned directory and bound socket until close()."""

    def __init__(self, run: Path, name: str, *, host_uid: int, gid: int, kind: SocketKind = "workspace"):
        if kind not in {"workspace", "control", "worker"}:
            raise ValueError("Unknown socket kind")
        if (kind == "workspace" and not re.fullmatch("[0-9a-f]{32}", name)) or (kind != "workspace" and name != kind):
            raise ValueError("Invalid listener directory name")
        self.path = Path(run) / name / "host.sock"
        check_socket_path(self.path)
        if threading.current_thread() is not threading.main_thread() or threading.active_count() != 1:
            raise RuntimeError("Bind listeners before starting any threads (fchdir fallback)")
        self.host_uid, self.kind = host_uid, kind
        self.workspace_id = name if kind == "workspace" else None
        self.sock: socket.socket | None = None
        self.lock_fd: int | None = None
        self.run: CheckedDirectory | None = None
        self.directory: CheckedDirectory | None = None
        self.identity: tuple[int, int] | None = None
        try:
            self.run = CheckedDirectory(Path(run), host_uid, mode=0o711)
            self.directory = CheckedDirectory(
                self.path.parent, host_uid, mode=0o700 if kind == "control" else 0o750, gid=gid
            )
            self.run.recheck()
            self.lock_fd = os.open(
                "lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600,
                dir_fd=self.directory.fd,
            )
            info = os.fstat(self.lock_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != host_uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise UnsafePath("Unsafe socket lock")
            check_acl(self.lock_fd, {0, host_uid})
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._bind(gid)
        except BaseException:
            self.close()
            raise

    def _bind(self, gid: int) -> None:
        assert self.directory is not None
        saved_cwd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        old_umask = os.umask(0o177)
        try:
            os.fchdir(self.directory.fd)
            try:
                info = os.stat("host.sock", follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.host_uid:
                    raise UnsafePath(f"Planted socket: owner {info.st_uid}")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect("host.sock")
                    except OSError as exc:
                        if exc.errno != errno.ECONNREFUSED:
                            raise UnsafePath("Cannot prove socket is stale") from exc
                    else:
                        raise UnsafePath("Socket is already serving")
                os.unlink("host.sock")
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind("host.sock")
            info = os.stat("host.sock", follow_symlinks=False)
            self.identity = info.st_dev, info.st_ino
            os.chown("host.sock", -1, gid, follow_symlinks=False)
            os.chmod("host.sock", 0o660)
            self.sock.listen(128)
            self.sock.setblocking(False)
        finally:
            os.fchdir(saved_cwd)
            os.close(saved_cwd)
            os.umask(old_umask)
        self.recheck()

    def recheck(self) -> None:
        assert self.run is not None and self.directory is not None
        self.run.recheck()
        self.directory.recheck()
        info = os.stat("host.sock", dir_fd=self.directory.fd, follow_symlinks=False)
        if (
            (info.st_dev, info.st_ino) != self.identity
            or not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != self.host_uid
            or info.st_gid != self.directory.gid
            or stat.S_IMODE(info.st_mode) != 0o660
        ):
            raise UnsafePath("Socket inode, owner, group or mode changed")

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.directory is not None:
            if self.identity is not None:
                with contextlib.suppress(FileNotFoundError):
                    info = os.stat("host.sock", dir_fd=self.directory.fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) == self.identity:
                        os.unlink("host.sock", dir_fd=self.directory.fd)
            self.directory.close()
            self.directory = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.run is not None:
            self.run.close()
            self.run = None


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

    §0 specifies 32 live connections, not an attempt rate. An optional attempt
    bucket is available for deployments, without inventing a normative default.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic, connection_rate: tuple[float, int] | None = None):
        self.clock, self.connection_rate = clock, connection_rate
        self.connections: dict[int, int] = {}
        self.workers: set[int] = set()
        self.agents: dict[int, Bucket] = {}
        self.attempts: dict[int, Bucket] = {}

    def acquire(self, peer: Peer) -> None:
        if peer.kind == "worker" and peer.uid in self.workers:
            raise frames.FrameError("peer.denied")
        if self.connection_rate is not None:
            bucket = self.attempts.setdefault(peer.uid, Bucket(*self.connection_rate, self.clock))
            if not bucket.take():
                raise frames.FrameError("quota.host")
        if self.connections.get(peer.uid, 0) >= 32:
            raise frames.FrameError("quota.host")
        self.connections[peer.uid] = self.connections.get(peer.uid, 0) + 1
        if peer.kind == "worker":
            self.workers.add(peer.uid)

    def release(self, peer: Peer) -> None:
        count = self.connections[peer.uid] - 1
        if count:
            self.connections[peer.uid] = count
        else:
            del self.connections[peer.uid]
        if peer.kind == "worker":
            self.workers.remove(peer.uid)

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
            raise frames.FrameError("quota.host", "Connection byte limit exceeded")
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
            raise frames.FrameError("quota.host", "Slow reader exceeded the write queue")
        self.queue.put_nowait((data, done))
        self.queued_bytes += len(data)

    def respond(self, request: frames.Request, frame: dict[str, Any]) -> None:
        if request.answered or request in self.answering or self.closed:
            return
        if frame.get("t") != "res" or frame.get("id") != request.id:
            raise ValueError("Response does not match its request")
        self.send(frame, lambda: self._responded(request))
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
                await asyncio.wait_for(loop.sock_sendall(self.sock, data), WRITE_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                raise frames.FrameError("quota.host", "Slow reader write deadline exceeded") from exc
            self.writing = False
            self.queued_bytes -= len(data)
            self.last_activity = self.server.clock()
            if done is not None:
                done()

    async def _work(self, request: frames.Request, work: Awaitable[None]) -> None:
        try:
            await work
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
                        self.failure = exc if isinstance(exc, frames.FrameError) else frames.FrameError("quota.host")

                task.add_done_callback(finished)
            else:
                if result not in self.answering and not result.answered:
                    # A synchronous refusal must answer; it cannot reserve an ID forever.
                    self._internal(result)
                self.state.stopped(result)

    async def _read(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            if self.failure is not None:
                raise self.failure
            now = self.server.clock()
            if self.state.phase == "hello" and now - self.accepted >= frames.HELLO_TIMEOUT_SECONDS:
                raise frames.FrameError("handshake.invalid")
            self.decoder.check_deadline()
            if now - self.last_activity >= IDLE_SECONDS and not self.state.inflight and not self.subscriptions:
                return
            try:
                data = await asyncio.wait_for(loop.sock_recv(self.sock, 65536), 0.1)
            except TimeoutError:
                continue
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
        error = None
        try:
            done, _ = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except frames.FrameError as exc:
            error = exc
        except (ConnectionError, OSError):
            pass
        finally:
            self.closed = True
            reader.cancel()
            writer.cancel()
            await asyncio.gather(reader, writer, return_exceptions=True)
            if error is not None:
                # A timed-out write may have sent a partial frame. Sending a fatal
                # after it would corrupt framing: close directly in that case.
                if not self.writing:
                    await _fatal(self.sock, error)
            for task in list(self.tasks.values()):
                task.cancel()
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
            self.sock.close()


async def _fatal(sock: socket.socket, error: frames.FrameError) -> None:
    with contextlib.suppress(OSError, TimeoutError):
        await asyncio.wait_for(asyncio.get_running_loop().sock_sendall(sock, frames.encode(error.response())), 0.2)


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
        clock: Callable[[], float] = monotonic,
    ):
        self.listener, self.registry, self.limits, self.handler = listener, registry, limits, handler
        self.metadata, self.protocols, self.worker_identity, self.clock = welcome, protocols, worker_identity, clock
        self.sessions: set[Session] = set()
        self.tasks: set[asyncio.Task[None]] = set()

    def classify(self, credentials: Credentials) -> Peer:
        uid = credentials.uid
        worker = uid == self.registry.worker_uid
        kind = self.listener.kind
        if kind == "worker":
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
            sock, _ = await loop.sock_accept(self.listener.sock)
            accepted_at = self.clock()
            sock.setblocking(False)
            acquired = None
            handed_off = False
            try:
                credentials = peer_credentials(sock)
                peer = self.classify(credentials)
                self.limits.acquire(peer)
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
            self.listener.recheck()

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

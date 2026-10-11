"""H1b acceptance and transport integration; no dispatcher or registry storage."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.host import client as client_module
from orch.host import frames, transport
from orch.host import listener as listener_module
from orch.host._unix import CheckedDirectory, UnsafePath, _linux_acl, check_acl, check_directory
from orch.host.transport import (
    Bucket,
    ByteBudget,
    Credentials,
    HostLimits,
    Listener,
    Peer,
    Pin,
    Server,
    Session,
    check_socket_path,
    connect_pinned,
    handshake_pinned,
    peer_credentials,
)

WS = "a" * 32
HELLO = {"t": "hello", "proto": [1], "client": "test", "caps": []}
UID, GID = os.getuid(), os.getgid()
META = {
    "host": "test",
    "workspace": {"id": WS, "prefix": "TEST"},
    "tier": "B",
    "caps": [],
    "ops": {},
    "registry": "sha256:" + "0" * 64,
    "limits": {"frame": frames.MAX_FRAME, "inflight": 8, "timeout_ms": 30_000, "blob_bytes": 0, "inline_bytes": 0},
}


class Registry:
    def __init__(self, peers=None, worker_uid=None):
        self.peers = {UID: Peer(UID, "agent")} if peers is None else peers
        self.worker_uid = worker_uid
        self.lookups = []

    def lookup(self, uid, workspace_id):
        self.lookups.append((uid, workspace_id))
        return self.peers.get(uid)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def req(request_id="r"):
    return {"t": "req", "id": request_id, "op": "status", "args": {}}


def result(request_id="r"):
    return {
        "t": "res",
        "id": request_id,
        "ok": True,
        "result": {
            "data": {},
            "key": None,
            "seq": 0,
            "cursor": 0,
            "hints": [],
            "lines": [],
            "duplicate": False,
            "exit": 0,
            "head": None,
        },
    }


def handler(session, frame, request):
    if session.admit(request):

        async def run():
            if session.state.start(request, "read"):
                session.respond(request, result(request.id))

        return run()
    return None


@pytest.fixture
def secure_tree():
    from tests.host.lifecycle_helpers import short_dir

    path = short_dir()
    try:
        yield path
    finally:
        shutil.rmtree(path)


@pytest.fixture
def listener(secure_tree):
    try:
        value = Listener(secure_tree / "r", WS, host_uid=UID, gid=GID)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    try:
        yield value
    finally:
        value.close()


def server_for(listener, **kwargs):
    return Server(
        listener,
        kwargs.pop("registry", Registry()),
        kwargs.pop("limits", HostLimits()),
        kwargs.pop("handler", handler),
        META,
        **kwargs,
    )


async def read_frame(sock, timeout=2):
    loop = asyncio.get_running_loop()
    decoder = frames.Decoder()
    while True:
        data = await asyncio.wait_for(loop.sock_recv(sock, 1), timeout)
        if not data:
            return None
        items = decoder.feed(data)
        if items:
            assert not isinstance(items[0], frames.FrameError)
            return items[0]


async def send_frame(sock, frame):
    await asyncio.get_running_loop().sock_sendall(sock, frames.encode(frame))


@contextlib.asynccontextmanager
async def running(server):
    task = asyncio.create_task(server.serve())
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_connect(client, str(server.listener.path))
        yield client, task
    finally:
        client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_checked_directory_create_modes_and_recheck(secure_tree):
    with contextlib.ExitStack() as stack:
        directory = CheckedDirectory(secure_tree / "run", UID, mode=0o711)
        stack.callback(directory.close)
        assert stat.S_IMODE(os.fstat(directory.fd).st_mode) == 0o711
        directory.recheck()
        (secure_tree / "run").chmod(0o777)
        with pytest.raises(UnsafePath):
            directory.recheck()


def test_symlink_and_writable_ancestor_refused(secure_tree):
    (secure_tree / "actual").mkdir()
    (secure_tree / "link").symlink_to(secure_tree / "actual", target_is_directory=True)
    with pytest.raises(OSError):
        CheckedDirectory(secure_tree / "link" / "run", UID, mode=0o711)
    (secure_tree / "actual").chmod(0o777)
    with pytest.raises(UnsafePath):
        CheckedDirectory(secure_tree / "actual" / "run", UID, mode=0o711)


def test_ancestor_replacement_even_with_correct_modes(secure_tree):
    parent = secure_tree / "parent"
    parent.mkdir(mode=0o700)
    directory = CheckedDirectory(parent / "run", UID, mode=0o711)
    try:
        parent.rename(secure_tree / "old")
        parent.mkdir(mode=0o700)
        (parent / "run").mkdir(mode=0o711)
        (parent / "run").chmod(0o711)
        with pytest.raises(UnsafePath, match="replaced"):
            directory.recheck()
    finally:
        directory.close()


def test_foreign_helper_owner_names_owner(monkeypatch):
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_uid=UID + 123, st_mode=stat.S_IFDIR | 0o755))
    with pytest.raises(UnsafePath, match=str(UID + 123)):
        check_directory(42, Path("/Users/Shared/orch"), UID)


@pytest.mark.parametrize("mode", [0o700, 0o775, 0o1777])
def test_helper_requires_root_0755(monkeypatch, mode):
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | mode))
    with pytest.raises(UnsafePath):
        check_directory(42, Path("/Users/Shared/orch"), UID)


def test_sticky_parent_requires_root_helper(secure_tree, monkeypatch):
    import orch.host._unix as unix

    original = unix.check_directory

    def checked(fd, path, host_uid, *, helper=False):
        if path == secure_tree:
            return True
        return original(fd, path, host_uid, helper=helper)

    monkeypatch.setattr(unix, "check_directory", checked)
    if UID == 0:
        pytest.skip("This assertion needs a non-root current UID")
    with pytest.raises(UnsafePath, match="expected root 0755"):
        CheckedDirectory(secure_tree / "run", UID, mode=0o711)


def acl(*entries):
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


@pytest.mark.parametrize("entry", [(2, 2, 99999), (4, 2, 0xFFFFFFFF), (8, 2, 99999), (32, 2, 0xFFFFFFFF)])
def test_linux_acl_rejects_foreign_write(entry):
    with pytest.raises(UnsafePath):
        _linux_acl(acl(entry), {0, UID}, empty=False)


def test_linux_acl_masks_and_read_only_entries():
    _linux_acl(acl((2, 6, 99999), (16, 4, 0xFFFFFFFF)), {0, UID}, empty=False)
    _linux_acl(acl((2, 7, UID)), {0, UID}, empty=False)
    with pytest.raises(UnsafePath):
        _linux_acl(acl((2, 4, UID)), {0, UID}, empty=True)
    with pytest.raises(UnsafePath):
        _linux_acl(b"bad", {UID}, empty=False)


def test_acl_inspection_failure_closed(monkeypatch):
    monkeypatch.setattr(sys, "platform", "unsupported")
    with pytest.raises(UnsafePath):
        check_acl(42, {UID})


def test_linux_default_acl_checked(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        os, "getxattr", lambda fd, name: acl((2, 2, 99999)) if name.endswith("default") else acl(), raising=False
    )
    with pytest.raises(UnsafePath):
        check_acl(42, {UID})


def test_real_platform_acl(secure_tree):
    fd = os.open(secure_tree, os.O_RDONLY)
    try:
        check_acl(fd, {0, UID})
    finally:
        os.close(fd)


@pytest.mark.parametrize("path", [Path("/" + "a" * 100), Path("/" + "é" * 50), Path("relative"), Path("/a/../b")])
def test_socket_path_limit(path):
    with pytest.raises(UnsafePath):
        check_socket_path(path)
    check_socket_path(Path("/" + "a" * 99))


def test_directory_and_socket_modes(listener):
    assert stat.S_IMODE(listener.path.stat().st_mode) == 0o660
    assert stat.S_IMODE(listener.path.parent.stat().st_mode) == 0o750
    assert stat.S_IMODE(listener.path.parent.parent.stat().st_mode) == 0o711
    assert listener.path.stat().st_gid == GID
    listener.recheck()


def test_bind_restores_cwd_and_umask(secure_tree, monkeypatch):
    cwd = Path.cwd()
    old = os.umask(0o027)
    monkeypatch.setattr(socket.socket, "bind", lambda *args: (_ for _ in ()).throw(OSError(errno.EPERM, "test")))
    try:
        with pytest.raises(OSError):
            Listener(secure_tree / "r", WS, host_uid=UID, gid=GID)
        assert Path.cwd() == cwd
        assert os.umask(0o027) == 0o027
    finally:
        os.umask(old)


def test_bind_uses_checked_fd_despite_replacement(secure_tree, monkeypatch):
    # Replace an ancestor between validation and bind. bind still operates in the
    # original inode, and post-bind recheck refuses service and cleans only it.
    original = Listener._bind
    bound_at = []
    original_bind = socket.socket.bind

    def bind_socket(sock, address):
        bound_at.append(Path(address).resolve().parent)
        return original_bind(sock, address)

    def replaced(self, gid):
        self.path.parent.rename(self.path.parent.with_name("old"))
        self.path.parent.mkdir(mode=0o750)
        original(self, gid)

    monkeypatch.setattr(Listener, "_bind", replaced)
    monkeypatch.setattr(socket.socket, "bind", bind_socket)
    try:
        with pytest.raises(UnsafePath):
            Listener(secure_tree / "r", WS, host_uid=UID, gid=GID)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    assert bound_at == [secure_tree / "r" / "old"]
    assert not (secure_tree / "r" / WS / "host.sock").exists()
    assert not (secure_tree / "r" / "old" / "host.sock").exists()


def test_live_lock_and_stale_socket(listener):
    with pytest.raises(BlockingIOError):
        Listener(listener.path.parent.parent, WS, host_uid=UID, gid=GID)
    # Simulate a crash: close socket/lock/fds without unlinking the stale socket.
    listener.sock.close()
    listener.sock = None
    listener.identity = None
    path = listener.path
    listener.close()
    replacement = Listener(path.parent.parent, WS, host_uid=UID, gid=GID)
    replacement.close()
    assert not path.exists()


def test_planted_file_and_symlink(secure_tree):
    run = secure_tree / "r"
    run.mkdir(mode=0o711)
    run.chmod(0o711)
    directory = run / WS
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    path = directory / "host.sock"
    path.write_text("not a socket")
    with pytest.raises(UnsafePath, match="Planted"):
        Listener(run, WS, host_uid=UID, gid=GID)
    assert path.read_text() == "not a socket"
    path.unlink()
    path.symlink_to(secure_tree / "elsewhere")
    with pytest.raises(UnsafePath, match="Planted"):
        Listener(run, WS, host_uid=UID, gid=GID)
    assert path.is_symlink()


def test_foreign_socket_owner_refused(listener, monkeypatch):
    path = listener.path
    listener.sock.close()
    listener.sock = None
    listener.identity = None
    listener.close()
    original = os.stat

    def foreign(name, *args, **kwargs):
        info = original(name, *args, **kwargs)
        if str(name) == "host.sock":
            return SimpleNamespace(st_mode=info.st_mode, st_uid=UID + 123)
        return info

    monkeypatch.setattr(os, "stat", foreign)
    with pytest.raises(UnsafePath, match="Planted socket"):
        Listener(path.parent.parent, WS, host_uid=UID, gid=GID)
    assert path.exists()


def test_binding_while_threads_do_relative_file_work(secure_tree, monkeypatch):
    monkeypatch.chdir(secure_tree)
    Path("sentinel").write_text("unchanged")
    stop = threading.Event()
    started = threading.Event()
    failures = []

    def relative_work():
        while not stop.is_set():
            try:
                assert Path("sentinel").read_text() == "unchanged"
                Path("worker-output").write_text("worker")
            except Exception as exc:
                failures.append(exc)
            started.set()

    thread = threading.Thread(target=relative_work)
    thread.start()
    started.wait()
    try:
        try:
            value = Listener(secure_tree / "r", WS, host_uid=UID, gid=GID)
        except OSError as exc:
            if exc.errno == errno.EPERM:
                pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
            raise
        try:
            value.recheck()
            assert Path.cwd() == secure_tree
            assert not failures
        finally:
            value.close()
    finally:
        stop.set()
        thread.join()


def test_binding_refuses_missing_safe_primitive(secure_tree, monkeypatch):
    monkeypatch.setattr(listener_module.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    if sys.platform != "darwin":
        monkeypatch.setattr(Path, "is_dir", lambda self: False)
    value = Listener.__new__(Listener)
    value.directory = SimpleNamespace(fd=42)
    with pytest.raises(UnsafePath, match="requires"):
        value._bind(GID)


def test_bind_does_not_chmod_planted_symlink(secure_tree, monkeypatch):
    target = secure_tree / "target"
    target.write_text("private")
    target.chmod(0o600)

    def replaced(sock, address):
        (secure_tree / "r" / WS / "host.sock").symlink_to(target)

    monkeypatch.setattr(socket.socket, "bind", replaced)
    with pytest.raises(UnsafePath, match="replaced"):
        Listener(secure_tree / "r", WS, host_uid=UID, gid=GID)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENODATA])
def test_linux_no_acl_support_is_no_acl(monkeypatch, code):
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []

    def unsupported(fd, name):
        calls.append(name)
        raise OSError(code, "No ACL support")

    monkeypatch.setattr(os, "getxattr", unsupported, raising=False)
    check_acl(42, {UID})
    assert calls == ["system.posix_acl_access", "system.posix_acl_default"]


def test_native_peer_credentials():
    a, b = socket.socketpair()
    try:
        credentials = peer_credentials(a)
        assert (credentials.uid, credentials.gid, credentials.pid) == (UID, GID, os.getpid())
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_peer_credential_structs(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)

    class Sock:
        def getsockopt(self, level, option, length):
            if platform == "linux":
                return struct.pack("3i", 321, 123, 456)
            return struct.pack("i", 321) if option == 2 else struct.pack("@IIh2x16I", 0, 123, 1, 456, *([0] * 15))

    # SO_PEERCRED is absent on macOS, but its Linux numeric value is stable.
    monkeypatch.setattr(socket, "SO_PEERCRED", 17, raising=False)
    assert peer_credentials(Sock()) == Credentials(123, 456, 321)


def test_uid_checked_before_client_sends(listener, monkeypatch):
    monkeypatch.setattr(client_module, "peer_credentials", lambda sock: Credentials(UID + 1, GID, 1))
    with pytest.raises(UnsafePath, match="UID"):
        connect_pinned(Pin(UID, WS, listener.path))
    accepted, _ = listener.sock.accept()
    try:
        accepted.settimeout(1)
        assert accepted.recv(1) == b""
    finally:
        accepted.close()


def test_pin_workspace_checked(listener):
    server = server_for(listener)
    welcome = server.welcome(Peer(UID, "agent"), 1)
    Pin(UID, WS, listener.path).check_welcome(welcome)
    with pytest.raises(UnsafePath):
        Pin(UID, "b" * 32, listener.path).check_welcome(welcome)
    with pytest.raises(UnsafePath):
        Pin(UID + 1, WS, listener.path).check_welcome(welcome)


@pytest.mark.parametrize(
    "kind,uid,worker,allowed",
    [
        ("workspace", 123, None, "agent"),
        ("workspace", 124, None, None),
        ("control", UID, None, "control"),
        ("control", UID + 123, None, None),
        ("workspace", 123, 123, None),
        ("control", UID, UID, None),
        ("worker", 123, 123, "worker"),
        ("worker", 124, 123, None),
    ],
)
def test_listener_class_rules(kind, uid, worker, allowed):
    listener = SimpleNamespace(kind=kind, host_uid=UID, workspace_id=WS)
    server = server_for(
        listener, registry=Registry({123: Peer(123, "agent")}, worker), worker_identity=lambda cred: True
    )
    if allowed:
        assert server.classify(Credentials(uid, GID, 1)).kind == allowed
    else:
        with pytest.raises(frames.FrameError, match="not allowed"):
            server.classify(Credentials(uid, GID, 1))


def test_worker_requires_identity_hook_and_single_connection():
    listener = SimpleNamespace(kind="worker", host_uid=UID, workspace_id=None)
    server = server_for(listener, registry=Registry(worker_uid=123))
    with pytest.raises(frames.FrameError):
        server.classify(Credentials(123, 1, 42))
    server.worker_identity = lambda cred: cred.pid == 42  # H10 also verifies start time.
    assert server.classify(Credentials(123, 1, 42)).kind == "worker"
    with pytest.raises(frames.FrameError):
        server.classify(Credentials(123, 1, 43))
    peer = Peer(123, "worker")
    server.limits.acquire(peer)
    with pytest.raises(frames.FrameError) as error:
        server.limits.acquire(peer)
    assert error.value.code == "peer.denied"
    server.limits.release(peer)
    server.limits.acquire(peer)


def test_uid_limits_and_agent_bucket():
    clock = Clock()
    limits = HostLimits(clock=clock)
    peer = Peer(123, "agent")
    for _ in range(32):
        limits.acquire(peer)
    with pytest.raises(frames.FrameError) as error:
        limits.acquire(peer)
    assert error.value.response()["t"] == "fatal"
    limits.release(peer)
    limits.acquire(peer)
    assert all(limits.request_allowed(peer) for _ in range(100))
    assert not limits.request_allowed(peer)
    assert limits.request_allowed(Peer(456, "agent"))
    assert limits.request_allowed(Peer(123, "person"))
    clock.now += 0.05
    assert limits.request_allowed(peer)
    attempts = HostLimits(clock=clock, connection_rate=(1, 1))
    attempts.acquire(peer)
    attempts.release(peer)
    with pytest.raises(frames.FrameError):
        attempts.acquire(peer)


def test_byte_budget_and_bucket_are_bounded():
    clock = Clock()
    budget = ByteBudget(clock)
    budget.add(transport.BYTES_PER_MINUTE)
    with pytest.raises(frames.FrameError) as exc:
        budget.add(1)
    assert exc.value.code == "conn.overloaded"
    assert exc.value.fate == "close"
    clock.now = 61
    budget.add(1)
    assert budget.total == 1
    for _ in range(1000):
        budget.add(1)
    assert len(budget.samples) == 1
    bucket = Bucket(20, 100, clock)
    clock.now += 100
    assert sum(bucket.take() for _ in range(101)) == 100


def test_unauthorized_kernel_uid_denied_without_client_bytes(listener):
    async def scenario():
        registry = Registry({})
        server = server_for(listener, registry=registry)
        async with running(server) as (client, _):
            fatal = await read_frame(client)
            assert fatal["error"]["code"] == "peer.denied"
            assert await read_frame(client) is None
            assert registry.lookups == [(UID, WS)]

    asyncio.run(scenario())


def test_handshake_request_malformed_and_reuse(listener):
    async def scenario():
        async with running(server_for(listener)) as (client, _):
            for byte in frames.encode(HELLO):
                await asyncio.get_running_loop().sock_sendall(client, bytes([byte]))
            assert (await read_frame(client))["t"] == "welcome"
            await send_frame(client, req() | {"unknown": True})
            assert (await read_frame(client))["error"]["code"] == "req.invalid"
            await send_frame(client, req())
            assert (await read_frame(client))["ok"]
            await send_frame(client, req())
            assert (await read_frame(client))["ok"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "first,code", [(req(), "handshake.invalid"), (HELLO | {"proto": [99]}, "host.version_mismatch")]
)
def test_handshake_failures(listener, first, code):
    async def scenario():
        async with running(server_for(listener)) as (client, _):
            await send_frame(client, first)
            assert (await read_frame(client))["error"]["code"] == code
            assert await read_frame(client) is None

    asyncio.run(scenario())


def test_request_before_welcome_is_fatal(listener):
    async def scenario():
        server = server_for(listener)
        async with running(server) as (client, _):
            await asyncio.get_running_loop().sock_sendall(client, frames.encode(HELLO) + frames.encode(req()))
            reply = await read_frame(client)
            if reply["t"] == "welcome":
                reply = await read_frame(client)
            assert reply["error"]["code"] == "handshake.invalid"
            assert await read_frame(client) is None
            await asyncio.wait_for(asyncio.gather(*server.tasks), 1)
            assert not server.sessions
            assert not server.tasks
            assert not server.limits.connections

    asyncio.run(scenario())


def test_fatal_during_completed_write_finishes_session(monkeypatch):
    async def scenario():
        sent, closed = [], []
        sock = SimpleNamespace(shutdown=lambda how: closed.append(how), close=lambda: closed.append("close"))
        server = server_for(SimpleNamespace())
        peer = Peer(UID, "agent")
        session = Session(sock, peer, Credentials(UID, GID, 1), server)

        async def read():
            # A read queues a reply and then fails in the same turn, as with
            # coalesced hello/request or requests overflowing the write queue.
            session.send(result())
            raise frames.FrameError("handshake.invalid")

        async def sendall(sock, data):
            # Immediate completion races with the supervisor cancelling the
            # writer inside wait_for on Python 3.11.
            sent.append(data)

        monkeypatch.setattr(session, "_read", read)
        monkeypatch.setattr(asyncio.get_running_loop(), "sock_sendall", sendall)
        server.limits.acquire(peer)
        server.sessions.add(session)
        task = asyncio.create_task(session.run())
        server.tasks.add(task)
        task.add_done_callback(lambda task: server._session_done(session, task))
        try:
            done, _ = await asyncio.wait({task}, timeout=1)
            assert task in done, "Fatal disposition left the session running"
            task.result()
            assert frames.Decoder().feed(sent[-1])[0]["t"] == "fatal"
            assert closed[:2] == [socket.SHUT_RDWR, "close"]
            assert not server.sessions
            assert not server.tasks
            assert not server.limits.connections
            assert not session.tasks
            assert not (asyncio.all_tasks() - {asyncio.current_task()})
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_connection_quota_before_hello(listener):
    async def scenario():
        limits = HostLimits()
        limits.connections[UID] = 32
        async with running(server_for(listener, limits=limits)) as (client, _):
            assert (await read_frame(client))["error"]["code"] == "quota.host"
            assert limits.connections[UID] == 32

    asyncio.run(scenario())


def test_monitor_stops_serving_on_replaced_ancestor(listener, monkeypatch):
    monkeypatch.setattr(transport, "RECHECK_SECONDS", 0.02)

    async def scenario():
        server = server_for(listener)
        async with running(server) as (client, task):
            await send_frame(client, HELLO)
            assert (await read_frame(client))["t"] == "welcome"
            run = listener.path.parent.parent
            run.rename(run.with_name("old"))
            run.mkdir(mode=0o711)
            run.chmod(0o711)
            with pytest.raises((UnsafePath, OSError)):
                await asyncio.wait_for(task, 1)
            assert await read_frame(client) is None
            assert not server.sessions
            assert not server.limits.connections

    asyncio.run(scenario())


@contextlib.asynccontextmanager
async def pair_session(clock=None, callback=handler):
    host, client = socket.socketpair()
    host.setblocking(False)
    client.setblocking(False)
    clock = clock or Clock()
    listener = SimpleNamespace(kind="workspace", host_uid=UID, workspace_id=WS)
    server = server_for(listener, clock=clock, handler=callback)
    session = Session(host, Peer(UID, "agent"), Credentials(UID, GID, os.getpid()), server)
    task = asyncio.create_task(session.run())
    try:
        yield session, client, task, clock
    finally:
        client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("partial,code", [(False, "handshake.invalid"), (True, "frame.invalid")])
def test_deadlines_without_further_bytes(partial, code):
    async def scenario():
        async with pair_session() as (session, client, _, clock):
            if partial:
                await send_frame(client, HELLO)
                assert (await read_frame(client))["t"] == "welcome"
                await asyncio.get_running_loop().sock_sendall(client, b"\x00")
                while not session.decoder.pending:
                    await asyncio.sleep(0.001)
            clock.now = 11
            fatal = await read_frame(client)
            assert fatal["error"]["code"] == code
            assert await read_frame(client) is None

    asyncio.run(scenario())


def test_idle_timeout_and_subscription_exemption():
    async def scenario():
        async with pair_session() as (session, client, task, clock):
            await send_frame(client, HELLO)
            await read_frame(client)
            session.subscriptions = 1
            clock.now = 601
            await asyncio.sleep(0.15)
            assert not task.done()
            session.subscriptions = 0
            assert await read_frame(client) is None

    asyncio.run(scenario())


def test_inflight_quota_cancel_and_idle_exemption():
    async def scenario():
        def parked(session, frame, request):
            session.admit(request)

            async def run():
                session.state.start(request, "wait")
                await asyncio.Event().wait()

            return run()

        async with pair_session(callback=parked) as (session, client, task, clock):
            await send_frame(client, HELLO)
            await read_frame(client)
            for i in range(9):
                await send_frame(client, req(str(i)))
            assert (await read_frame(client))["error"]["code"] == "quota.host"
            assert session.state.slots == 8
            clock.now = 601
            await asyncio.sleep(0.15)
            assert not task.done()
            await send_frame(client, {"t": "cancel", "id": "0"})
            assert (await read_frame(client))["error"]["code"] == "cancelled"
            await asyncio.sleep(0)
            assert session.state.slots == 7

    asyncio.run(scenario())


def test_bounded_queue_and_fatal_disposition(monkeypatch):
    monkeypatch.setattr(transport, "WRITE_QUEUE_FRAMES", 2)

    async def scenario():
        async with pair_session() as (_, client, _, _):
            await send_frame(client, HELLO)
            await read_frame(client)
            # Coalesced malformed requests bypass admission but cannot grow an
            # unbounded queue. No writer has begun a partial frame at overflow.
            data = b"".join(frames.encode(req(str(i)) | {"bad": True}) for i in range(4))
            await asyncio.get_running_loop().sock_sendall(client, data)
            replies = []
            while (reply := await read_frame(client)) is not None:
                replies.append(reply)
            assert replies[-1]["t"] == "fatal"
            assert replies[-1]["error"]["code"] == "conn.overloaded"
            assert len(replies) <= 3

    asyncio.run(scenario())


def test_write_queue_byte_bound():
    host, client = socket.socketpair()
    try:
        server = server_for(SimpleNamespace(kind="workspace", host_uid=UID, workspace_id=WS))
        session = Session(host, Peer(UID, "agent"), Credentials(UID, GID, 1), server)
        response = result()
        response["result"]["data"] = "x" * (frames.MAX_FRAME - 1000)
        session.send(response)
        session.send(response)
        with pytest.raises(frames.FrameError, match="Slow reader"):
            session.send(response)
        assert session.queued_bytes <= transport.WRITE_QUEUE_BYTES
    finally:
        host.close()
        client.close()


def test_slow_reader_write_timeout_closes_without_corrupt_fatal(monkeypatch):
    monkeypatch.setattr(transport, "WRITE_TIMEOUT_SECONDS", 0.02)

    async def scenario():
        async with pair_session() as (session, client, task, _):
            session.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            await send_frame(client, HELLO)
            await read_frame(client)
            response = result()
            response["result"]["data"] = "x" * 1_000_000
            session.send(response)
            await asyncio.wait_for(task, 1)
            assert session.closed
            assert session.writing  # Partial body: appending fatal would corrupt it.

    asyncio.run(scenario())


# Actual second-user checks are opt-in even when pytest runs as root. A dedicated
# container/dev-user fixture should set ORCH_TEST_OS_USERS=1 and execute as root.
OS_USERS = pytest.mark.skipif(
    os.environ.get("ORCH_TEST_OS_USERS") != "1" or os.geteuid() != 0,
    reason="needs root and explicit ORCH_TEST_OS_USERS=1 for a second real UID",
)


@pytest.mark.needs_os_users
@OS_USERS
def test_second_os_uid_is_denied_before_sending_bytes():
    base = Path(tempfile.mkdtemp(prefix="orch-users-", dir="/private/tmp" if sys.platform == "darwin" else "/tmp"))
    base.chmod(0o755)  # Root-owned helper boundary under the sticky directory.
    listener = Listener(base / "r", WS, host_uid=0, gid=0)
    code = """
import os, socket, sys
os.setgroups([])
os.setgid(0)  # Deliberately permit path access so kernel-UID admission is tested.
os.setuid(65534)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
size = int.from_bytes(s.recv(4), 'big')
data = b''
while len(data) < size:
    data += s.recv(size - len(data))
assert b'peer.denied' in data
assert s.recv(1) == b''
"""

    async def scenario():
        server = server_for(listener, registry=Registry({}))
        task = asyncio.create_task(server.serve())
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(listener.path))
            assert await asyncio.wait_for(process.wait(), 5) == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        listener.close()
        shutil.rmtree(base)


@pytest.mark.needs_os_users
@OS_USERS
def test_actual_foreign_owned_planted_socket(secure_tree):
    run = secure_tree / "r"
    run.mkdir(mode=0o711)
    run.chmod(0o711)
    directory = run / WS
    directory.mkdir(mode=0o750)
    directory.chmod(0o750)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as planted:
        planted.bind(str(directory / "host.sock"))
        os.chown(directory / "host.sock", 65534, -1)
        with pytest.raises(UnsafePath, match="Planted"):
            Listener(run, WS, host_uid=0, gid=0)


def test_handshake_pinned_checks_workspace_before_returning(listener):
    # Child process avoids both blocking the server loop and starting threads.
    code = """
import sys
from pathlib import Path
from orch.host.transport import Pin, handshake_pinned
from orch.host._unix import UnsafePath
hello = {'t':'hello', 'proto':[1], 'client':'test', 'caps':[]}
try:
    handshake_pinned(Pin(int(sys.argv[2]), 'b'*32, Path(sys.argv[1])), hello)
except UnsafePath:
    pass
else:
    raise AssertionError('Wrong workspace accepted')
"""

    async def scenario():
        server = server_for(listener)
        task = asyncio.create_task(server.serve())
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(listener.path), str(UID))
            assert await asyncio.wait_for(process.wait(), 5) == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_handshake_helper_rejects_request_before_connect():
    with pytest.raises(frames.FrameError):
        handshake_pinned(Pin(UID, WS, Path("/nonexistent")), req())


def test_native_acl_write_rejected(secure_tree):
    if sys.platform != "darwin":
        pytest.skip("Native Darwin ACL test; POSIX ACL decoding is covered separately")
    path = secure_tree / "acl"
    path.mkdir()
    subprocess.run(["/bin/chmod", "+a", "everyone allow add_subdirectory,delete_child", str(path)], check=True)
    try:
        with pytest.raises(UnsafePath, match="ACL grants write"):
            CheckedDirectory(path, UID, mode=0o700)
    finally:
        subprocess.run(["/bin/chmod", "-N", str(path)], check=True)


@pytest.mark.parametrize("kind,mode", [("control", 0o700), ("worker", 0o750)])
def test_separate_listeners_modes_and_welcome(secure_tree, kind, mode):
    try:
        listener = Listener(secure_tree / "r", kind, host_uid=UID, gid=GID, kind=kind)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    server = server_for(
        listener,
        registry=Registry(worker_uid=UID if kind == "worker" else None),
        worker_identity=lambda credentials: credentials.pid == os.getpid(),
    )

    async def scenario():
        async with running(server) as (client, _):
            assert stat.S_IMODE(listener.path.parent.stat().st_mode) == mode
            await send_frame(client, HELLO)
            welcome = await read_frame(client)
            assert welcome["peer"]["class"] == kind
            assert welcome["workspace"] is None

    try:
        asyncio.run(scenario())
    finally:
        listener.close()


def test_control_rejects_simulated_agent_uid_before_read(secure_tree, monkeypatch):
    try:
        listener = Listener(secure_tree / "r", "control", host_uid=UID, gid=GID, kind="control")
    except OSError as exc:
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    monkeypatch.setattr(transport, "peer_credentials", lambda sock: Credentials(UID + 123, GID, 1))
    registry = Registry({UID + 123: Peer(UID + 123, "agent")})

    async def scenario():
        async with running(server_for(listener, registry=registry)) as (client, _):
            assert (await read_frame(client))["error"]["code"] == "peer.denied"
            assert await read_frame(client) is None

    try:
        asyncio.run(scenario())
    finally:
        listener.close()


def test_cancel_before_work_starts_releases_slot():
    async def scenario():
        started = []

        def dispatch(session, frame, request):
            session.admit(request)

            async def run():
                started.append(True)
                await asyncio.Event().wait()

            return run()

        async with pair_session(callback=dispatch) as (session, client, _, _):
            await send_frame(client, HELLO)
            await read_frame(client)
            data = frames.encode(req()) + frames.encode({"t": "cancel", "id": "r"})
            await asyncio.get_running_loop().sock_sendall(client, data)
            assert (await read_frame(client))["error"]["code"] == "cancelled"
            await asyncio.sleep(0)
            assert not started
            assert session.state.slots == 0

    asyncio.run(scenario())


def test_dispatch_precedence_before_rate_limit():
    async def scenario():
        def denied(session, frame, request):
            session.respond(
                request,
                {
                    "t": "res",
                    "id": request.id,
                    "ok": False,
                    "exit": 2,
                    "error": {"code": "unknown_command", "message": "Unknown op", "retryable": False},
                },
            )

        async with pair_session(callback=denied) as (session, client, _, _):
            for _ in range(100):
                session.server.limits.request_allowed(session.peer)
            for bucket in session.server.limits.agents.values():
                bucket.rate = 0  # no refill: the result must not depend on how fast this machine is
            await send_frame(client, HELLO)
            await read_frame(client)
            await send_frame(client, req())
            assert (await read_frame(client))["error"]["code"] == "unknown_command"
            session.server.handler = handler
            await send_frame(client, req())
            assert (await read_frame(client))["error"]["code"] == "rate.limited"

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [True, False])
def test_handler_error_is_request_scoped(asynchronous):
    def broken(session, frame, request):
        if not asynchronous:
            raise RuntimeError("private details")
        session.admit(request)

        async def run():
            raise RuntimeError("private details")

        return run()

    async def scenario():
        async with pair_session(callback=broken) as (session, client, _, _):
            await send_frame(client, HELLO)
            await read_frame(client)
            await send_frame(client, req())
            response = await read_frame(client)
            assert response["error"]["code"] == "internal"
            assert "private details" not in str(response)
            session.server.handler = handler
            await send_frame(client, req())
            assert (await read_frame(client))["ok"]

    asyncio.run(scenario())


def test_default_run_paths(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert transport.default_run_directory(501) == Path("/Users/Shared/orch/run/501")
    monkeypatch.setattr(sys, "platform", "linux")
    assert transport.default_run_directory(1000) == Path("/run/orch/1000")


def test_native_linux_default_acl(secure_tree):
    if sys.platform != "linux":
        pytest.skip("Native Linux POSIX ACL test")
    path = secure_tree / "acl"
    path.mkdir(mode=0o700)
    data = acl((1, 7, 0xFFFFFFFF), (2, 2, UID + 123), (4, 0, 0xFFFFFFFF), (16, 2, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF))
    try:
        os.setxattr(path, "system.posix_acl_default", data)
    except OSError as exc:
        if exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            pytest.skip("Filesystem does not support POSIX ACLs")
        raise
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(UnsafePath, match="ACL grants write"):
            check_acl(fd, {0, UID})
    finally:
        os.close(fd)


def test_acl_read_error_refuses_start(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def unreadable(*args):
        raise OSError(errno.EACCES, "ACL unavailable")

    monkeypatch.setattr(os, "getxattr", unreadable, raising=False)
    with pytest.raises(UnsafePath, match="Cannot inspect"):
        check_acl(42, {UID})


def test_hello_deadline_starts_at_accept_not_after_registry():
    async def scenario():
        async with pair_session() as (session, client, _, clock):
            # Represent time spent resolving credentials/registry after accept.
            session.accepted = -3
            await send_frame(client, HELLO)
            assert (await read_frame(client))["error"]["code"] == "handshake.invalid"

    asyncio.run(scenario())


@pytest.mark.parametrize("after_hello", [False, True])
def test_rights_are_closed_and_fatal(after_hello):
    import array

    async def scenario():
        async with pair_session() as (_, client, task, _):
            if after_hello:
                await send_frame(client, HELLO)
                await read_frame(client)
            with open(__file__) as source:
                before = len(os.listdir("/dev/fd"))
                payload = frames.encode(req() if after_hello else HELLO)
                client.sendmsg(
                    [payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [source.fileno()] * 50))]
                )
                fatal = await read_frame(client)
                assert fatal["error"]["code"] == "frame.invalid"
                await asyncio.wait_for(task, 1)
                assert len(os.listdir("/dev/fd")) <= before

    asyncio.run(scenario())


def test_truncated_control_data_closes_visible_rights():
    from orch.host._socket import recv

    fd = os.open(__file__, os.O_RDONLY)
    sock = SimpleNamespace(
        recvmsg=lambda size, control: (
            b"data",
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))],
            socket.MSG_CTRUNC,
            None,
        )
    )
    with pytest.raises(frames.FrameError, match="Ancillary"):
        recv(sock)
    with pytest.raises(OSError):
        os.fstat(fd)
    with pytest.raises(frames.FrameError):
        recv(SimpleNamespace(recvmsg=lambda *args: (b"", [], socket.MSG_CTRUNC, None)))


def test_pinned_handshake_rejects_rights_without_leaking(monkeypatch):
    import array

    host, client = socket.socketpair()
    monkeypatch.setattr(client_module, "connect_pinned", lambda *args, **kwargs: client)
    try:
        with open(__file__) as source:
            before = len(os.listdir("/dev/fd"))
            welcome = server_for(SimpleNamespace(kind="workspace", host_uid=UID, workspace_id=WS)).welcome(
                Peer(UID, "agent"), 1
            )
            host.sendmsg(
                [frames.encode(welcome)],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [source.fileno()] * 50))],
            )
            with pytest.raises(frames.FrameError, match="Ancillary"):
                handshake_pinned(Pin(UID, WS, Path("/unused")), HELLO)
            assert len(os.listdir("/dev/fd")) <= before
    finally:
        host.close()
        client.close()


def test_blocked_loop_does_not_lose_requests():
    import time

    async def scenario():
        async with pair_session() as (_, client, _, _):
            await send_frame(client, HELLO)
            await read_frame(client)
            for i in range(5):

                def send(request_id=str(i)):
                    time.sleep(0.05)
                    client.sendall(frames.encode(req(request_id)))

                thread = threading.Thread(target=send)
                thread.start()
                time.sleep(0.3)  # Reader and old wait_for timer become ready together.
                thread.join()
                response = await read_frame(client)
                assert response["id"] == str(i)
                assert response["ok"]

    asyncio.run(scenario())


def test_unexpected_receive_exception_is_fatal_internal():
    async def scenario():
        async with pair_session() as (session, client, _, _):
            session.server.metadata = {}  # Invalid welcome, outside the request handler.
            await send_frame(client, HELLO)
            fatal = await read_frame(client)
            assert fatal == {
                "t": "fatal",
                "error": {"code": "internal", "message": "Internal host error", "retryable": False},
            }
            assert await read_frame(client) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("code", sorted(transport.TRANSIENT_ACCEPT_ERRORS))
def test_accept_retries_transient_errors(monkeypatch, code):
    async def scenario():
        server = server_for(SimpleNamespace(sock=object()))
        attempts = 0
        recovered = asyncio.Event()

        async def accept(sock):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError(code, "transient")
            recovered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(asyncio.get_running_loop(), "sock_accept", accept)
        task = asyncio.create_task(server._accept())
        try:
            await asyncio.wait_for(recovered.wait(), 1)
            assert not task.done()
            assert attempts == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_recheck_retries_exhaustion_but_stops_on_violation(monkeypatch):
    monkeypatch.setattr(transport, "RECHECK_SECONDS", 0.001)
    calls = []

    def recheck():
        calls.append(True)
        if len(calls) == 1:
            raise OSError(errno.EMFILE, "No descriptors")
        raise UnsafePath("ancestor replaced")

    async def scenario():
        server = server_for(SimpleNamespace(recheck=recheck))
        with pytest.raises(UnsafePath, match="replaced"):
            await asyncio.wait_for(server._monitor(), 1)
        assert len(calls) == 2

    asyncio.run(scenario())


def test_host_cap_reserves_fd_headroom_across_uids(monkeypatch):
    monkeypatch.setattr(transport.resource, "getrlimit", lambda limit: (64, 256))
    limits = HostLimits()
    assert limits.max_connections == 16
    for uid in range(16):
        limits.acquire(Peer(uid, "agent"))
    with pytest.raises(frames.FrameError) as exc:
        limits.acquire(Peer(99, "person"))
    assert exc.value.code == "quota.host"
    limits.release(Peer(0, "agent"))
    limits.acquire(Peer(99, "person"))


def test_default_attempt_bucket_refills_per_uid():
    clock = Clock()
    limits = HostLimits(clock=clock)
    peer = Peer(123, "agent")
    for _ in range(40):
        limits.acquire(peer)
        limits.release(peer)
    with pytest.raises(frames.FrameError):
        limits.acquire(peer)
    limits.acquire(Peer(456, "agent"))
    clock.now += 0.05
    limits.acquire(peer)


def test_eight_unanswered_handlers_release_ids_slots_and_idle():
    def unanswered(session, frame, request):
        session.admit(request)

        async def run():
            session.state.start(request, "read")

        return run()

    async def scenario():
        async with pair_session(callback=unanswered) as (session, client, _, clock):
            await send_frame(client, HELLO)
            await read_frame(client)
            for i in range(8):
                await send_frame(client, req(str(i)))
                response = await read_frame(client)
                assert response["id"] == str(i)
                assert response["error"]["code"] == "internal"
            assert session.state.slots == 0
            session.server.handler = handler
            await send_frame(client, req("0"))
            assert (await read_frame(client))["ok"]
            clock.now = 601
            assert await read_frame(client) is None

    asyncio.run(scenario())


def test_cancelled_work_without_cancel_frame_answers_internal():
    async def scenario():
        entered = asyncio.Event()

        def parked(session, frame, request):
            session.admit(request)

            async def run():
                session.state.start(request, "wait")
                entered.set()
                await asyncio.Event().wait()

            return run()

        async with pair_session(callback=parked) as (session, client, _, _):
            await send_frame(client, HELLO)
            await read_frame(client)
            await send_frame(client, req())
            await entered.wait()
            next(iter(session.tasks.values())).cancel()
            assert (await read_frame(client))["error"]["code"] == "internal"
            assert session.state.slots == 0
            session.server.handler = handler
            await send_frame(client, req())
            assert (await read_frame(client))["ok"]

    asyncio.run(scenario())


def test_oversized_result_is_request_scoped_and_id_reusable():
    def oversized(session, frame, request):
        session.admit(request)

        async def run():
            session.state.start(request, "read")
            response = result(request.id)
            response["result"]["data"] = "x" * frames.MAX_FRAME
            session.respond(request, response)

        return run()

    async def scenario():
        async with pair_session(callback=oversized) as (session, client, _, _):
            await send_frame(client, HELLO)
            await read_frame(client)
            await send_frame(client, req())
            response = await read_frame(client, timeout=10)  # encoding a 2 MiB result is slow under load
            assert response["t"] == "res"
            assert response["error"]["code"] == "result.too_large"
            assert response["exit"] == 1
            assert response["error"]["retryable"] is False
            assert "limit" in response["error"]["hint"] and "page" in response["error"]["hint"]
            session.server.handler = handler
            await send_frame(client, req())
            assert (await read_frame(client))["ok"]

    asyncio.run(scenario())


def test_write_deadline_has_overloaded_disposition(monkeypatch):
    async def scenario():
        async with pair_session() as (session, client, _, _):
            await send_frame(client, HELLO)
            await read_frame(client)

            async def timeout(sock, data):
                raise TimeoutError

            monkeypatch.setattr(asyncio.get_running_loop(), "sock_sendall", timeout)
            session.send(result())
            # Run a separate writer to inspect the protocol error directly.
            with pytest.raises(frames.FrameError) as exc:
                await session._write()
            assert (exc.value.code, exc.value.exit, exc.value.retryable, exc.value.fate) == (
                "conn.overloaded",
                9,
                True,
                "close",
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM, errno.EINTR])
def test_acl_resource_failure_is_retryable_by_monitor(monkeypatch, code):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(transport, "RECHECK_SECONDS", 0.001)
    calls = []

    def exhausted(fd, name):
        calls.append(name)
        if len(calls) == 1:
            raise OSError(code, "Resource exhausted")
        raise OSError(errno.EACCES, "Actual inspection refusal")

    monkeypatch.setattr(os, "getxattr", exhausted, raising=False)

    async def scenario():
        server = server_for(SimpleNamespace(recheck=lambda: check_acl(42, {UID})))
        with pytest.raises(UnsafePath, match="Cannot inspect"):
            await asyncio.wait_for(server._monitor(), 1)
        assert len(calls) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [errno.EMFILE, errno.ENOMEM, errno.EACCES])
def test_native_acl_failure_preserves_resource_errno(code):
    from orch.host._unix import _acl_failure

    if code == errno.EACCES:
        with pytest.raises(UnsafePath):
            _acl_failure("ACL failed", code)
    else:
        with pytest.raises(OSError) as exc:
            _acl_failure("ACL failed", code)
        assert exc.value.errno == code

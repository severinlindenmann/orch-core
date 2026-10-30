"""No hub wait breaks at fd 1024 (#1740: the R4 redial e2e test failed in CI with ``LinkClosed`` because the hub's
``select.select`` raised ``ValueError: filedescriptor out of range in select()`` late in a long test worker, and the
hub dropped the client's connection without a word). The hub waits through :mod:`orch_hub.fdwait` only."""

from __future__ import annotations

import ast
import contextlib
import os
import resource
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import orch_hub
from orch_hub import fdwait
from orch_hub.link import tls
from orch_hub.tlsid import TlsIdentity

SRC = Path(orch_hub.__file__).parent
HIGH = 1100  # past FD_SETSIZE (1024)


@contextlib.contextmanager
def high_fds() -> Iterator[None]:
    """Hold enough dummy fds that the next one opened is numbered above ``HIGH``."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = HIGH + 64
    if soft < want:
        if hard != resource.RLIM_INFINITY and hard < want:
            pytest.skip(f"the fd limit ({hard}) is below {want}")
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    held: list[int] = []
    try:
        while True:
            fd = os.open(os.devnull, os.O_RDONLY)
            held.append(fd)
            if fd >= HIGH:
                break
        yield
    finally:
        for fd in held:
            os.close(fd)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_no_select_select_in_the_hub():
    hits = []
    for p in sorted(SRC.rglob("*.py")):
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if (
                isinstance(n, ast.Attribute)
                and n.attr == "select"
                and isinstance(n.value, ast.Name)
                and n.value.id == "select"
            ):
                hits.append(f"{p.relative_to(SRC)}:{n.lineno}")
            if isinstance(n, ast.ImportFrom) and n.module == "select" and any(a.name == "select" for a in n.names):
                hits.append(f"{p.relative_to(SRC)}:{n.lineno}")
    assert not hits, f"select.select breaks at fd 1024; wait through orch_hub.fdwait: {hits}"


def test_the_waits_work_above_fd_setsize():
    with high_fds():
        a, b = socket.socketpair()
        try:
            assert a.fileno() > 1024
            assert not fdwait.readable(a, 0)
            assert fdwait.writable(a, 1)
            b.send(b"x")
            assert fdwait.readable(a, 1)
        finally:
            a.close()
            b.close()


def test_a_pinned_tls_link_works_above_fd_setsize():
    """The deterministic repro of #1740: handshake, write and read on sockets numbered above 1024."""
    ident = TlsIdentity.generate()
    ctx = tls.server_context(ident)
    with high_fds():
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        got: dict[str, object] = {}

        def serve() -> None:
            s, _ = lsock.accept()
            try:
                conn = tls.accept(ctx, s, time.monotonic() + 10)
                got["fd"] = s.fileno()
                got["msg"] = conn.recv_exact(5, time.monotonic() + 10)
                conn.send_all(b"world", time.monotonic() + 10)
                got["ok"] = True
            except Exception as e:  # noqa: BLE001
                got["err"] = repr(e)
            finally:
                s.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            c = tls.connect_pinned("127.0.0.1", lsock.getsockname()[1], ident.spki_sha256, timeout=10)
            try:
                c.send_all(b"hello", time.monotonic() + 10)
                assert c.wait_readable(10)
                assert c.recv_exact(5, time.monotonic() + 10) == b"world"
            finally:
                c.close()
            t.join(10)
        finally:
            lsock.close()
        assert got.get("err") is None and got.get("ok") is True, got
        assert got["fd"] > 1024 and got["msg"] == b"hello"


def test_a_wait_on_a_closed_connection_is_link_closed_not_a_crash():
    """The endpoint's pump waits while the writer thread closes the connection: that must end as ``LinkClosed``
    (handled), not ``ValueError`` (logged as ``remote_endpoint_error``)."""
    a, b = socket.socketpair()
    a.setblocking(False)

    class _NoTls:
        def pending(self) -> int:
            return 0

    conn = tls.TlsConn(_NoTls(), a)  # type: ignore[arg-type]  # only the waits are used
    a.close()
    with pytest.raises(tls.LinkClosed):
        conn._wait(False, time.monotonic() + 1)
    assert conn.wait_readable(0) is True
    b.close()

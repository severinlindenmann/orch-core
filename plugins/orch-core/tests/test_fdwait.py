"""No wait on an fd breaks at fd 1024 (CI incident 21 Oct: ``ValueError: filedescriptor out of range in select()``).

``select.select`` refuses an fd at or above ``FD_SETSIZE``. Production code waits through :mod:`orch.fdwait`
(``selectors``: epoll, kqueue or poll), and nothing in ``src`` may call ``select.select`` again."""

from __future__ import annotations

import ast
import contextlib
import os
import resource
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

import orch
from orch import fdwait, gitbrowse

SRC = Path(orch.__file__).parent
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


def test_no_select_select_in_the_product_code():
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
    assert not hits, f"select.select breaks at fd 1024; wait through orch.fdwait: {hits}"


def test_the_waits_work_above_fd_setsize():
    with high_fds():
        a, b = socket.socketpair()
        r, w = os.pipe()
        try:
            assert a.fileno() > 1024 and r > 1024
            assert not fdwait.readable(a, 0)
            assert fdwait.writable(a, 1)
            b.send(b"x")
            os.write(w, b"y")
            assert fdwait.readable(a, 1)
            assert fdwait.ready([r, a], 1) == [r, a]
        finally:
            a.close(), b.close(), os.close(r), os.close(w)


def test_the_git_read_path_works_above_fd_setsize():
    """``gitbrowse.spawn`` (every git read the browser does) waits on the child's pipe, numbered above 1024 here."""
    with high_fds():
        res = gitbrowse.spawn(["/bin/echo", "past 1024"], {"PATH": "/usr/bin:/bin"}, timeout=10)
    assert res.out == b"past 1024\n" and not res.cut

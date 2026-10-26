"""No wait on an fd breaks at fd 1024 (CI incident 21 Oct: ``ValueError: filedescriptor out of range in select()``).

``select.select`` refuses an fd at or above ``FD_SETSIZE``. Production code waits through :mod:`orch.fdwait`
(``selectors``: epoll, kqueue or poll), and nothing in ``src`` may call ``select.select`` again."""

from __future__ import annotations

import ast
import contextlib
import os
import pty
import resource
import socket
import time
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
        if p == SRC / "fdwait.py":  # the one place: the fallback for fds kqueue cannot watch (/dev/tty on macOS)
            continue
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


def test_a_controlling_tty_can_be_waited_on():
    """kqueue (macOS) refuses ``/dev/tty`` with EINVAL; the wait must fall back instead of raising."""
    pid, master = pty.fork()
    if pid == 0:  # child: the pty is now its controlling terminal
        code = 90
        try:
            with open("/dev/tty", "rb", buffering=0) as tty:
                if fdwait.readable(tty, 0.2):
                    code = 91
                else:
                    os.write(1, b"ready\n")
                    code = 92 if not fdwait.readable(tty, 10) else 0
        except BaseException:
            code = 93
        os._exit(code)
    reaped = False
    try:
        deadline = time.monotonic() + 10
        buf = b""
        while b"ready" not in buf and time.monotonic() < deadline:
            if fdwait.readable(master, 0.2):
                try:
                    chunk = os.read(master, 1024)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
        os.write(master, b"hello\n")
        status = None
        while time.monotonic() < deadline + 10:
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
                break
            if fdwait.readable(master, 0.05):  # keep draining: macOS blocks a child's exit on unread tty output
                try:
                    os.read(master, 1024)
                except OSError:
                    pass
        reaped = status is not None
        if status is None:
            pytest.fail("the child did not finish")
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, f"child exit status {status}"
    finally:
        os.close(master)  # macOS: close before waiting, a child blocks in exit while its pty master is open
        if not reaped:  # the parent raised before waitpid: do not leave the child behind
            with contextlib.suppress(OSError):
                os.kill(pid, 9)
                os.waitpid(pid, 0)


def test_a_tty_above_fd_setsize_is_never_reported_ready_at_once():
    """macOS ``poll()`` answers POLLNVAL for /dev/tty at once; that must not count as readable."""
    pid, master = pty.fork()
    if pid == 0:
        code = 90
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            if hard != resource.RLIM_INFINITY and hard < HIGH + 64:
                os._exit(77)  # cannot get an fd this high
            if soft < HIGH + 64:
                resource.setrlimit(resource.RLIMIT_NOFILE, (HIGH + 64, hard))
            tty = os.open("/dev/tty", os.O_RDONLY)
            fd = os.dup2(tty, HIGH + 1)
            try:
                code = 91 if fdwait.readable(fd, 0.2) else 0  # True here is the bug
            except OSError:
                code = 0  # refusing is fine, claiming ready is not
        except BaseException:
            code = 93
        os._exit(code)
    reaped = False
    try:
        deadline = time.monotonic() + 15
        status = None
        while time.monotonic() < deadline:
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status, reaped = st, True
                break
            if fdwait.readable(master, 0.05):
                try:
                    os.read(master, 1024)
                except OSError:
                    pass
        assert status is not None, "the child did not finish"
        assert os.WIFEXITED(status)
        if os.WEXITSTATUS(status) == 77:
            pytest.skip("the fd limit is too low for an fd above 1024")
        assert os.WEXITSTATUS(status) == 0, f"child exit status {status}"
    finally:
        os.close(master)  # macOS: close before waiting, a child blocks in exit while its pty master is open
        if not reaped:
            with contextlib.suppress(OSError):
                os.kill(pid, 9)
                os.waitpid(pid, 0)

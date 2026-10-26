"""B11 (owner run 2): the R3 terminal confirmation on a REAL terminal, not a recording fake.

A child process gets a pseudo-terminal as its controlling terminal (``pty.fork``), runs the production
``Creator._tty_confirm`` with the production ``workspace_init.TERMINAL`` in a background thread (as ``orch setup`` does:
the run thread asks while the main thread waits), and the test reads what actually reaches the terminal and types back.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from orch import fdwait

pty = pytest.importorskip("pty")

CHILD = r"""
import sys, threading, types
from orch.ops import workspace_init
from orch.setup.steps import Creator
from orch.setup.protocol import SetupError

plan = types.SimpleNamespace(tty_confirm=True, fingerprint=["stick", "napkin", "dress"])
me = types.SimpleNamespace(plan=plan, run=types.SimpleNamespace(waiting_tty=False),
                           ports=types.SimpleNamespace(terminal=workspace_init.TERMINAL))
out = {}

def work():
    try:
        Creator._tty_confirm(me)
        out["code"] = 0
    except SetupError as e:
        out["code"] = 10 if e.code == "tty.declined" else 11

t = threading.Thread(target=work)
t.start()
t.join(20)
sys.exit(out.get("code", 12))
"""


def _run(answer: bytes, early: bytes = b"") -> tuple[int, str]:
    src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    pid, fd = pty.fork()
    if pid == 0:  # the child: its controlling terminal is the pty
        try:
            env = {**os.environ, "PYTHONPATH": os.path.abspath(src)}
            os.execve(sys.executable, [sys.executable, "-c", CHILD], env)
        finally:
            os._exit(13)
    if early:  # typed before any prompt exists (type-ahead, a paste): must never count as the answer
        os.write(fd, early)
    seen = b""
    deadline = time.monotonic() + 20
    sent = False
    while time.monotonic() < deadline:
        if fdwait.readable(fd, 0.2):
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
        if not sent and b"type yes: " in seen:
            if early:  # the early line was discarded: the child still waits for an answer
                time.sleep(1.0)
                assert os.waitpid(pid, os.WNOHANG)[0] == 0, f"type-ahead was taken as the answer: {seen!r}"
            os.write(fd, answer)
            sent = True
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
    else:
        os.kill(pid, 9)
        os.close(fd)  # macOS: a killed child blocks in exit until the pty master is closed
        os.waitpid(pid, 0)
        raise AssertionError(f"no confirmation prompt on the terminal: {seen!r}")
    if not done:
        _, status = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(status), seen.decode("utf-8", "replace")


def test_the_terminal_shows_the_plain_block_and_yes_confirms():
    code, seen = _run(b"yes\n")
    assert "orch setup is asking you to confirm." in seen
    assert "stick napkin dress" in seen
    assert "If they match, type yes: " in seen
    assert code == 0


def test_anything_else_on_the_terminal_declines():
    code, seen = _run(b"GRANT\n")  # the page's word is not the terminal's
    assert "stick napkin dress" in seen
    assert code == 10


def test_input_typed_before_the_prompt_is_discarded_never_an_answer():
    """Opus review MEDIUM: a "yes" queued before the prompt (type-ahead, a pasted line) is flushed; only a line typed
    after the prompt answers it. Here the later line is "no", so the run is declined."""
    code, seen = _run(b"no\n", early=b"yes\n")
    assert "If they match, type yes: " in seen
    assert code == 10

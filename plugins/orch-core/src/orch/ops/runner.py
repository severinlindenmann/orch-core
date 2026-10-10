"""Running a task's verify command for ``task done --run`` (F1 5.4.1, 6, 10.3), in one place so it can be audited.

What the receipt is: "this command exited 0 in the agent's environment, in this working copy, at this commit". It is
**attested by the agent's environment**, not independent verification: the agent controls ``PATH`` (a fake ``pytest``
exits 0), may choose among the linked repositories by its working directory, and a ``verify.cmd`` such as
``sh -c '...'`` runs a shell (orch adds none). The control is that ``verify.cmd`` sits in the plan a person approves,
and that the host runs it itself from P2. What orch does guarantee:

* the command is the ticket's ``verify.cmd`` split into arguments (``shlex``), never anything the agent passes;
* it runs in its own process group with a hard timeout (the group is killed; a process that leaves it with ``setsid``
  survives, as without a separate user or job object it must), stdin closed, output kept up to a bound and the rest
  dropped (never held in memory);
* the environment is an **allow-list**: ``PATH``, ``HOME``, ``LANG``, ``LC_*``, ``TMPDIR``, ``TERM`` and the names a
  caller adds (the variables the ticket's skills declare, D56); nothing else, so no grant, token, ``ORCH_*`` or
  ``GIT_*`` variable reaches it;
* git is asked with a scrubbed environment (``orch.store.observe``) for the commit and the state of the tree before and
  after: a repository whose HEAD moved is refused, and a **dirty tree** (before or after) gives ``commit: null`` (a
  receipt for code that is not committed is never evidence, F1 section 6) and the output says so.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from orch.canon.text import clean_line
from orch.cli import render
from orch.ops.errors import OrchError
from orch.store import observe

__all__ = ["OUTPUT_LIMIT", "RUN_TIMEOUT", "TRUNCATED", "Ran", "allowed_env", "run_verify"]

RUN_TIMEOUT = 3600  # seconds a verify command may take
OUTPUT_LIMIT = 1 << 20  # bytes of output kept as the receipt log; the rest is read and dropped
TRUNCATED = b"\n[output truncated by orch]\n"
ENV_ALLOW = ("PATH", "HOME", "LANG", "TMPDIR", "TERM")


class Ran:
    """The result of a verify run: the receipt, the output (the receipt log) and what to tell the agent."""

    def __init__(self, receipt: dict[str, Any], output: bytes, notes: list[str]) -> None:
        self.receipt, self.output, self.notes = receipt, output, notes


def allowed_env(env: Mapping[str, str], extra: Iterable[str] = ()) -> dict[str, str]:
    """The allow-listed part of ``env`` for a verify command."""
    names = {*ENV_ALLOW, *extra}
    return {k: v for k, v in env.items() if k in names or k.startswith("LC_")}


def _pump(stream: Any, limit: int, sink: bytearray) -> None:
    over = False
    while chunk := stream.read(65536):
        room = limit - len(sink)
        if room > 0:
            sink += chunk[:room]
        if len(chunk) > max(room, 0):
            over = True
    if over:
        sink += TRUNCATED


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def run_verify(
    cmd: str, *, repo: str | None, path: Path | None, cwd: Path, env: Mapping[str, str], grant_secret: str = ""
) -> Ran:
    """Run ``cmd`` (a ticket's ``verify.cmd``) and return the receipt. A non-zero exit, a timeout, a command that cannot
    start or a repository whose commit moved is ``verify.failed`` and nothing is recorded."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        raise OrchError("verify.failed", "the verify command cannot be split into arguments") from None
    if not argv:
        raise OrchError("verify.failed", "the verify command is empty")
    before = (observe.head(path), observe.porcelain(path)) if path is not None else (None, "")
    start = time.monotonic()
    sink = bytearray()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=allowed_env(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as e:
        raise OrchError("verify.failed", f"cannot run {clean_line(argv[0])[:60]}: {e.strerror}") from None
    reader = threading.Thread(target=_pump, args=(proc.stdout, OUTPUT_LIMIT, sink), daemon=True)
    reader.start()
    timed_out = False
    try:
        code = proc.wait(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out, code = True, -1
    finally:
        _kill_group(proc)  # the command, and what it left running in its group
        proc.wait()
    reader.join(5)
    ms = int((time.monotonic() - start) * 1000)
    out = render.redact(bytes(sink).decode("utf-8", "replace"), [grant_secret]).encode()
    if code != 0:
        tail = clean_line(out.decode("utf-8", "replace"))[-120:]
        why = f"timed out after {RUN_TIMEOUT} s" if timed_out else f"exit {code}"
        raise OrchError("verify.failed", f"{clean_line(cmd)[:60]}: {why}: {tail}")
    notes: list[str] = []
    commit = None
    if path is not None:
        after = (observe.head(path), observe.porcelain(path))
        if after[0] != before[0]:
            raise OrchError("verify.failed", "the repository's commit changed while the command ran; run it again")
        if before[1] or after[1] or before[1] is None or after[1] is None:
            notes.append("the working tree has uncommitted changes: the receipt names no commit and is not evidence")
        else:
            commit = after[0]
    return Ran({"cmd": cmd, "exit": 0, "ms": ms, "repo": repo, "commit": commit}, out, notes)

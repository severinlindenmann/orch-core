"""Verification receipts: orch runs a task's verify line, or a check the workspace config names, itself and keeps
what happened — each step's command, exit code and time, the commit and whether the tree had uncommitted changes, and
the tail of the output — so "the tests pass" is a record, not a sentence. Written only by `orch task done --run`
(ops_tasks.task_done_run); the artifact kind `receipt` is reserved for it.

What a run has to do differs per project, so the steps come from `checks.<name>.steps` in the workspace config
(load.check_steps); a plain verify line is one step named `verify`."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from orch.clock import stamp_s

CUT = b"[... earlier output cut ...]\n"


@dataclass(frozen=True)
class Receipt:
    exit: int | None  # the first failing step's exit code; 0 when every step passed; None when one timed out
    timed_out: bool
    commit: str | None
    dirty: bool
    at: str
    seconds: int
    steps: list[dict] = field(default_factory=list)  # {name, run, status: pass|fail|skip, exit, timed_out, seconds}
    log: bytes = b""

    @property
    def ok(self) -> bool:
        return self.exit == 0 and not self.timed_out

    def record(self) -> dict:
        """What the artifact entry keeps: everything but the output, which is the receipt file itself."""
        return {"exit": self.exit, "timed_out": self.timed_out, "commit": self.commit, "dirty": self.dirty,
                "at": self.at, "seconds": self.seconds, "steps": [dict(s) for s in self.steps]}


def _git(cwd: Path, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _run_one(cmd: str, cwd: Path, out, timeout: float) -> tuple[int | None, bool]:
    # the verify line is the agent's own text, or the project's configured check: run through the shell, in the
    # caller's checkout, as the caller would run it
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        return proc.wait(timeout=max(timeout, 0.01)), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the whole group: `npm run x` starts children of its own
        except ProcessLookupError:
            pass
        proc.wait()
        return None, True


def run_steps(steps: list[dict], cwd: Path, *, timeout: int, max_bytes: int, keep_going: bool = False) -> Receipt:
    """Run `steps` ({name, run}) in order in `cwd`. `timeout` (seconds) covers the whole run. After a failure the
    remaining steps are skipped unless `keep_going`; after a timeout they are always skipped."""
    commit = _git(cwd, "rev-parse", "HEAD")
    dirty = bool(commit and _git(cwd, "status", "--porcelain", "--untracked-files=no"))
    at, start = stamp_s(), time.monotonic()
    deadline = start + timeout
    done: list[dict] = []
    exit_code: int | None = 0
    timed_out = stop = False
    with tempfile.TemporaryFile() as out:
        for step in steps:
            name, cmd = str(step["name"]), str(step["run"])
            if stop:
                done.append({"name": name, "run": cmd, "status": "skip", "exit": None, "timed_out": False,
                             "seconds": 0})
                continue
            out.write(f"$ {cmd}\n".encode())
            out.flush()
            t0 = time.monotonic()
            code, late = _run_one(cmd, cwd, out, deadline - t0)
            secs = int(time.monotonic() - t0)
            out.write(f"[{name}: {'timed out' if late else f'exit {code}'} after {secs}s]\n".encode())
            ok = code == 0 and not late
            done.append({"name": name, "run": cmd, "status": "pass" if ok else "fail", "exit": code,
                         "timed_out": late, "seconds": secs})
            if late:
                timed_out, exit_code, stop = True, None, True
            elif not ok:
                if exit_code == 0:
                    exit_code = code
                stop = not keep_going
        size = out.tell()
        out.seek(max(0, size - max_bytes))
        log = out.read()
    if size > max_bytes:
        log = CUT + log[len(CUT):]
    return Receipt(exit_code, timed_out, commit, dirty, at, int(time.monotonic() - start), done, log)

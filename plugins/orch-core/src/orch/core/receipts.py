"""Verification receipts: orch runs a task's verify line, or a check the workspace config names, itself and keeps
what happened — each step's command, exit code and time, the commit and whether the tree had uncommitted changes, and
the tail of the output — so "the tests pass" is a record, not a sentence. Written only by `orch task done --run`
(ops_tasks.task_done_run); the artifact kind `receipt` is reserved for it.

What a run has to do differs per project, so the steps come from `checks.<name>.steps` in the workspace config
(load.check_steps); a plain verify line is one step named `verify`."""
from __future__ import annotations

import json
import os
import signal
import select
import subprocess
import tempfile
import threading
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
    repo: str | None
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
                "repo": self.repo,
                "at": self.at, "seconds": self.seconds, "steps": [dict(s) for s in self.steps]}


def _git(cwd: Path, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


class _Sink:
    """The run's output on disk, bounded: a temp file that never holds more than `cap` bytes. When it would, the
    oldest output is dropped and only the newest `keep` bytes stay, so a command that prints for an hour cannot fill
    the disk; `dropped` says output was cut (the receipt then starts with CUT)."""

    def __init__(self, keep: int, cap: int | None = None):
        self.keep = max(int(keep), 1)
        self.cap = max(int(cap or 0), 2 * self.keep)
        self.file = tempfile.TemporaryFile()
        self.dropped = False
        self.lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self.lock:
            self.file.seek(0, 2)
            self.file.write(data)
            if self.file.tell() > self.cap:
                self._rotate()

    def _rotate(self) -> None:
        size = self.file.tell()
        self.file.seek(size - self.keep)
        tail = self.file.read()
        self.file.seek(0)
        self.file.truncate()
        self.file.write(tail)
        self.dropped = True

    def tail(self) -> bytes:
        with self.lock:
            size = self.file.seek(0, 2)
            self.file.seek(max(0, size - self.keep))
            log = self.file.read()
            if self.dropped or size > self.keep:
                log = CUT + log[len(CUT):]
            return log

    def size(self) -> int:
        with self.lock:
            return self.file.seek(0, 2)

    def close(self) -> None:
        self.file.close()


def _pump(fd: int, sink: _Sink, stop: threading.Event) -> None:
    """Copy the command's output into the sink until the pipe closes, or `stop` is set and nothing is waiting."""
    while True:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            data = os.read(fd, 65536)
            if not data:
                return
            sink.write(data)
        elif stop.is_set():
            return


def _run_one(cmd: str, cwd: Path, out: _Sink, timeout: float) -> tuple[int | None, bool]:
    # the verify line is the agent's own text, or the project's configured check: run through the shell, in the
    # caller's checkout, as the caller would run it
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    stop = threading.Event()
    pump = threading.Thread(target=_pump, args=(proc.stdout.fileno(), out, stop), daemon=True)
    pump.start()
    try:
        return proc.wait(timeout=max(timeout, 0.01)), False
    except subprocess.TimeoutExpired:
        _kill(proc)
        return None, True
    except BaseException:  # Ctrl-C, or SIGTERM from the harness (see run_steps): never leave the command behind
        _kill(proc)
        raise
    finally:
        stop.set()
        pump.join(timeout=5)  # what the command wrote is in the sink; a child that kept the pipe open is let go
        proc.stdout.close()


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the whole group: `npm run x` starts children of its own
    except ProcessLookupError:
        pass
    proc.wait()


class Terminated(BaseException):
    """SIGTERM while a run is in progress (a harness's own timeout): unwinds so the step's group is killed."""


def _on_term(signum, frame):
    raise Terminated()


def run_steps(steps: list[dict], cwd: Path, *, timeout: int, max_bytes: int, keep_going: bool = False) -> Receipt:
    """Run `steps` ({name, run}) in order in `cwd`. `timeout` (seconds) covers the whole run. After a failure the
    remaining steps are skipped unless `keep_going`; after a timeout they are always skipped."""
    commit = _git(cwd, "rev-parse", "HEAD")
    dirty = bool(commit and _git(cwd, "status", "--porcelain", "--untracked-files=no"))
    top = _git(cwd, "rev-parse", "--show-toplevel")
    repo = Path(top).name if top else None  # which checkout it ran in, by name: never a local path
    at, start = stamp_s(), time.monotonic()
    deadline = start + timeout
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGTERM, _on_term)
    try:
        return _run(steps, cwd, deadline, keep_going, max_bytes, commit, dirty, repo, at, start)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _run(steps, cwd, deadline, keep_going, max_bytes, commit, dirty, repo, at, start) -> Receipt:
    done: list[dict] = []
    exit_code: int | None = 0
    timed_out = stop = False
    out = _Sink(max_bytes)
    try:
        for step in steps:
            name, cmd = str(step["name"]), str(step["run"])
            if stop:
                done.append({"name": name, "run": cmd, "status": "skip", "exit": None, "timed_out": False,
                             "seconds": 0})
                continue
            out.write(f"$ {cmd}\n".encode())
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
        log = out.tail()
    finally:
        out.close()
    return Receipt(exit_code, timed_out, commit, dirty, repo, at, int(time.monotonic() - start), done, log)


# -- the receipt as a widget ---------------------------------------------------------------------------------------

_STATUS = {"pass": "pass", "fail": "fail", "skip": "skip"}


_UNSIGNED = {"unsigned": "check not signed by the human", "changed": "check changed since the human signed it"}


def gates_block(receipt: Receipt, task_id: str, check: str | None, artifact: str,
                check_state: str | None = None) -> dict:
    """A core `gates` widget (docs/widgets.md) of one run: a row per step, its status and time. The id is per task,
    so the next run of the same task replaces the block instead of adding one."""
    source = f"artifact:{artifact}" + (f" · {receipt.commit[:7]}" if receipt.commit else "") \
        + (" · uncommitted changes" if receipt.dirty else "") \
        + (f" · {_UNSIGNED[check_state]}" if check_state in _UNSIGNED else "")
    items = [{"name": s["name"], "status": _STATUS[s["status"]],
              **({"seconds": s["seconds"]} if s["status"] != "skip" else {})} for s in receipt.steps]
    return {"type": "gates", "id": f"receipt-{task_id.lower()}", "title": f"{task_id} {check or 'verify'}",
            "source": source, "items": items}


def put_block(section_text: str, block: dict) -> str:
    """`section_text` with the ```orch block whose id is block["id"] replaced by `block`, or `block` appended."""
    from orch.widgets.blocks import parse_blocks
    fence = f"```orch\n{json.dumps(block, ensure_ascii=False)}\n```"  # one line of JSON: no fence inside it
    lines = (section_text or "").split("\n")
    for b in parse_blocks(section_text or ""):
        if isinstance(b.data, dict) and b.data.get("id") == block["id"]:
            start = b.line - 1
            end = start + len(b.raw.split("\n")) + 2  # opening fence, body, closing fence
            return "\n".join(lines[:start] + fence.split("\n") + lines[end:])
    return f"{section_text}\n\n{fence}" if (section_text or "").strip() else fence

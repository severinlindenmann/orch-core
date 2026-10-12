"""Runner test helpers. Every process a test here signals is one the test started (kill only own PIDs)."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from orch.runner import procs
from orch.runner.supervise import SpawnSpec

_COUNTER = [0]
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def run_id() -> str:
    _COUNTER[0] += 1
    n = _COUNTER[0] * 7919 + os.getpid()
    tail = ""
    for _ in range(25):
        tail = ALPHABET[n % 32] + tail
        n //= 32
    return "r_0" + tail


def pin(path: Path) -> dict:
    st = os.stat(path)
    return {"path": str(path), "dev": st.st_dev, "ino": st.st_ino}


def spec(cwd: Path, argv: list[str], *, run: str | None = None, env: dict | None = None, **kw) -> SpawnSpec:
    return SpawnSpec(
        run or run_id(), "0" * 32, kw.pop("kind", "shell"), argv, env if env is not None else {}, pin(cwd), **kw
    )


def py(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


def wait_for(cond, timeout: float = 10.0, step: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not met in time")


def read_pid(path: Path) -> tuple[int, int]:
    pid = int(wait_for(lambda: path.exists() and path.read_text().strip() and path.read_text().strip()))
    found = wait_for(lambda: procs.info(pid))
    return pid, found.birth


def kill_own(pid: int, birth: int) -> None:
    """Clean up a process this test started (verified by birth identity)."""
    procs.signal_verified(pid, birth, signal.SIGKILL)

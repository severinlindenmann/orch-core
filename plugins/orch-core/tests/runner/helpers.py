"""Runner test helpers. Every process a test here signals is one the test started (kill only own PIDs)."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from orch.runner import procs
from orch.runner.supervise import SpawnSpec

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def run_id() -> str:
    """A fresh run id (``r_`` + a time-based ULID, as the host draws them)."""
    import secrets

    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    tail = ""
    for _ in range(26):
        tail = ALPHABET[n & 31] + tail
        n >>= 5
    return "r_" + tail


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

from __future__ import annotations

from contextlib import contextmanager

from filelock import FileLock, Timeout

from orch.errors import LockBusyError


@contextmanager
def lock(ws, name: str, timeout: float = 5.0):
    directory = ws.state_dir / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    fl = FileLock(str(directory / f"{name}.lock"), timeout=timeout)
    try:
        fl.acquire()
    except Timeout as e:
        raise LockBusyError(f"{name} is locked by another process", hint="retry in a moment") from e
    try:
        yield
    finally:
        fl.release()

"""One remote client per OS user (R5 design section 1).

``client.lock`` (an advisory lock held for the process's life) decides who the client is; ``client.pid`` (0600, in the
client's private state directory) names it. A second ``orch remote open`` does not start another client: it leaves a
``handover`` file, and the running client, which looks for it every half second, opens its home page in the person's
browser (a new launch code, through the private redirect file) and removes the file. Everything is inside the 0700
state directory, so it is the same trust as the client's keys."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from orch import oslock

__all__ = ["SingleInstance"]

HANDOVER_WAIT_S = 5.0


class SingleInstance:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)
        self.lock_path, self.pid_path, self.handover_path = (
            self.root / "client.lock",
            self.root / "client.pid",
            self.root / "handover",
        )
        self._fd: int | None = None

    def acquire(self) -> bool:
        """True when this process is now the client; False when another one holds the lock."""
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            oslock.flock(fd, oslock.LOCK_EX | oslock.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            return False
        self._fd = fd
        self._write(self.pid_path, str(os.getpid()).encode())
        self.handover_path.unlink(missing_ok=True)  # a request from before this client started is stale
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        self.pid_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            oslock.flock(self._fd, oslock.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    def running_pid(self) -> int | None:
        try:
            return int(self.pid_path.read_text())
        except (OSError, ValueError):
            return None

    def request_handover(self, wait_s: float = HANDOVER_WAIT_S, sleep: float = 0.1) -> bool:
        """Ask the running client to open its page; True once it did (the file is gone)."""
        self._write(self.handover_path, b"1")
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if not self.handover_path.exists():
                return True
            time.sleep(sleep)
        return False

    def take_handover(self) -> bool:
        """The client's side: was a handover requested? Consumes the request."""
        try:
            self.handover_path.unlink()
            return True
        except FileNotFoundError:
            return False

    def _write(self, path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

"""The setup flow: phases, the routes of the contract and the orchestration (filled in step by step)."""

from __future__ import annotations

import threading
from typing import Any

from .protocol import SetupError

__all__ = ["SetupFlow"]


class SetupFlow:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.phase = "profile"
        self.mode = "new"
        self.dir = ""
        self._running = False
        self._ended: str | None = None

    @property
    def busy(self) -> bool:
        return self._running

    @property
    def finished(self) -> bool:
        return self.phase in ("done", "cancelled")

    def launch(self, directory: str) -> None:
        self.dir = directory

    def end(self, why: str) -> None:
        with self.lock:
            if self.phase not in ("done", "cancelled"):
                self.phase = "cancelled"
            self._ended = why

    def dispatch(self, method: str, route: str, query: dict[str, str], body: Any) -> tuple[int, dict[str, Any]]:
        with self.lock:
            if (method, route) == ("GET", "state"):
                return 200, {"phase": self.phase, "mode": self.mode, "dir": self.dir}
            if (method, route) == ("POST", "cancel"):
                self.phase = "cancelled"
                return 200, {"ok": True}
        raise SetupError("setup.not_found", "no such route", 404)

"""``runner.restart``: the host asks the runner to restart itself on the new release (H1 L3, spec 13.2 item 7).

In tier B the runner is a system unit the person cannot restart (no root), so a release update reaches it through the
host: the host sends the ``restart`` command, the runner ends when it has no live runs (agents, terminals) and
``Restart=always`` starts it again from ``<release>/current``, which is the new code. With ``force`` it ends at once and
its runs are stopped like any controlled stop (D-P2-39); the person chose that. Once armed, the link refuses every new
spawn (``runner.unavailable``, "restarting on a new release"). The gate only ever sets the runner's own
stop event: it carries no authority beyond what the host socket's peer check already gave the command.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

__all__ = ["RestartGate"]


class RestartGate:
    def __init__(
        self,
        live: Callable[[], int],
        stop: threading.Event,
        *,
        lock: Any = None,
        poll: float = 0.5,
        delay: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._live, self._stop, self._poll, self._delay, self._sleep = live, stop, poll, delay, sleep
        #: the supervisor's spawn lock: the "no live runs" decision and a spawn's admission are one step, so a spawn is
        #: either refused (the link checks :attr:`armed` inside the same lock) or counted, never lost to the restart
        self._spawn_lock = lock if lock is not None else threading.Lock()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._force = False

    @property
    def armed(self) -> bool:
        return self._thread is not None

    def request(self, force: bool = False) -> dict[str, Any]:
        """Arm the restart. Returns at once: ``{"restarting": True, "live": n, "force": bool}``. A second request only
        upgrades to ``force``; the runner ends after ``delay`` seconds, so this answer reaches the host first."""
        with self._lock:
            self._force = self._force or bool(force)
            if self._thread is None:
                self._thread = threading.Thread(target=self._wait, name="runner-restart", daemon=True)
                self._thread.start()
            return {"restarting": True, "live": self._live(), "force": self._force}

    def _wait(self) -> None:
        while not self._stop.is_set():
            with self._spawn_lock:
                ready = self._force or self._live() == 0
            if ready:
                self._sleep(self._delay)
                self._stop.set()
                return
            self._sleep(self._poll)

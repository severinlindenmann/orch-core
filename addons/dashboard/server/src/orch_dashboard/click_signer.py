"""Click-to-sign (H5c, orch-v2-host section 0.8.6 item 5): the one signer child of this server, and nothing else.

**The key never comes here.** The signer is core's own child process (`python -I -m orch.click`): it holds the device
key, talks to the host itself and builds the View. This module only starts it, restarts it when it exits, speaks its
frames (through `orch.click.client.ClickClient`) and turns its refusals into the server's errors. It never builds a
preview of its own, never logs a View or a credential, and is not reachable from any addon path (only the typed
routes of `click.py` call it).

Trust (binding): the dashboard, its HTTP process and its browser, is trusted with signing authority; the signer keeps
the key out of this process against passive disclosure. The card and every sign panel say so.
"""

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .errors import HostError, fail

CONTRACT = 1
#: `ORCH_DASHBOARD_CLICK=off` never starts a signer (a kill switch; the tests use it unless they test the signer).
SWITCH = "ORCH_DASHBOARD_CLICK"
RESTART_AFTER = 1.0  # seconds: a signer that dies at once is not respawned in a tight loop
CONTRACT_RETRY = 30.0
TIMEOUT = {"status": 8.0, "prepare": 30.0, "view": 30.0, "sign": 60.0, "cancel": 30.0}


def child_env(state: Path) -> dict[str, str]:
    """The signer's environment: ours, with the state directory made explicit (it must be the registry we read) and
    no PYTHONPATH (`-I` ignores it, and a path to another core is never part of a running server)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["ORCH_STATE_DIR"] = str(state)
    return env


def unavailable(why: str = "The signing helper is not running. Sign in the terminal instead.") -> HostError:
    return fail("click.unavailable", why, exit=9)


class Signer:
    """One signer child per dashboard server, started on first use, restarted if it exits, stopped on shutdown.

    `start` is a seam for tests (default `orch.click.spawn.start`); everything it returns must have `call(op, args,
    timeout=)`, `close()` and an optional `proc` with `poll()`."""

    def __init__(self, state: Path, start: Callable | None = None, clock: Callable[[], float] = time.monotonic):
        self.state = Path(state)
        self._start, self.clock = start, clock
        self.lock = threading.RLock()
        self.client = None
        self.core: str | None = None
        self.last_spawn = -1e9
        self.refused: tuple[float, HostError] | None = None  # a signer of another contract: until this time
        self.restarts = 0
        self.stopped = False

    # -- lifecycle -------------------------------------------------------------------------------------------------
    @property
    def off(self) -> bool:
        return self.stopped or os.environ.get(SWITCH, "").lower() == "off"

    def _spawn(self):
        if self._start is not None:
            return self._start(child_env(self.state))
        from orch.click import spawn

        return spawn.start(child_env(self.state))

    def _alive(self) -> bool:
        proc = getattr(self.client, "proc", None)
        return self.client is not None and (proc is None or proc.poll() is None)

    def _drop(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - a dead child must not stop a restart
                pass

    def _ensure(self):
        """The running signer, started (or restarted) if needed, and checked: its status must say `contract: 1`."""
        if self.off:
            raise unavailable("Click-to-sign is switched off for this server.")
        if self.refused and self.clock() < self.refused[0]:
            raise self.refused[1]
        if self._alive():
            return self.client
        if self.client is not None:
            self._drop()
            self.restarts += 1  # it exited: its open approvals are gone (terminal-only, which is safe)
        if self.clock() - self.last_spawn < RESTART_AFTER:
            raise unavailable()
        self.last_spawn = self.clock()
        try:
            client = self._spawn()
        except Exception:  # noqa: BLE001 - not a traceback: the person is told to use the terminal
            raise unavailable() from None
        try:
            status = client.call("status", {}, timeout=TIMEOUT["status"])
        except Exception:  # noqa: BLE001
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            raise unavailable() from None
        if status.get("contract") != CONTRACT:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            error = fail("click.contract", "Update orch-core: its signing helper speaks another contract.", exit=9)
            self.refused = (self.clock() + CONTRACT_RETRY, error)
            raise error
        self.refused = None
        self.core = str(status.get("core"))[:40]
        self.client = client
        return client

    def close(self) -> None:
        with self.lock:
            self.stopped = True
            self._drop()

    # -- calls -----------------------------------------------------------------------------------------------------
    def call(self, op: str, args: dict) -> dict:
        """One request. A refusal of the signer becomes a `HostError` with its code, reason and wait; a lost
        channel drops the child (the next call starts a new one) and says so."""
        from orch.click.protocol import ClickError

        with self.lock:
            client = self._ensure()
            try:
                return client.call(op, args, timeout=TIMEOUT[op])
            except ClickError as exc:
                raise refusal(exc) from None
            except Exception:  # noqa: BLE001 - channel loss, a frame error, a timeout: the signer is not trusted on
                self._drop()
                raise unavailable(
                    "The signing helper stopped. Open sign requests are gone; sign in the terminal."
                ) from None

    def status(self) -> dict[str, dict]:
        """Per workspace: custody, enabled, available, problem. Empty when no signer can run (never raises)."""
        if self.off:
            return {}
        try:
            got = self.call("status", {})
        except HostError:
            return {}
        return {w["workspace_id"]: w for w in got.get("workspaces", []) if isinstance(w, dict) and "workspace_id" in w}


def refusal(exc) -> HostError:
    """The signer's `ClickError` as the server's error: `code`, `reason` and `retry_after_ms` are kept (the UI
    branches on them); the message is the signer's display text."""
    error = {"code": exc.code, "message": exc.message, "retryable": bool(exc.retryable)}
    if exc.reason is not None:
        error["reason"] = exc.reason
    if exc.retry_after_ms is not None:
        error["retry_after_ms"] = int(exc.retry_after_ms)
    return HostError(error, exit=3)

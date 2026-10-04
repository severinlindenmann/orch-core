"""The setup checks behind the menu badge and the Workspace page (`orch doctor`, each repo's hook state, `orch check`),
computed off the request path.

They shell out to git (about 20 calls for four repos) and read every ticket, which took 200-750 ms. `orch serve`
recomputes them in a background thread every TTL seconds and pages read the last result. Without that loop (a test
client without lifespan, or before the first round finished) the first reader computes them once, and an expired
result is recomputed by its next reader, as before. A Workspace POST marks the result stale, so the page it redirects
to shows fresh checks.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

TTL = 60.0
RECHECK_AFTER = 5.0  # a Workspace visit to a result older than this asks the loop for a new round (shown next visit)


@dataclass(frozen=True)
class Snapshot:
    at: float  # time.monotonic() when the round started
    checks: list
    checks_error: str | None
    repos: list[dict]
    findings: list


@dataclass
class SetupState:
    ws: object
    snapshot: Snapshot | None = None
    _gen: int = 0  # bumped by invalidate(); a snapshot counts only when its round started at or after the bump
    _snap_gen: int = -1
    background: bool = False  # the lifespan loop runs: expired results are refreshed there, not by a reader
    _compute_lock: threading.Lock = field(default_factory=threading.Lock)
    _finished: float = float("-inf")
    _wake: asyncio.Event | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    def compute(self) -> Snapshot:
        """One round, at most one at a time; a caller that waited for a running round gets its result."""
        called = time.monotonic()
        with self._compute_lock:
            snap = self.snapshot
            if snap is not None and self._finished >= called and not self.stale:
                return snap  # a round that started after the last invalidate() finished while we waited
            gen = self._gen
            snap = _compute(self.ws, time.monotonic())
            self.snapshot, self._snap_gen, self._finished = snap, gen, time.monotonic()
            return snap

    @property
    def stale(self) -> bool:
        """True from invalidate() until a round that STARTED after it has finished (a round already running when
        invalidate() was called does not count: it may have read the old state)."""
        return self._snap_gen < self._gen

    def get(self) -> Snapshot:
        snap = self.snapshot
        if snap is not None and not self.stale:
            if self.background or time.monotonic() - snap.at < TTL:
                return snap
        return self.compute()

    def peek(self) -> Snapshot | None:
        """`get()` for the menu badge: while the background loop's first round still runs, None (no badge yet)
        instead of waiting for it, so the first page after `orch serve` starts never waits on git."""
        if self.background and self.snapshot is None:
            return None
        return self.get()

    def refresh(self) -> Snapshot:
        """A new round now, whatever the last one was (the Workspace page without the background loop)."""
        self._gen += 1
        return self.compute()

    def invalidate(self) -> None:
        self._gen += 1
        self.refresh_soon()

    def refresh_soon(self) -> None:
        """Wake the background loop for a new round now (no-op without one); safe from any thread."""
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None and not loop.is_closed():
            loop.call_soon_threadsafe(wake.set)

    async def run_forever(self, seconds: float = TTL) -> None:
        from orch.addons.loader import _log_error

        self._loop, self._wake = asyncio.get_running_loop(), asyncio.Event()
        self.background = True
        try:
            while True:
                self._wake.clear()
                try:
                    await asyncio.to_thread(self.compute)
                except Exception:  # never let one bad round stop the loop; readers keep the last result
                    _log_error(self.ws, "dashboard", "setup checks")
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.background = False
            self._loop = self._wake = None


def _compute(ws, at: float) -> Snapshot:
    from orch.dashboard import routes_workspace

    try:
        repos = routes_workspace._repos(ws)
    except Exception:  # noqa: BLE001 - e.g. git missing; doctor reports that itself
        repos = []
    checks, error = routes_workspace._checks(ws, hook_states={r["path"]: r["hook"] for r in repos})
    try:
        findings = routes_workspace.run_checks(ws, emit_events=False)  # a timer never writes events; the Workspace page records them
    except Exception as e:  # noqa: BLE001 - shown on the page, never raised from the loop
        from orch.core.check import Finding
        findings = [Finding("error", "check-failed", None, f"could not run orch check: {e}")]
    return Snapshot(at, checks, error, repos, findings)


_STATES: dict[str, SetupState] = {}
_STATES_LOCK = threading.Lock()


def state(ws) -> SetupState:
    key = str(Path(ws.root).resolve())
    with _STATES_LOCK:
        found = _STATES.get(key)
        if found is None or found.ws is not ws:  # another Workspace object (e.g. reloaded config): start over
            found = _STATES[key] = SetupState(ws)
        return found


def invalidate(ws) -> None:
    key = str(Path(ws.root).resolve())
    found = _STATES.get(key)
    if found is not None:
        found.invalidate()


def open_count(snap: Snapshot, ws) -> int:
    """Open, non-dismissed setup items of `snap` (dismissals are read now, so a dismissal shows at once)."""
    from orch import onboarding

    dismissed = set(onboarding.load_state()["dismissed_items"].get(str(Path(ws.root).resolve()), []))
    return len([c for c in snap.checks if not c.ok and c.code in onboarding.OPEN_ITEM_CODES
                and c.code not in dismissed])

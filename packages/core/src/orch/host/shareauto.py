"""Automatic share passes for a SELECTIVE workspace (V3 C6 follow-up): a debounced background thread per workspace.

A commit to the workspace (any ticket change) marks the workspace dirty; after a quiet moment (:data:`DEBOUNCE_S`, at
most :data:`MAX_WAIT_S` under a steady stream of commits) the thread runs :func:`orch.host.shareops.auto_sync`. That
pass only re-sends or withdraws items of the selection the owner already confirmed (see
:meth:`orch.hubclient.shares.ShareState.auto_wanted`); it never widens it. It does nothing without a
bound hub (selective or full), or while the workspace is degraded. A hub that cannot be reached is retried with a growing delay
(:data:`RETRY_MIN_S` to :data:`RETRY_MAX_S`); a pass that cannot ever apply (full mode, no hub) parks the thread until
the owner's next share operation arms it again.

The thread is armed by the owner's share operations (the first save of ``hub/shares.json``) and, after a restart, at
workspace open when ``hub/shares.json`` exists. :func:`touch` is called from the store's commit hook, so it only sets a
flag and wakes the thread: no I/O, never raises."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import WorkspaceService

LOG = logging.getLogger("orch.host.shareauto")

DEBOUNCE_S = 3.0
MAX_WAIT_S = 30.0
RETRY_MIN_S = 30.0
RETRY_MAX_S = 300.0

# Time seam (tests only): production reads the monotonic clock and sleeps until the next deadline. A test installs a
# manual clock with ``_CLOCK`` and ``_TIMED = False``; the thread then waits without a timeout and re-checks its
# deadlines on every :func:`tick`, so a debounce or a retry is stepped by the test instead of waited for.
_CLOCK = time.monotonic
_TIMED = True

_MU = threading.Lock()
_AUTOS: dict[tuple[str, str], _Auto] = {}


def _key(service: WorkspaceService) -> tuple[str, str]:
    return (str(service.state_dir), str(service.workspace_id))


class _Auto:
    def __init__(self, service: WorkspaceService) -> None:
        self.service = service
        self.cv = threading.Condition()
        self.stopped = False
        self.parked = False  # the last pass could not apply at all (full mode, no hub): wait for arm()
        self.dirty_since: float | None = None
        self.last_touch = 0.0
        self.not_before = 0.0
        self.backoff = RETRY_MIN_S
        self.passes = 0  # finished passes (tests wait on it)
        #: commits seen (``mark``) and how many of them the last finished pass had seen when it started: a pass covers a
        #: commit only if it started after it (tests wait for ``covered >= marks``; counting passes raced a pass that
        #: finished before the test began to count, CI flake 29 Oct)
        self.marks = 0
        self.covered = 0
        self._taking = 0
        self.last: dict[str, Any] | str | None = None
        self.thread = threading.Thread(
            target=self._loop, name=f"orch-shareauto-{service.workspace_id[:8]}", daemon=True
        )

    def mark(self, now: float) -> None:
        with self.cv:
            if self.dirty_since is None:
                self.dirty_since = now
            self.last_touch = now
            self.marks += 1
            self.cv.notify_all()

    def _wait_due(self) -> bool:
        """Block until a pass is due; ``False``: stopped."""
        with self.cv:
            while not self.stopped and (self.dirty_since is None or self.parked):
                self.cv.wait()
            while not self.stopped:
                now = _CLOCK()
                due = max(min(self.last_touch + DEBOUNCE_S, self.dirty_since + MAX_WAIT_S), self.not_before)  # type: ignore[operator]
                if now >= due:
                    self.dirty_since = None
                    self._taking = self.marks
                    return True
                self.cv.wait(due - now if _TIMED else None)
            return False

    def _loop(self) -> None:
        from . import shareops

        while self._wait_due():
            svc = self.service
            result: dict[str, Any] | str | None
            try:
                result = shareops.auto_sync(svc)
            except Exception as exc:  # noqa: BLE001 - full mode, no hub, unreadable state: nothing applies; park
                code = getattr(exc, "code", type(exc).__name__)
                LOG.info("share auto pass of %s does not apply: %s", svc.workspace_id, code)
                result = f"parked:{code}"
                with self.cv:
                    self.parked = True
            else:
                if isinstance(result, dict) and result.get("synced") is False:  # hub unreachable or refused: retry
                    with self.cv:
                        self.dirty_since = _CLOCK()
                        self.not_before = self.dirty_since + self.backoff
                        self.backoff = min(self.backoff * 2, RETRY_MAX_S)
                elif isinstance(result, dict):
                    self.backoff = RETRY_MIN_S
                    LOG.info(
                        "share auto pass of %s: %s",
                        svc.workspace_id,
                        {k: v for k, v in result.items() if type(v) is int},
                    )
            with self.cv:
                self.last = result
                self.passes += 1
                self.covered = self._taking
                self.cv.notify_all()


def arm(service: WorkspaceService) -> None:
    """Start the thread for this workspace if it is not running (and unpark it). Never raises."""
    try:
        k = _key(service)
        with _MU:
            a = _AUTOS.get(k)
            if a is not None and a.service is not service:
                _drop(a)
                a = None
            if a is None:
                a = _AUTOS[k] = _Auto(service)
                a.thread.start()
        with a.cv:
            a.parked = False
            a.cv.notify_all()
    except Exception:  # noqa: BLE001 - an optimisation, never fails the owner's operation
        LOG.exception("share auto thread of %s could not start", getattr(service, "workspace_id", "?"))


def touch(service: WorkspaceService) -> None:
    """A commit happened (store hook: inside the store's critical section, so cheap and non-raising)."""
    try:
        a = _AUTOS.get(_key(service))
        if a is not None and a.service is service:
            a.mark(_CLOCK())
    except Exception:  # noqa: BLE001
        pass


def maybe_start(service: WorkspaceService) -> None:
    """At workspace open: arm when this workspace has share state. Never fails the open."""
    try:
        from . import hubops

        if (hubops._dir(service) / "shares.json").exists():  # noqa: SLF001
            arm(service)
    except Exception:  # noqa: BLE001
        return


def _drop(a: _Auto) -> None:
    with a.cv:
        a.stopped = True
        a.cv.notify_all()


def stop(service: WorkspaceService) -> None:
    with _MU:
        a = _AUTOS.get(_key(service))
        if a is None or a.service is not service:
            return
        del _AUTOS[_key(service)]
    _drop(a)
    if a.thread is not threading.current_thread():
        a.thread.join(10)


def tick() -> None:
    """For tests with a manual clock: wake every auto thread so it re-checks its deadlines."""
    with _MU:
        autos = list(_AUTOS.values())
    for a in autos:
        with a.cv:
            a.cv.notify_all()


def state_of(service: WorkspaceService) -> _Auto | None:
    """For tests: the workspace's auto state."""
    return _AUTOS.get(_key(service))

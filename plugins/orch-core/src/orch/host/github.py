"""Own GitHub display polling and warm starts; not wired into WorkspaceService yet.

Visibility-filtered host ops and link resolution belong to a later task. This service is
an internal snapshot owner, not an authorization boundary or a source of signed facts.
"""

from __future__ import annotations

import logging
import os
import random
import resource
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

from orch.github import cache, token
from orch.github.client import GitHubClient
from orch.github.poller import GitHubSettings, Poller, Snapshot

log = logging.getLogger(__name__)
_core_lock = threading.Lock()
_core_disabled = False


def _disable_core():
    global _core_disabled
    with _core_lock:
        if not _core_disabled:
            _core_disabled = True
            try:
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            except (OSError, ValueError):
                pass


@dataclass(frozen=True)
class RefreshResult:
    accepted: bool
    retry_after_s: int
    reason: str


class GitHubService:
    def __init__(
        self,
        state_dir,
        ws_id,
        host_uid=None,
        *,
        client_factory=GitHubClient,
        clock=time.time,
        rng=None,
        wait=None,
        priority=None,
    ):
        self.state_dir, self.ws_id = state_dir, ws_id
        self.host_uid = os.getuid() if host_uid is None else host_uid
        self.clock, self.rng = clock, rng or random.Random()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._wait = wait or self._wake.wait
        self._lock, self._poll_lock = threading.RLock(), threading.Lock()
        self._thread = None
        self._settings = None
        self._enabled = False
        self._generation = 0
        self._needs_clear = False
        self._closed = False
        self._viewers, self._refreshes = {}, deque()

        def source():
            value = token.load(self.state_dir, self.ws_id, self.host_uid)
            _disable_core()
            return value

        self.poller = Poller(
            token_source=source, client_factory=client_factory, clock=clock, rng=self.rng, priority=priority
        )
        loaded = cache.load(state_dir, ws_id, self.host_uid)
        self._snapshot = loaded.snapshot if loaded else Snapshot()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def _wipe(self):
        """Generation fencing prevents an old in-flight response from republishing erased data."""
        with self._lock:
            self._generation += 1
            self._snapshot = Snapshot()
            self._needs_clear = True
            if self._poll_lock.acquire(blocking=False):
                try:
                    self.poller.clear()
                    self._needs_clear = False
                finally:
                    self._poll_lock.release()
            cache.remove(self.state_dir, self.ws_id, self.host_uid)

    def reconcile(self, settings: GitHubSettings | None, addon_on: bool):
        with self._lock:
            if self._closed:
                return
            old = self._settings
            self._enabled = bool(addon_on and settings is not None)
            self._settings = settings
        if not self._enabled:
            self._stop_thread()
            self._wipe()
            return
        if old is not None and dict(old.repos) != dict(settings.repos):
            self._wipe()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop = threading.Event()
                self._thread = threading.Thread(target=self._loop, name=f"orch-github-{self.ws_id[:8]}", daemon=True)
                self._thread.start()
            self._wake.set()

    def run_once(self):
        """Tests can drive exactly the same serialized operation as the daemon."""
        with self._poll_lock:
            with self._lock:
                if not self._enabled or self._closed or self._settings is None:
                    return self._snapshot
                if self._needs_clear:
                    self.poller.clear()
                    self._needs_clear = False
                generation, settings = self._generation, self._settings
                fingerprint = self.poller.fingerprint
            result = self.poller.poll_once(settings)
            with self._lock:
                if generation != self._generation or not self._enabled or self._closed:
                    self.poller.clear()
                    self._needs_clear = False
                    return self._snapshot
                if result.state == "no_token" or (fingerprint is not None and fingerprint != self.poller.fingerprint):
                    cache.remove(self.state_dir, self.ws_id, self.host_uid)
                    self._snapshot = Snapshot()
                if result.state == "ok":
                    self._snapshot = result
                    try:
                        cache.save(self.state_dir, self.ws_id, self.host_uid, cache.document(result), result.as_of)
                    except Exception as exc:
                        log.warning("%s", type(exc).__name__)
                elif self._snapshot.verified or self._snapshot.state == "empty" or result.state == "no_token":
                    self._snapshot = result
                else:
                    # still the unverified warm start: keep its rows, but say what the poll found (paused, bad
                    # token, error) so the page never shows old data as if nothing were wrong
                    self._snapshot = replace(
                        self._snapshot,
                        state=result.state,
                        paused_until=result.paused_until,
                        last_error_kind=result.last_error_kind,
                        repo_status=result.repo_status,
                        request_count=result.request_count,
                    )
                return self._snapshot

    def refresh_now(self, viewer: str) -> RefreshResult:
        with self._lock:
            now = self.clock()
            if not self._enabled or self._closed:
                return RefreshResult(False, 0, "disabled")
            self._viewers = {key: when for key, when in self._viewers.items() if now - when < 20}
            while self._refreshes and now - self._refreshes[0] >= 3600:
                self._refreshes.popleft()
            if viewer in self._viewers:
                return RefreshResult(False, max(1, int(20 - (now - self._viewers[viewer]) + 0.999)), "viewer_limit")
            if len(self._refreshes) >= 12:
                return RefreshResult(False, max(1, int(3600 - (now - self._refreshes[0]) + 0.999)), "workspace_limit")
            self._viewers[viewer] = now
            self._refreshes.append(now)
            self._wake.set()
            return RefreshResult(True, 0, "accepted")

    def _loop(self):
        stop = self._stop
        while not stop.is_set():
            self._wake.clear()
            try:
                self.run_once()
            except Exception as exc:
                log.warning("%s", type(exc).__name__)
            with self._lock:
                interval = self._settings.poll_minutes * 60 if self._settings else 300
            delay = (
                max(1, self.poller.next_poll_at - self.clock())
                if self.poller.next_poll_at
                else interval * self.rng.uniform(0.9, 1.1)
            )
            if not stop.is_set():
                try:
                    self._wait(delay)
                except Exception as exc:
                    log.warning("%s", type(exc).__name__)
                    return

    def _stop_thread(self):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def purge(self):
        self.reconcile(None, False)
        token.remove(self.state_dir, self.ws_id)

    def close(self):
        with self._lock:
            self._closed = True
            self._enabled = False
            self._generation += 1
        self._stop_thread()
        self.poller.client.shutdown()

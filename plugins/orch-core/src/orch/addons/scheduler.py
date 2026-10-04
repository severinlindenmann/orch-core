"""Background provider fetches (v2 §11.10): per job single-flight, timeouts, exponential backoff, Retry-After,
refresh only while a live-update stream is connected or after a manual Refresh. Pages never call this.

An `always_on` provider also runs without an open tab, but only after the human switched on "Keep syncing while
Mission Control runs" for its addon in this workspace (workspaces.json `addons[name].background`). A `long_poll`
provider is due again right after a good fetch; its ctx.run is capped at 35 s and the whole fetch at 40 s."""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import traceback
from dataclasses import dataclass

from orch.addons import cache
from orch.addons.api import Snapshot
from orch.addons.loader import _log_error
from orch.clock import now as wall_now

MAX_BACKOFF = 3600.0
SCOPES_TIMEOUT = 10.0  # a provider's scopes() runs in a worker thread and is given up on after this
_FAILING = ("error", "offline")
_KEEP_ITEMS = ("error", "offline", "auth_required", "rate_limited", "stale", "never_fetched")  # never blank a table
LONG_POLL_RUN_CAP = 35.0     # ctx.run inside a long-poll fetch
LONG_POLL_FETCH_CAP = 40.0   # the whole fetch
LONG_POLL_MIN_WAIT = 5.0     # a fetch faster than this did not really wait for news: back off to the interval


def _mode(provider) -> str:
    return "long_poll" if getattr(provider, "mode", "interval") == "long_poll" else "interval"


def _always_on(provider) -> bool:
    return getattr(provider, "always_on", False) is True


@dataclass(frozen=True)
class JobKey:
    addon: str
    provider: str
    scope: str


@dataclass
class _State:
    next_at: float = 0.0
    hold_until: float = 0.0
    failures: int = 0
    running: bool = False
    manual: bool = False


class Scheduler:
    def __init__(self, ws, registry=None, *, clock=time.monotonic, wall=wall_now, live=lambda: False, timeout=None):
        self.ws = ws
        self._registry = registry
        self.clock, self.wall, self.live, self.timeout = clock, wall, live, timeout
        self.state: dict[JobKey, _State] = {}
        self._targets: dict[JobKey, tuple] = {}
        # (addon, provider id) -> (provider object, valid until, scopes); and scopes() calls still running
        self._scope_cache: dict[tuple[str, str], tuple[object, float, list[str]]] = {}
        self._scope_threads: dict[tuple[str, str], threading.Thread] = {}
        self._tasks: set[asyncio.Future] = set()
        self._lock = threading.Lock()  # guards the scopes-thread start and the pending refreshes
        self._refresh: set[str] = set()  # addon names whose Refresh applies at the next due()
        self._refresh_all = False

    @property
    def registry(self):
        return self._registry if self._registry is not None else self.ws.addons

    def _scopes(self, loaded, provider) -> list[str]:
        """The provider's scopes, cached for one interval. Called in a worker thread with a timeout, so a
        hanging scopes() never blocks the caller; while one is still running, the last known scopes are used."""
        ck = (loaded.name, provider.id)
        now = self.clock()
        result: dict = {}
        with self._lock:  # check and start together: two callers never start two scopes() threads
            cached = self._scope_cache.get(ck)
            known = cached[2] if cached is not None and cached[0] is provider else []
            if cached is not None and cached[0] is provider and now < cached[1]:
                return known
            pending = self._scope_threads.get(ck)
            if pending is not None and pending.is_alive():
                return known
            ctx = loaded.ctx.provider_context()

            def work() -> None:
                try:
                    result["scopes"] = list(provider.scopes(ctx))
                except Exception:
                    result["error"] = traceback.format_exc()

            thread = threading.Thread(target=work, daemon=True, name=f"orch-scopes-{loaded.name}-{provider.id}")
            self._scope_threads[ck] = thread
            thread.start()
        thread.join(SCOPES_TIMEOUT)
        if thread.is_alive():
            _log_error(self.ws, loaded.name, f"scopes {provider.id}", f"timed out after {SCOPES_TIMEOUT:g} s\n")
            return known
        self._scope_threads.pop(ck, None)
        if "error" in result:
            _log_error(self.ws, loaded.name, f"scopes {provider.id}", result["error"])
            scopes = []
        else:
            scopes = [s for s in result.get("scopes", []) if isinstance(s, str) and s]
        self._scope_cache[ck] = (provider, now + self._interval(provider), scopes)
        return scopes

    def jobs(self, only=None) -> list[JobKey]:
        """Every (addon, provider, scope) job; with `only(loaded, provider)`, just those providers (no other
        provider's scopes() runs)."""
        keys = []
        for loaded in self.registry:
            for provider in loaded.providers():
                if only is not None and not only(loaded, provider):
                    continue
                for scope in self._scopes(loaded, provider):
                    key = JobKey(loaded.name, provider.id, scope)
                    self._targets[key] = (loaded, provider)
                    self._st(key)
                    keys.append(key)
        return keys

    def _st(self, key: JobKey) -> _State:
        return self.state.setdefault(key, _State())

    def status(self, key: JobKey) -> dict:
        st = self._st(key)
        return {"next_at": st.next_at, "failures": st.failures, "running": st.running}

    def request_refresh(self, addon: str | None = None) -> bool:
        """Queue a manual Refresh for one addon (or all); the next due() applies it. Runs no addon code, so
        a POST never waits on a provider. False when that addon is not loaded."""
        if addon is not None and self.registry.get(addon) is None:
            return False
        with self._lock:
            if addon is None:
                self._refresh_all = True
            else:
                self._refresh.add(addon)
        return True

    def _background_addons(self) -> set[str]:
        """Addons the human switched to "Keep syncing while Mission Control runs" in this workspace."""
        from orch.addons.userfiles import workspace_addons
        try:
            return {n for n, v in workspace_addons(self.ws.root).items() if v.get("background") is True}
        except Exception:
            return set()

    def due(self) -> list[JobKey]:
        now, live = self.clock(), self.live()
        with self._lock:
            names, every = self._refresh, self._refresh_all
            self._refresh, self._refresh_all = set(), False
        bg = self._background_addons()

        def background_job(loaded, provider) -> bool:
            return loaded.name in bg and _always_on(provider)

        quiet = not live and not names and not every and not any(st.manual for st in list(self.state.values()))
        if quiet and not (bg and any(background_job(la, p) for la in self.registry for p in la.providers())):
            return []  # nobody is looking and nothing was asked for: run no provider code at all
        out, keyed = [], set()
        # While nobody is looking, only the always_on providers the human switched on run (also their scopes()).
        for key in self.jobs(only=background_job if quiet else None):
            keyed.add(key.addon)
            st = self._st(key)
            if every or key.addon in names:
                st.manual = True
                st.next_at = st.hold_until  # a rate limit still holds; everything else runs now
            target = self._targets.get(key)
            background = target is not None and background_job(target[0], target[1])
            if not st.running and (live or st.manual or background) and now >= st.next_at:
                out.append(key)
        asked = {la.name for la in self.registry} if every else names
        waiting = {n for n in asked - keyed if self._scopes_pending(n)}
        if waiting:  # a cold, slow scopes(): keep the Refresh until its keys are known, so it is never lost
            with self._lock:
                self._refresh |= waiting
        return out

    def _scopes_pending(self, addon: str) -> bool:
        """True while some provider of `addon` has no scopes yet (its first scopes() is still running)."""
        loaded = self.registry.get(addon)
        if loaded is None:
            return False
        for provider in loaded.providers():
            cached = self._scope_cache.get((addon, provider.id))
            if cached is None or cached[0] is not provider:
                return True
        return False

    def _interval(self, provider) -> float:
        try:
            return max(5.0, float(getattr(provider, "interval_s", 300) or 300))
        except (TypeError, ValueError):
            return 300.0

    def _failed(self, key: JobKey, previous: Snapshot | None, message: str) -> Snapshot:
        if previous is None or (not previous.items and previous.health != "ok"):
            # nothing good to keep: a first failure (or one after failures only) is an error, not "stale"
            return Snapshot(key.provider, key.scope, self.wall(), health="error", message=message)
        return previous.replace(health="stale", message=message)

    def _schedule(self, key: JobKey, snap: Snapshot, failed: bool, interval: float, elapsed: float = 0.0) -> None:
        st = self._st(key)
        now = self.clock()
        st.failures = st.failures + 1 if failed else 0
        st.hold_until = 0.0
        if snap.health == "rate_limited" and snap.retry_after is not None:
            # wait until Retry-After, but at least one interval (also for a Retry-After already in the past)
            st.hold_until = now + max(interval, (snap.retry_after - self.wall()).total_seconds())
            st.next_at = st.hold_until
        elif failed:
            st.next_at = now + min(interval * 2 ** st.failures, MAX_BACKOFF)
        elif snap.health == "auth_required":
            st.next_at = now + min(interval * 4, MAX_BACKOFF)
        elif key in self._targets and _mode(self._targets[key][1]) == "long_poll" and elapsed >= LONG_POLL_MIN_WAIT:
            st.next_at = now  # the provider itself waited (up to 35 s) for news: ask again at once
        elif key in self._targets and _mode(self._targets[key][1]) == "long_poll":
            # it returned almost instantly: not a real long-poll wait (a misbehaving or abusive endpoint) —
            # back off to the ordinary interval instead of hammering it in a tight loop
            st.next_at = now + interval
        else:
            st.next_at = now + interval

    def run_job(self, key: JobKey) -> Snapshot:
        st = self._st(key)
        st.running, st.manual = True, False
        try:
            loaded, provider = self._targets[key]
            previous = cache.read_snapshot(self.ws, key.addon, key.provider, key.scope)
            interval = self._interval(provider)
            start = self.clock()
            try:
                cap = LONG_POLL_RUN_CAP if _mode(provider) == "long_poll" else None
                snap = provider.fetch(loaded.ctx.provider_context(max_timeout=cap), key.scope, previous)
                if not isinstance(snap, Snapshot) or snap.provider != key.provider or snap.scope != key.scope:
                    raise ValueError("fetch must return a Snapshot for its own provider id and scope")
                snap.to_dict()
            except Exception as e:
                _log_error(self.ws, key.addon, f"fetch {key.provider} {key.scope}")
                snap, failed = self._failed(key, previous, f"fetch failed: {type(e).__name__}: {e}"), True
            else:
                failed = snap.health in _FAILING
                if snap.health in _KEEP_ITEMS and not snap.items and previous is not None and previous.items:
                    snap = snap.replace(items=previous.items, complete=False)
            self._schedule(key, snap, failed, interval, self.clock() - start)
            cache.write_snapshot(self.ws, key.addon, snap)
            return snap
        finally:
            st.running = False

    def step(self) -> list[JobKey]:
        keys = self.due()
        for key in keys:
            self.run_job(key)
        return keys

    async def _run_async(self, key: JobKey) -> None:
        target = self._targets.get(key)
        if target is None:  # the addon went away between due() and now
            self._st(key).running = False
            return
        provider = target[1]
        if _mode(provider) == "long_poll":
            limit = LONG_POLL_FETCH_CAP if self.timeout is None else min(self.timeout, LONG_POLL_FETCH_CAP)
        else:
            limit = self.timeout if self.timeout is not None else max(30.0, self._interval(provider))
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.to_thread(self.run_job, key)), timeout=limit)
        except asyncio.TimeoutError:
            await asyncio.to_thread(self._timed_out, key, provider, limit)  # file I/O off the event loop
        except Exception:  # run_job's own `finally` has already cleared `running`
            _log_error(self.ws, key.addon, f"scheduler {key.provider} {key.scope}")

    def _timed_out(self, key: JobKey, provider, limit: float) -> None:
        previous = cache.read_snapshot(self.ws, key.addon, key.provider, key.scope)
        snap = self._failed(key, previous, f"timed out after {limit:g} s")
        self._schedule(key, snap, True, self._interval(provider))
        cache.write_snapshot(self.ws, key.addon, snap)  # `running` stays True until the thread returns

    async def shutdown(self) -> None:
        """Cancel the fetches still in flight and force-kill any live subprocess (ctx.run) they started: a
        blocked `_run_async` is shielded from its own task's cancellation (so a late answer can still be
        scheduled), so cancelling the tasks alone would leave a long-poll's subprocess running for up to its
        own 35 s cap, holding up exit for no reason. Killing it first lets its worker thread finish at once."""
        from orch.addons.runner import kill_live_processes

        for task in list(self._tasks):
            task.cancel()
        await asyncio.to_thread(kill_live_processes)
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def run_forever(self, tick: float = 2.0) -> None:
        while True:
            try:
                # due() calls providers' scopes(): never on the event-loop thread (ruling F12)
                for key in await asyncio.to_thread(self.due):
                    self._st(key).running = True
                    task = asyncio.ensure_future(self._run_async(key))
                    self._tasks.add(task)  # keep a reference until it is done
                    task.add_done_callback(self._tasks.discard)
            except Exception:
                _log_error(self.ws, "scheduler", "tick")
            await asyncio.sleep(tick)

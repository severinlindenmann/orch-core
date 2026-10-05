import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from addon_fixtures import Clock, loaded
from orch.addons import cache
from orch.addons.api import Snapshot
from orch.addons.loader import AddonRegistry
from orch.addons.scheduler import JobKey, Scheduler

WALL = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


class Scripted:
    """A provider that returns (or raises) the next scripted result for each fetch."""
    id = "fake"
    kind = "status"
    interval_s = 60

    def __init__(self, *results, scopes=("a",)):
        self.results = list(results)
        self._scopes = scopes
        self.calls = []

    def scopes(self, ctx):
        return list(self._scopes)

    def fetch(self, ctx, scope, previous):
        self.calls.append((scope, previous))
        r = self.results.pop(0)
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return r(scope)
        return r.replace(scope=scope)


def snap(health="ok", items=({"id": "x", "label": "X", "role": "ok", "text": "1"},), **kw):
    return Snapshot("fake", "a", WALL, health=health, items=tuple(items), **kw)


class Obj:
    def __init__(self, provider):
        self.providers = [provider]


def sched(ws, provider, **kw):
    clock = Clock()
    reg = AddonRegistry(ws, {"demo": loaded(ws, Obj(provider))})
    return Scheduler(ws, reg, clock=clock, wall=lambda: WALL, **kw), clock


def test_nothing_runs_without_a_live_stream_or_refresh(ws):
    s, _ = sched(ws, Scripted(snap()))
    assert s.jobs() == [JobKey("demo", "fake", "a")] and s.due() == []


def test_live_runs_and_caches(ws):
    p = Scripted(snap(), snap())
    s, clock = sched(ws, p, live=lambda: True)
    assert s.step() == [JobKey("demo", "fake", "a")]
    assert cache.read_snapshot(ws, "demo", "fake", "a").items[0]["id"] == "x"
    assert s.due() == []                      # next run after interval_s
    clock.t += 60
    assert s.step() and p.calls[1][1] is not None  # the previous snapshot is passed in


def test_manual_refresh_runs_once_without_live(ws):
    s, _ = sched(ws, Scripted(snap()))
    assert s.request_refresh("demo") is True and s.request_refresh("other") is False
    assert s.step() == [JobKey("demo", "fake", "a")] and s.due() == []


def test_single_flight(ws):
    s, _ = sched(ws, Scripted(snap()), live=lambda: True)
    key = s.jobs()[0]
    s.state[key].running = True
    assert s.due() == []


def test_failure_keeps_previous_items_as_stale(ws):
    p = Scripted(snap(), RuntimeError("network down"), RuntimeError("still down"))
    s, clock = sched(ws, p, live=lambda: True)
    s.step()
    clock.t += 60
    s.step()
    stale = cache.read_snapshot(ws, "demo", "fake", "a")
    assert stale.health == "stale" and stale.items and stale.fetched_at == WALL and "network down" in stale.message
    assert s.status(s.jobs()[0])["next_at"] == clock.t + 120   # 60 × 2^1
    clock.t += 120
    s.step()
    assert s.status(s.jobs()[0])["next_at"] == clock.t + 240   # 60 × 2^2
    assert "network down" in (ws.state_dir / "addon-errors.log").read_text(encoding="utf-8")


def test_first_failure_without_previous_is_an_error(ws):
    s, _ = sched(ws, Scripted(ValueError("bad")), live=lambda: True)
    s.step()
    assert cache.read_snapshot(ws, "demo", "fake", "a").health == "error"


def test_error_without_items_keeps_previous_items(ws):
    s, clock = sched(ws, Scripted(snap(), snap("offline", items=(), message="no network")), live=lambda: True)
    s.step()
    clock.t += 60
    s.step()
    got = cache.read_snapshot(ws, "demo", "fake", "a")
    assert got.health == "offline" and got.items and got.complete is False


def test_wrong_return_value_is_a_failure(ws):
    s, _ = sched(ws, Scripted(lambda scope: {"not": "a snapshot"}), live=lambda: True)
    s.step()
    assert cache.read_snapshot(ws, "demo", "fake", "a").health == "error"


def test_backoff_is_capped(ws):
    s, clock = sched(ws, Scripted(*[RuntimeError("x")] * 12), live=lambda: True)
    gaps = []
    for _ in range(12):
        s.step()
        gaps.append(s.status(s.jobs()[0])["next_at"] - clock.t)
        clock.t += gaps[-1]
    assert gaps[:3] == [120, 240, 480] and max(gaps) == 3600


def test_rate_limit_honours_retry_after_even_on_refresh(ws):
    limited = snap("rate_limited", items=(), retry_after=WALL + timedelta(minutes=10), message="rate limit")
    s, clock = sched(ws, Scripted(snap(), limited, snap()), live=lambda: True)
    s.step()
    clock.t += 60
    s.step()
    key = s.jobs()[0]
    assert s.status(key)["next_at"] == clock.t + 600
    s.request_refresh("demo")
    assert s.due() == []
    clock.t += 600
    assert s.due() == [key]


def test_identical_content_does_not_touch_the_marker(ws):
    s, clock = sched(ws, Scripted(snap(), snap()), live=lambda: True)
    s.step()
    marker = cache.changed_marker(ws)
    first = marker.read_text(encoding="utf-8")
    marker.write_text("sentinel", encoding="utf-8")
    clock.t += 60
    s.step()
    assert marker.read_text(encoding="utf-8") == "sentinel" and first


def test_scopes_error_is_logged_and_others_run(ws):
    class BadScopes(Scripted):
        def scopes(self, ctx):
            raise RuntimeError("no config")
    bad = BadScopes()
    bad.id = "bad"
    good = Scripted(snap())
    clock = Clock()
    obj = Obj(good)
    obj.providers.append(bad)
    s = Scheduler(ws, AddonRegistry(ws, {"demo": loaded(ws, obj)}), clock=clock, wall=lambda: WALL, live=lambda: True)
    assert s.step() == [JobKey("demo", "fake", "a")]
    assert "scopes bad" in (ws.state_dir / "addon-errors.log").read_text(encoding="utf-8")


def test_scope_slug_is_safe_and_unique():
    a, b = cache.scope_slug("acme/ticket-orch-demo"), cache.scope_slug("acme_ticket-orch-demo")
    assert "/" not in a and a != b and a.startswith("acme_ticket-orch-demo-")


def test_broken_cache_file_reads_as_none(ws):
    path = cache.cache_path(ws, "demo", "fake", "a")
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    assert cache.read_snapshot(ws, "demo", "fake", "a") is None and cache.read_snapshots(ws, "demo") == []


def test_timeout_marks_stale_and_stays_single_flight(ws):
    def slow(scope):
        time.sleep(0.6)
        return snap()
    s, _ = sched(ws, Scripted(snap(), slow), live=lambda: True, timeout=0.1)
    s.step()
    s.state[s.jobs()[0]].next_at = 0

    async def main():
        task = asyncio.create_task(s.run_forever(tick=0.05))
        await asyncio.sleep(0.4)  # timed out at 0.1 s, the thread is still sleeping
        got = cache.read_snapshot(ws, "demo", "fake", "a")
        running = s.status(s.jobs()[0])["running"]
        task.cancel()
        return got, running
    got, running = asyncio.run(main())  # asyncio.run waits for the thread; its late result may land after this
    assert got.health == "stale" and "timed out" in got.message and running is True


def test_scopes_never_run_on_the_event_loop_thread(ws):
    import threading
    where = []

    class Watched(Scripted):
        def scopes(self, ctx):
            where.append(threading.get_ident())
            return ["a"]
    s, _ = sched(ws, Watched(snap()), live=lambda: True)

    async def main():
        loop_thread = threading.get_ident()
        task = asyncio.create_task(s.run_forever(tick=0.05))
        await asyncio.sleep(0.3)
        task.cancel()
        return loop_thread
    loop_thread = asyncio.run(main())
    assert where and loop_thread not in where


def test_hanging_scopes_time_out_and_are_not_called_again_meanwhile(ws, monkeypatch):
    from orch.addons import scheduler
    monkeypatch.setattr(scheduler, "SCOPES_TIMEOUT", 0.1)
    calls = []

    class Hanging(Scripted):
        def scopes(self, ctx):
            calls.append(1)
            time.sleep(0.5)
            return ["a"]
    s, _ = sched(ws, Hanging(snap()), live=lambda: True)
    started = time.monotonic()
    assert s.jobs() == [] and s.jobs() == []
    assert time.monotonic() - started < 0.4 and len(calls) == 1
    assert "timed out" in (ws.state_dir / "addon-errors.log").read_text(encoding="utf-8")


def test_scopes_are_cached_for_one_interval(ws):
    calls = []

    class Counting(Scripted):
        def scopes(self, ctx):
            calls.append(1)
            return ["a"]
    s, clock = sched(ws, Counting(snap()))
    s.jobs()
    s.jobs()
    assert len(calls) == 1
    clock.t += 60
    s.jobs()
    assert len(calls) == 2


def test_job_whose_addon_went_away_does_not_stay_running(ws):
    s, _ = sched(ws, Scripted(snap()), live=lambda: True)
    key = s.jobs()[0]
    s._targets.clear()
    with pytest.raises(KeyError):
        s.run_job(key)
    assert s.status(key)["running"] is False


class CountingScopes(Scripted):
    def __init__(self, *results):
        super().__init__(*results)
        self.scope_calls = 0

    def scopes(self, ctx):
        self.scope_calls += 1
        return ["a"]


def test_refresh_runs_no_addon_code_until_the_next_tick(ws):
    p = CountingScopes(snap())
    s, _ = sched(ws, p)
    assert s.request_refresh() is True and s.request_refresh("demo") is True
    assert p.scope_calls == 0 and p.calls == []
    assert s.step() == [JobKey("demo", "fake", "a")] and p.scope_calls == 1


def test_no_viewer_and_no_refresh_runs_no_provider_code(ws):
    p = CountingScopes(snap())
    s, clock = sched(ws, p)
    for _ in range(3):
        assert s.due() == []
        clock.t += 600
    assert p.scope_calls == 0


def test_refresh_during_a_running_fetch_runs_once_it_is_done(ws):
    s, _ = sched(ws, Scripted(snap(), snap()))
    key = s.jobs()[0]
    s.state[key].running = True
    s.request_refresh("demo")
    assert s.due() == []
    s.state[key].running = False
    assert s.due() == [key]


def test_past_retry_after_waits_one_interval(ws):
    limited = snap("rate_limited", items=(), retry_after=WALL - timedelta(minutes=5), message="rate limit")
    s, clock = sched(ws, Scripted(limited), live=lambda: True)
    s.step()
    assert s.status(s.jobs()[0])["next_at"] == clock.t + 60


@pytest.mark.parametrize("health", ["stale", "never_fetched"])
def test_stale_or_never_fetched_without_items_keeps_previous_items(ws, health):
    s, clock = sched(ws, Scripted(snap(), snap(health, items=())), live=lambda: True)
    s.step()
    clock.t += 60
    s.step()
    got = cache.read_snapshot(ws, "demo", "fake", "a")
    assert got.health == health and got.items and got.complete is False


def test_shutdown_cancels_fetches_in_flight(ws):
    def slow(scope):
        time.sleep(0.3)
        return snap()
    s, _ = sched(ws, Scripted(slow), live=lambda: True, timeout=5)

    async def main():
        task = asyncio.create_task(s.run_forever(tick=0.05))
        await asyncio.sleep(0.1)
        assert s._tasks
        task.cancel()
        await s.shutdown()
        return all(t.done() for t in s._tasks)
    assert asyncio.run(main()) is True


def test_shutdown_kills_a_live_subprocess_promptly(ws, monkeypatch):
    """A fetch blocked inside ctx.run (a long-poll can wait up to 35 s) is shielded from its own task's
    cancellation (shutdown cancels the outer task, not the shielded inner one — a late answer can still be
    scheduled); shutdown() must still kill the real subprocess so the worker thread (and so exit) is not held
    up for the rest of its timeout."""
    import os
    import sys
    from pathlib import Path

    exe = Path(sys.executable)
    monkeypatch.setenv("PATH", str(exe.parent) + os.pathsep + os.environ.get("PATH", ""))

    class Blocking:
        id, kind, interval_s = "fake", "status", 60

        def scopes(self, ctx):
            return ["a"]

        def fetch(self, ctx, scope, previous):
            ctx.run([exe.name, "-c", "import time; time.sleep(30)"], timeout=30)
            return snap()

    reg = AddonRegistry(ws, {"demo": loaded(ws, Obj(Blocking()), binaries=[exe.name])})
    s = Scheduler(ws, reg, clock=Clock(), wall=lambda: WALL, live=lambda: True)

    async def main():
        task = asyncio.create_task(s.run_forever(tick=0.05))
        await asyncio.sleep(0.3)  # let the subprocess actually start
        assert s._tasks
        task.cancel()
        await s.shutdown()
        # shutdown() kills the process at once; the worker thread's own poll loop (up to 0.2 s) then notices
        # it died and drops it from the live registry — poll briefly instead of racing that.
        from orch.addons.runner import _LIVE
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _LIVE:
            await asyncio.sleep(0.02)
        return len(_LIVE)

    remaining = asyncio.run(main())
    assert remaining == 0, "the subprocess was still alive after shutdown()"


@pytest.mark.parametrize("all_addons", [False, True])
def test_refresh_on_a_cold_slow_scopes_is_not_lost(ws, monkeypatch, all_addons):
    import threading
    from orch.addons import scheduler
    monkeypatch.setattr(scheduler, "SCOPES_TIMEOUT", 0.05)
    release = threading.Event()

    class Slow(Scripted):
        def scopes(self, ctx):
            release.wait(5)
            return ["a"]

    p = Slow(snap())
    s, _ = sched(ws, p)
    assert s.request_refresh(None if all_addons else "demo")
    assert s.due() == []  # scopes() still running: no keys yet
    release.set()
    for _ in range(100):
        if not s._scope_threads.get(("demo", "fake")) or not s._scope_threads[("demo", "fake")].is_alive():
            break
        time.sleep(0.01)
    assert s.due() == [JobKey("demo", "fake", "a")]  # the Refresh was kept until the scopes were known


def test_refresh_for_an_addon_without_scopes_is_not_requeued_forever(ws):
    s, _ = sched(ws, Scripted(scopes=()))
    s.request_refresh("demo")
    assert s.due() == [] and s._refresh == set() and s._refresh_all is False


def test_snapshots_of_a_scope_the_provider_no_longer_lists_are_dropped(ws):
    """WK-01: switching provider (or removing a repo) must not leave the old scope's failure showing on the page."""
    cache.write_snapshot(ws, "demo", Snapshot("fake", "old", WALL, health="error", message="no longer wanted"))
    cache.write_snapshot(ws, "demo", Snapshot("other", "old", WALL, health="error", message="another provider's"))
    p = Scripted(snap())
    s, _ = sched(ws, p, live=lambda: True)
    assert s.step() == [JobKey("demo", "fake", "a")]
    left = {(x.provider, x.scope) for x in cache.read_snapshots(ws, "demo")}
    assert left == {("fake", "a"), ("other", "old")}  # only this provider's unlisted scope went


def test_a_failing_scopes_call_does_not_delete_the_cache(ws):
    class BadScopes(Scripted):
        def scopes(self, ctx):
            raise RuntimeError("no config")
    cache.write_snapshot(ws, "demo", Snapshot("fake", "a", WALL, items=({"id": "x", "label": "X", "role": "ok", "text": "1"},)))
    s, _ = sched(ws, BadScopes(), live=lambda: True)
    s.step()
    assert cache.read_snapshot(ws, "demo", "fake", "a") is not None

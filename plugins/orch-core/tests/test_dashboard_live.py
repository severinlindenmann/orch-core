import asyncio

import pytest

pytest.importorskip("fastapi")

from orch.dashboard.routes_live import change_stream, watch_filter


def test_watch_filter(ws):
    accept = watch_filter(ws)
    assert accept(None, str(ws.status_dir("open") / "L-0001-x.md"))
    assert accept(None, str(ws.artifacts_dir / "L-0001" / "shot.png"))
    assert accept(None, str(ws.state_dir / "events.jsonl"))
    assert not accept(None, str(ws.state_dir / "index.json"))
    assert not accept(None, str(ws.state_dir / "locks" / "L-0001.lock"))
    assert not accept(None, str(ws.status_dir("open") / ".L-0001-x.md.abc.tmp"))


def test_change_stream_reports_ticket_changes(ws):
    """The watcher may arm (or coalesce events) a little after the first ping, so keep rewriting the
    ticket on every ping until a change event arrives; fail only after a generous deadline."""
    target = ws.status_dir("open") / "L-0001-x.md"

    async def main():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        stream = change_stream(ws, timeout=30, heartbeat_ms=100)
        try:
            assert (await stream.__anext__()).startswith(": connected\n\nevent: hello\ndata: ")
            n = 0
            while loop.time() < deadline:
                try:
                    frame = await asyncio.wait_for(stream.__anext__(), timeout=max(0.1, deadline - loop.time()))
                except (StopAsyncIteration, asyncio.TimeoutError):
                    return False
                if frame.startswith("event: change"):
                    return True
                n += 1  # a ping: the watcher is running; (re)write so a change is pending
                target.write_text(f"---\nid: L-0001\nrev: {n}\n---\n", encoding="utf-8")
            return False
        finally:
            await stream.aclose()

    assert asyncio.run(main())


def test_events_endpoint(dash):
    r = dash.get("/events?timeout=0.2")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.text.startswith(": connected")


def test_live_reload_is_debounced(dash):
    js = dash.get("/static/app.js").text
    assert "clearTimeout(timer)" in js and "}, 1500);" in js


def _fake_watcher(monkeypatch):
    """Replace the file watcher: count how many run, and let the test push one change set."""
    from orch.dashboard import routes_live

    started = []
    pushed = asyncio.Event()

    async def fake_awatch(*paths, **kwargs):
        started.append(paths)
        await pushed.wait()
        yield {("modified", "x")}
        await asyncio.Event().wait()  # then stay idle until cancelled

    monkeypatch.setattr(routes_live, "_awatch", lambda: fake_awatch)
    return started, pushed


def test_many_streams_share_one_watcher(ws, monkeypatch):
    """Each page holds an /events stream; a watcher (and its worker thread) per stream exhausted the
    server's thread pool and stalled every page for up to 15 s after page switches."""
    started, pushed = _fake_watcher(monkeypatch)

    async def main():
        streams = [change_stream(ws, timeout=30, heartbeat_ms=60000) for _ in range(50)]
        for s in streams:
            assert (await s.__anext__()).startswith(": connected")
        nexts = [asyncio.ensure_future(s.__anext__()) for s in streams]
        await asyncio.sleep(0.05)
        assert len(started) == 1
        pushed.set()
        frames = await asyncio.wait_for(asyncio.gather(*nexts), timeout=5)
        for s in streams:
            await s.aclose()
        return frames

    frames = asyncio.run(main())
    assert all(f.startswith("event: change") for f in frames)


def test_watcher_stops_when_the_last_stream_closes(ws, monkeypatch):
    from orch.dashboard import routes_live

    started, _ = _fake_watcher(monkeypatch)

    async def main():
        s = change_stream(ws, timeout=30, heartbeat_ms=60000)
        await s.__anext__()
        nxt = asyncio.ensure_future(s.__anext__())
        await asyncio.sleep(0.05)
        hub = routes_live._hub(ws)
        assert hub.task is not None and not hub.task.done()
        nxt.cancel()  # what Starlette does when the browser leaves the page
        with pytest.raises(asyncio.CancelledError):
            await nxt
        await s.aclose()
        await asyncio.sleep(0.05)
        assert not hub.subscribers and (hub.task is None or hub.task.done())

    asyncio.run(main())
    assert len(started) == 1


def test_hello_frame_carries_the_page_version(ws, dash, put):
    """A hidden tab drops its /events stream (browsers allow only 6 connections per host); when it
    comes back it compares the server's version with the one its page was rendered with."""
    import re

    from orch.dashboard.routes_live import live_version

    tid = put("open")
    html = dash.get("/").text
    page_v = re.search(r'<html[^>]*data-version="([^"]+)"', html).group(1)
    assert page_v == live_version(ws)
    text = dash.get("/events?timeout=0.2").text
    assert text.startswith(": connected") and f"event: hello\ndata: {page_v}\n\n" in text

    from orch.core import store
    path = store.resolve(ws, tid).path
    path.write_text(path.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    assert live_version(ws) != page_v


def test_only_visible_tabs_hold_a_live_connection(dash):
    js = dash.get("/static/app.js").text
    assert "visibilitychange" in js and "source.close()" in js
    assert 'addEventListener("hello"' in js and "dataset.version" in js


def test_cache_marker_wakes_the_live_stream(ws):
    from orch.addons import cache
    from orch.dashboard.routes_live import live_version, watch_filter
    marker = cache.changed_marker(ws)
    before = live_version(ws)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("x", encoding="utf-8")
    assert watch_filter(ws)(None, str(marker)) is True and live_version(ws) != before
    assert watch_filter(ws)(None, str(cache.cache_path(ws, "demo", "fake", "a"))) is False


def test_subscriber_count(ws):
    import asyncio
    from orch.dashboard import routes_live

    async def main():
        hub = routes_live._hub(ws)
        q = asyncio.Queue()
        hub.subscribers.add(q)
        try:
            return routes_live.subscriber_count(ws)
        finally:
            hub.subscribers.discard(q)
    assert asyncio.run(main()) == 1


def test_live_version_reuses_the_requests_file_stats(ws, put, monkeypatch):
    import os
    from orch.core import store
    from orch.dashboard.routes_live import live_version
    for _ in range(3):
        put("open")
    outside = live_version(ws)
    calls = []
    real = os.stat
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: calls.append(os.fspath(p)) or real(p, *a, **k))
    with store.request_scope():
        store.scan(ws)
        calls.clear()
        assert live_version(ws) == outside
        assert not [c for c in calls if c.endswith(".md")]  # every ticket file was already stat'ed by the scan

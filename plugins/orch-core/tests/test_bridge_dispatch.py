"""The in-memory bridge dispatcher: ASGI only, no server, no network, no crypto."""
import asyncio
import contextlib
import subprocess
import time

import pytest

pytest.importorskip("fastapi")

from orch.dashboard import reach, terminals  # noqa: E402
from orch.dashboard.app import create_app  # noqa: E402
from orch.dashboard.bridge_dispatch import (Body, BridgeRequest, End, Limits, Refused, Start,  # noqa: E402
                                            dispatch)
from orch.dashboard.reach import RemoteOrigin, Scope  # noqa: E402
from orch.dashboard.routes_live import subscriber_count  # noqa: E402
from orch.dashboard.remote_gate import NO_WAY  # noqa: E402

TOKEN = "tok-SECRET-9f3a71c0"


def origin(scope=Scope.TYPE, fresh=False):
    return RemoteOrigin("dev_abc123", scope, "Pixel 8", fresh)


@pytest.fixture
def app(ws):
    return create_app(ws, TOKEN)


def yes():
    return True


async def collect(app, request, o=None, *, auth=yes, limits=Limits()):
    return [e async for e in dispatch(app, request, o or origin(), still_authorized=auth, limits=limits)]


def get(path, **headers):
    return BridgeRequest("GET", path, headers)


def run(coro):
    return asyncio.run(coro)


def blob(events):
    out = b""
    for e in events:
        if isinstance(e, Body):
            out += e.chunk
        elif isinstance(e, Start):
            out += repr(e.headers).encode()
    return out


def extra_tasks():
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


# -- normal pages and the gate -------------------------------------------------------------------------------------

def test_a_page_comes_back_for_a_device_with_enough_scope(app, put):
    put("backlog")
    events = run(collect(app, get("/", accept="text/html"), origin(Scope.LOOK)))
    assert isinstance(events[0], Start) and events[0].status == 200 and isinstance(events[-1], End)
    assert b"<html" in blob(events)
    assert not [e for e in events if isinstance(e, Refused)]


def test_the_gate_still_decides_and_every_refusal_reads_the_same(app, put):
    tid = put("backlog")

    async def main():
        low = await collect(app, BridgeRequest("POST", f"/t/{tid}/comment", {"content-type":
                            "application/x-www-form-urlencoded"}, b"text=hi"), origin(Scope.LOOK))
        never = await collect(app, get("/workspace"), origin(Scope.TYPE))
        unknown = await collect(app, get("/no/such/page"), origin(Scope.TYPE))
        return low, never, unknown

    results = run(main())
    for events in results:
        assert events[0].status == 403 and isinstance(events[-1], End)
    assert len({blob(e[1:]) for e in results}) == 1 and NO_WAY.encode() in blob(results[0])


def test_the_token_is_never_in_any_event(app, put):
    put("backlog")

    async def main():
        events = []
        for path in ("/", "/board", "/workspace", f"/?token={TOKEN}", "/static/app.css"):
            events += await collect(app, get(path))
        return events

    events = run(main())
    assert TOKEN.encode() not in blob(events)
    assert not [e for e in events if isinstance(e, Start) and any(k.lower() == "set-cookie" for k, _ in e.headers)]


def test_forged_headers_are_ignored_and_the_app_sees_the_bridges_scope(app, put):
    put("backlog")
    seen = []

    async def spy(scope, receive, send):
        seen.append(scope)
        await app(scope, receive, send)

    spy.state = app.state
    o = origin(Scope.LOOK)
    forged = {"Cookie": "orch_token=evil", "Origin": "http://evil.example", "Host": "evil.example",
              "X-Forwarded-For": "127.0.0.1", "x-orch-remote": "1", "orch.remote": "1", "Accept": "text/html",
              "accept-encoding": "gzip"}
    events = run(collect(spy, get("/board", **forged), o))
    assert events[0].status == 200  # the real cookie was injected, so the page opened
    scope = seen[0]
    names = {k: v for k, v in scope["headers"]}
    assert names[b"host"] == b"127.0.0.1" and names[b"origin"] == b"http://127.0.0.1"
    assert names[b"cookie"] == f"orch_token={TOKEN}".encode()
    assert set(names) == {b"host", b"origin", b"cookie", b"content-length", b"accept"}
    assert scope["client"] == ("remote", 0) and scope[reach.SCOPE_KEY] is o
    assert scope["path"] == "/board" and scope["raw_path"] == b"/board"


def test_malformed_requests_are_refused_before_anything_runs(app):
    ran = []

    async def spy(scope, receive, send):
        ran.append(1)

    spy.state = app.state
    bad = [BridgeRequest("GET", "board", {}), BridgeRequest("GET", "/a b", {}), BridgeRequest("GET", "/%ff", {}),
           BridgeRequest("get", "/", {}), BridgeRequest("GET", "/é", {}), BridgeRequest("GET", "/x\r\ny", {}),
           BridgeRequest("GET", "/", {"accept": "a\r\nb"}), BridgeRequest("GET", "/", {}, "text")]
    for r in bad:
        assert run(collect(spy, r)) == [Refused("bad_request")], r
    assert run(collect(spy, get("/"), object())) == [Refused("error")]
    assert not ran


def test_a_redirect_is_returned_not_followed(app, put):
    tid = put("backlog")
    r = BridgeRequest("POST", f"/t/{tid}/comment", {"content-type": "application/x-www-form-urlencoded"}, b"text=hi")
    events = run(collect(app, r, origin(Scope.OPERATE)))
    assert events[0].status == 303 and dict(events[0].headers)["location"].startswith("/") and isinstance(events[-1], End)


# -- caps ----------------------------------------------------------------------------------------------------------

def test_a_body_over_the_request_cap_is_refused_before_running(app):
    ran = []

    async def spy(scope, receive, send):
        ran.append(1)

    spy.state = app.state
    r = BridgeRequest("POST", "/new", {}, b"x" * 101)
    assert run(collect(spy, r, limits=Limits(max_request_bytes=100))) == [Refused("too_large")]
    assert not ran


def test_a_response_over_the_cap_ends_with_too_large(app):
    events = run(collect(app, get("/"), limits=Limits(max_response_bytes=100)))
    assert isinstance(events[0], Start) and events[-1] == Refused("too_large")
    assert sum(len(e.chunk) for e in events if isinstance(e, Body)) <= 100


def test_chunks_are_split_to_the_chunk_cap(app):
    whole = run(collect(app, get("/board")))
    small = run(collect(app, get("/board"), limits=Limits(max_chunk=64)))
    chunks = [e.chunk for e in small if isinstance(e, Body)]
    assert chunks and all(len(c) <= 64 for c in chunks) and len(chunks) > 1
    assert b"".join(chunks) == b"".join(e.chunk for e in whole if isinstance(e, Body))


def test_a_response_over_the_duration_cap_ends_with_timeout(app):
    async def slow(scope, receive, send):  # starts at once, then produces nothing more
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x", "more_body": True})
        await asyncio.sleep(60)

    slow.state = app.state

    async def main():
        before = set(extra_tasks())
        t0 = time.monotonic()
        events = await collect(slow, get("/"), limits=Limits(max_seconds=0.3, grace_seconds=0.2))
        elapsed = time.monotonic() - t0
        await settle()
        return events, elapsed, set(extra_tasks()) - before

    events, elapsed, leaked = run(main())
    assert isinstance(events[0], Start) and events[1] == Body(b"x") and events[-1] == Refused("timeout")
    assert elapsed < 3 and not leaked


# -- authorization re-checks ---------------------------------------------------------------------------------------

def test_not_authorized_before_the_start_runs_nothing(app):
    ran = []

    async def spy(scope, receive, send):
        ran.append(1)

    spy.state = app.state
    assert run(collect(spy, get("/"), auth=lambda: False)) == [Refused("not_authorized")]
    assert run(collect(spy, get("/"), auth=lambda: 1)) == [Refused("not_authorized")]  # only True goes on

    def boom():
        raise RuntimeError

    assert run(collect(spy, get("/"), auth=boom)) == [Refused("not_authorized")]
    assert not ran


def test_authorization_is_asked_again_before_the_first_event_and_mid_stream(app):
    calls = []

    async def main():
        def auth():
            calls.append(1)
            return len(calls) <= 2  # before running, before the response starts; then revoked

        before = set(extra_tasks())
        events = await collect(app, get("/events"), auth=auth, limits=Limits(grace_seconds=5))
        await settle()
        return events, set(extra_tasks()) - before, subscriber_count(app.state.ws)

    events, leaked, subs = run(main())
    assert isinstance(events[0], Start) and events[-1] == Refused("not_authorized")
    assert len(calls) == 3 and not [e for e in events if isinstance(e, Body)]
    assert subs == 0 and not leaked


# -- streaming, cancel and disconnect through the app's middleware layers ----------------------------------------

def test_the_event_stream_is_incremental_and_stops_promptly_on_cancel(app):
    async def main():
        before = set(extra_tasks())
        ws = app.state.ws
        stream = dispatch(app, get("/events"), origin(Scope.LOOK), still_authorized=yes,
                          limits=Limits(grace_seconds=10))  # a cancel fallback this late would fail the timing
        first = await stream.__anext__()
        second = await asyncio.wait_for(stream.__anext__(), 5)
        assert isinstance(first, Start) and first.status == 200
        assert isinstance(second, Body) and second.chunk.startswith(b": connected")
        assert subscriber_count(ws) == 1  # the route's generator is running, the response is not finished
        t0 = time.monotonic()
        await stream.aclose()
        elapsed = time.monotonic() - t0
        await settle()
        return elapsed, subscriber_count(ws), set(extra_tasks()) - before

    elapsed, subs, leaked = run(main())
    assert elapsed < 3, "the disconnect did not reach the stream through the middleware layers"
    assert subs == 0  # the stream generator's own cleanup ran
    assert not leaked


def test_a_stuck_app_is_cancelled_after_the_grace(app):
    finished = []

    async def stuck(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await asyncio.sleep(60)  # ignores the disconnect
        finally:
            finished.append(1)

    stuck.state = app.state

    async def main():
        before = set(extra_tasks())
        stream = dispatch(stuck, get("/"), origin(), still_authorized=yes, limits=Limits(grace_seconds=0.2))
        await stream.__anext__()
        await stream.aclose()
        await settle()
        return set(extra_tasks()) - before

    assert not run(main()) and finished


def test_leaving_the_iterator_by_an_exception_also_disconnects(app):
    async def main():
        before = set(extra_tasks())
        with pytest.raises(RuntimeError):
            stream = dispatch(app, get("/events"), origin(Scope.LOOK), still_authorized=yes)
            async with contextlib.aclosing(stream):  # an async generator is closed by its owner, as documented
                async for event in stream:
                    if isinstance(event, Body):
                        raise RuntimeError
        await settle()
        return subscriber_count(app.state.ws), set(extra_tasks()) - before

    subs, leaked = run(main())
    assert subs == 0 and not leaked


def test_a_cancelled_consumer_leaves_nothing_behind(app):
    async def consume():
        async for _ in dispatch(app, get("/events"), origin(Scope.LOOK), still_authorized=yes):
            pass

    async def main():
        before = set(extra_tasks())
        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        await settle()
        return subscriber_count(app.state.ws), set(extra_tasks()) - before

    subs, leaked = run(main())
    assert subs == 0 and not leaked


# -- the terminal stream, with a fake tmux -------------------------------------------------------------------------

class FakeTmux:
    def __init__(self, root):
        self.root, self.captures = root, 0

    def __call__(self, args, timeout=5):
        out, rc = "", 0
        if args[0] == "list-sessions":
            fmt = args[args.index("-F") + 1]
            if fmt == "#{session_name}":
                out = "DEMO-1"
            else:
                out = f"DEMO-1\t{self.root}\t100\t200\t200\t50\t4000"
        elif args[0] == "capture-pane":
            self.captures += 1
            out = "hello\n200 50\n"
        return subprocess.CompletedProcess(args, rc, out, "")


def test_the_terminal_stream_delivers_and_stops_on_cancel(app, ws, monkeypatch):
    from orch.dashboard import routes_terminals
    fake = FakeTmux(ws.root)
    monkeypatch.setattr(terminals, "tmux", fake)
    monkeypatch.setattr(terminals, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(terminals, "addon_on", lambda ws: True)
    monkeypatch.setattr(routes_terminals, "VIEW_SECONDS", 0.02)
    monkeypatch.setattr(routes_terminals, "VIEW_IDLE_SECONDS", 0.05)

    async def main():
        before = set(extra_tasks())
        stream = dispatch(app, get("/terminals/DEMO-1/stream"), origin(Scope.OPERATE), still_authorized=yes,
                          limits=Limits(grace_seconds=10))
        assert (await stream.__anext__()).status == 200
        seen = b""
        while b"event: screen" not in seen:
            event = await asyncio.wait_for(stream.__anext__(), 5)
            assert isinstance(event, Body)
            seen += event.chunk
        t0 = time.monotonic()
        await stream.aclose()
        elapsed = time.monotonic() - t0
        await settle()
        count = fake.captures
        await asyncio.sleep(0.3)
        return elapsed, fake.captures == count, set(extra_tasks()) - before

    elapsed, stopped, leaked = run(main())
    assert elapsed < 3 and stopped and not leaked

    # a device below Operate never reaches the stream
    low = run(collect(app, get("/terminals/DEMO-1/stream"), origin(Scope.DECIDE)))
    assert low[0].status == 403 and b"event: screen" not in blob(low)

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

    spy.state, spy.user_middleware = app.state, app.user_middleware  # as the gated app
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

    spy.state, spy.user_middleware = app.state, app.user_middleware  # as the gated app
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

    spy.state, spy.user_middleware = app.state, app.user_middleware  # as the gated app
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

    slow.state, slow.user_middleware = app.state, app.user_middleware  # as the gated app

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

    spy.state, spy.user_middleware = app.state, app.user_middleware  # as the gated app
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

    stuck.state, stuck.user_middleware = app.state, app.user_middleware  # as the gated app

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


# -- review follow-ups ---------------------------------------------------------------------------------------------

def stub(app, *messages):
    """An app that sends exactly these ASGI messages; it carries the real app's state and gate marker."""
    async def inner(scope, receive, send):
        for m in messages:
            await send(m)

    inner.state, inner.user_middleware = app.state, app.user_middleware
    return inner


START = {"type": "http.response.start", "status": 200, "headers": []}
END = {"type": "http.response.body", "body": b"", "more_body": False}


def test_an_app_without_the_gate_is_refused_before_anything_runs(app):
    from fastapi import FastAPI
    ran = []

    async def bare(scope, receive, send):
        ran.append(1)

    bare.state, bare.user_middleware = app.state, []
    inner = FastAPI()  # a sub-app: has middleware lists, but not the gate
    inner.state.token = TOKEN
    no_state = stub(app)
    no_state.user_middleware = None
    for a in (bare, inner, no_state, object()):
        assert run(collect(a, get("/"))) == [Refused("error")]
    assert not ran


def test_set_cookie_in_any_casing_is_stripped(app):
    headers = [(b"Set-Cookie", b"a=1"), (b"SET-COOKIE", b"b=2"), (b"set-cookie2", b"c=3"), (b"X-Ok", b"yes")]
    events = run(collect(stub(app, {**START, "headers": headers}, END), get("/")))
    assert events[0].headers == (("X-Ok", "yes"),)


def test_a_second_start_is_refused(app):
    events = run(collect(stub(app, START, START, END), get("/")))
    assert events == [Start(200, ())] + [Refused("error")]


def test_duplicate_and_case_variant_allowed_headers_are_refused(app):
    for h in ({"Accept": "a", "accept": "b"}, {"RANGE": "bytes=0-1", "range": "bytes=0-2"}):
        assert run(collect(app, BridgeRequest("GET", "/", h))) == [Refused("bad_request")]


def test_subclasses_and_missing_origins_are_refused(app):
    class Sub(BridgeRequest):
        pass

    class SubOrigin(RemoteOrigin):
        pass

    class SubBytes(bytes):
        pass

    for r in (Sub("GET", "/", {}), BridgeRequest("POST", "/", {}, SubBytes(b"x"))):
        assert run(collect(app, r)) == [Refused("bad_request")]
    for o in (SubOrigin("dev_abc123", Scope.LOOK), None):
        events = run(dispatch_one(app, o))
        assert events == [Refused("error")]


async def dispatch_one(app, o):
    return [e async for e in dispatch(app, get("/"), o, still_authorized=yes)]


def test_head_and_range_pass_through(app):
    head = run(collect(app, BridgeRequest("HEAD", "/static/app.css", {})))
    assert head[0].status == 200 and isinstance(head[-1], End) and not [e for e in head if isinstance(e, Body)]
    part = run(collect(app, BridgeRequest("GET", "/static/app.css", {"Range": "bytes=0-9"})))
    assert part[0].status == 206 and sum(len(e.chunk) for e in part if isinstance(e, Body)) == 10


def test_revocation_after_the_first_body_ends_with_a_refusal_and_no_more_bodies(app):
    calls = []

    def auth():
        calls.append(1)
        return len(calls) <= 3  # run, start, first body

    body = {"type": "http.response.body", "body": b"one", "more_body": True}
    events = run(collect(stub(app, START, body, {**body, "body": b"two"}, END), get("/"), auth=auth))
    assert events == [Start(200, ()), Body(b"one"), Refused("not_authorized")]


def test_a_change_the_app_completed_before_a_late_revocation_stays_applied(app, ws, put, aops):
    """The dispatcher never rolls back: the pre-run check is the only gate that matters for a state change."""
    from orch.core import store
    tid = put("open")
    aops.claim(tid)
    calls = []

    def auth():
        calls.append(1)
        return len(calls) == 1  # allowed to run, revoked before the response starts

    r = BridgeRequest("POST", f"/t/{tid}/comment", {"content-type": "application/x-www-form-urlencoded"},
                      b"text=hello")
    events = run(collect(app, r, origin(Scope.OPERATE), auth=auth))
    assert events == [Refused("not_authorized")]
    assert "hello" in store.load(ws, tid)[1].section("Log")  # applied all the same


def test_a_hung_authorization_check_counts_as_no(app):
    async def hung():
        await asyncio.sleep(60)

    t0 = time.monotonic()
    events = run(collect(app, get("/"), auth=hung, limits=Limits(auth_timeout=0.2)))
    assert events == [Refused("not_authorized")] and time.monotonic() - t0 < 3


def test_an_abandoned_iterator_is_cut_by_the_watchdog(app):
    async def main():
        before = set(extra_tasks())
        # max_seconds leaves room to read the first two events on a busy machine (0.2 s was cut before the second
        # one under parallel CI load); then the consumer holds the iterator and never asks again
        stream = dispatch(app, get("/events"), origin(Scope.LOOK), still_authorized=yes,
                          limits=Limits(max_seconds=1.0, grace_seconds=0.2))
        await stream.__anext__()
        await stream.__anext__()
        assert subscriber_count(app.state.ws) == 1
        deadline = time.monotonic() + 5.0  # the watchdog cuts it after about 1.2 s; wait for that, not a fixed time
        while subscriber_count(app.state.ws) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)  # let the cut task finish
        subs, leaked = subscriber_count(app.state.ws), set(extra_tasks()) - before
        await stream.aclose()
        return subs, leaked

    subs, leaked = run(main())
    assert subs == 0 and not leaked


def test_on_error_hears_the_exception_and_the_device_gets_nothing(app):
    async def boom(scope, receive, send):
        raise ValueError("secret detail")

    boom.state, boom.user_middleware = app.state, app.user_middleware
    heard = []

    async def main():
        return [e async for e in dispatch(boom, get("/"), origin(), still_authorized=yes, on_error=heard.append)]

    assert run(main()) == [Refused("error")] and isinstance(heard[0], ValueError)

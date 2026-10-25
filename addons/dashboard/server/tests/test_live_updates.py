import asyncio
from contextlib import suppress
from unittest.mock import Mock

import pytest

from orch_dashboard.auth import Auth
from orch_dashboard.errors import HostError, fail
from orch_dashboard.live import Live, Tab, WorkspaceLive


def authenticated():
    auth = Auth()
    token = auth.exchange(auth.launch(43210)["code"], "local")[0]["token"]
    return auth, token


def test_auth_and_head(client, token, stub):
    assert client.get(f"/api/events?ws={stub.ws}").status_code == 401
    assert client.get(f"/api/events?ws={stub.ws}&token={token}").status_code == 401
    assert client.get(f"/api/w/{stub.ws}/head", headers={"Authorization": f"Bearer {token}"}).json()["sub_cursor"]


def test_snapshot_reset_and_reopen():
    async def run():
        link = Mock()
        calls = []

        def call(op, args=None):
            calls.append((op, args))
            return {"sub_cursor": "snapshot"} if op == "ticket.query" else {"sub": "sub_a"}

        link.call.side_effect = call
        frames = [{"sub": "sub_a", "items": [], "end": "reset"}]
        link.next_event.side_effect = lambda: frames.pop(0) if frames else None
        auth, token = authenticated()
        live = Live(Mock(enabled=Mock(return_value=link)), auth)
        w, tab = live.attach("ws", token)
        for _ in range(100):
            if sum(op == "sub.open" for op, _ in calls) >= 2:
                break
            await asyncio.sleep(0.01)
        assert [op for op, _ in calls][:7] == [
            "ticket.query",
            "needs",
            "sub.open",
            "sub.close",
            "ticket.query",
            "needs",
            "sub.open",
        ]
        assert calls[2][1]["after"] == "snapshot"
        for _ in range(100):  # the resync frame is queued by the worker thread, slightly after the second sub.open
            if tab.queue.qsize() >= 2:
                break
            await asyncio.sleep(0.01)
        assert [tab.queue.get_nowait()[0] for _ in range(tab.queue.qsize())] == ["ready", "resync"]
        await live.close()

    asyncio.run(run())


def test_resume_cap_slow_drop_and_logout():
    async def run():
        auth, token = authenticated()
        live = Live(Mock(), auth)
        w = WorkspaceLive(live, "ws")
        live.workspaces["ws"] = w
        w.ready = True
        w.task = asyncio.create_task(asyncio.sleep(100))
        _, same = live.attach("ws", token, w.latest)
        assert same.queue.get_nowait()[0] == "ready"
        _, gap = live.attach("ws", token, "old-instance:4")
        assert [gap.queue.get_nowait()[0], gap.queue.get_nowait()[0]] == ["ready", "resync"]
        for _ in range(14):
            live.attach("ws", token)
        with pytest.raises(HostError, match="quota.host"):
            live.attach("ws", token)
        for _ in range(257):
            w.emit("items", [])
        assert same.dropped and same.queue.qsize() == 1
        stream = live.stream(w, same)
        assert "event: resync" in await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        auth.logout(token)
        stream = live.stream(w, gap)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await live.close()

    asyncio.run(run())


class FakeLink:
    """Counts host calls. `visible` is the set of ticket keys this person may read."""

    def __init__(self, visible=("KNOWN-0001",), fail_with=None):
        self.visible, self.fail_with, self.calls = set(visible), fail_with, []
        self.welcome = {"peer": {"person": "p"}}
        self.uids = {}

    def supports(self, op, cap="ops/1"):
        return False  # no run/1 here (M7a's run.sub is covered in test_terminals_host.py)

    def call(self, op, args=None):
        self.calls.append(op)
        if self.fail_with:
            raise self.fail_with
        if op == "ticket.get":
            if args["ref"] not in self.visible:
                raise fail("not_found", exit=2)
            return {"doc": {"uid": self.uids.get(args["ref"], "u")}}
        return {
            "member.list": {"members": [{"person": "p"}]},
            "addon.list": {"addons": []},
            "ticket.query": {"rows": [], "next_page": None},
        }[op]


def fake_live(link):
    gateway = Mock()
    gateway.enabled.side_effect = lambda ws: (gateway.resolved.append(ws), link)[1]
    gateway.resolved = []
    auth, token = authenticated()
    return Live(gateway, auth), gateway, auth, token


def test_visibility_at_drain_and_gone():
    async def run():
        link = FakeLink()
        live, gateway, auth, token = fake_live(link)
        w = WorkspaceLive(live, "ws")
        tab = Tab(token)
        w.tabs.add(tab)
        w.emit(
            "items", [{"id": "1.e", "uid": "hidden", "key": "SECRET-0001"}, {"id": "1.g", "uid": "known", "gone": True}]
        )
        stream = live.stream(w, tab)
        result = await anext(stream)
        assert "SECRET" not in result and '"gone":true' in result
        link.fail_with = fail("peer.denied")
        live.invalidate("ws")
        w.emit("items", [{"id": "2.e", "uid": "x", "key": "SECRET-0002"}])
        assert "event: revoked" in await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(run())


def test_no_empty_items_event():
    async def run():
        live, gateway, auth, token = fake_live(FakeLink())
        w = WorkspaceLive(live, "ws")
        tab = Tab(token)
        w.tabs.add(tab)
        w.emit("items", [{"id": "1.e", "uid": "hidden", "key": "SECRET-0001"}])
        w.emit("items", [{"id": "2.e", "uid": "u", "key": "KNOWN-0001"}])
        frame = await anext(live.stream(w, tab))
        assert "KNOWN-0001" in frame and "id: " in frame and '"SECRET' not in frame
        assert "[]" not in frame

    asyncio.run(run())


def test_host_calls_do_not_grow_with_tabs():
    async def drain(tabs):
        link = FakeLink()
        live, gateway, auth, token = fake_live(link)
        w = WorkspaceLive(live, "ws")
        streams = []
        for _ in range(tabs):
            tab = Tab(token)
            w.tabs.add(tab)
            streams.append(live.stream(w, tab))
        w.emit("items", [{"id": "1.e", "uid": "u", "key": "KNOWN-0001"}, {"id": "1.n", "type": "needs.changed"}])
        frames = await asyncio.gather(*(anext(s) for s in streams))
        assert all("KNOWN-0001" in f for f in frames)
        return list(link.calls), gateway.resolved

    one = asyncio.run(drain(1))
    many = asyncio.run(drain(8))
    assert one == many
    calls, resolved = many
    assert calls.count("ticket.get") == 1 and "ticket.query" not in calls  # only the batch's tickets
    assert len(resolved) == 1  # host link resolved once per check


def test_membership_change_invalidates_shared_cache():
    live, gateway, auth, token = fake_live(FakeLink())
    live.checked("ws", [{"id": "1", "uid": "u", "key": "KNOWN-0001"}])
    first = len(gateway.resolved)
    live.checked("ws", [{"id": "2", "uid": "u", "key": "KNOWN-0001"}])
    assert len(gateway.resolved) == first
    w = WorkspaceLive(live, "ws")
    w.partial = False
    w.admit([{"id": "3", "type": "member.removed"}])
    live.checked("ws", [{"id": "4", "uid": "u", "key": "KNOWN-0001"}])
    assert len(gateway.resolved) == first + 1


def test_every_host_gone_is_forwarded():
    live, *_ = fake_live(FakeLink())
    w = WorkspaceLive(live, "ws")
    w.partial = False
    w.visible = {"seen"}
    # A ticket never checked (e.g. the check failed with host.unavailable) must still be removed on the client.
    out = w.admit([{"id": "1.g", "uid": "seen", "gone": True}, {"id": "2.g", "uid": "unseen", "gone": True}])
    assert [i["uid"] for i in out] == ["seen", "unseen"] and not w.visible


def test_ticket_get_must_answer_for_the_same_uid():
    link = FakeLink()
    link.uids["KNOWN-0001"] = "other-uid"
    live, *_ = fake_live(link)
    assert live.checked("ws", [{"id": "1", "uid": "u", "key": "KNOWN-0001"}]) == []


def test_cache_dropped_on_host_error_and_reopen():
    link = FakeLink()
    live, gateway, auth, token = fake_live(link)
    live.checked("ws", [{"id": "1", "uid": "u", "key": "KNOWN-0001"}])
    assert "ws" in live.cache
    link.fail_with = fail("host.unavailable")
    live.cache["ws"]["at"] = 0  # stale entry forces a host call, which fails
    with pytest.raises(HostError):
        live.checked("ws", [{"id": "2", "uid": "u", "key": "KNOWN-0001"}])
    assert "ws" not in live.cache
    link.fail_with = None
    live.checked("ws", [{"id": "3", "uid": "u", "key": "KNOWN-0001"}])
    assert "ws" in live.cache
    live.checked("ws", [{"id": "4", "uid": "u", "key": "KNOWN-0001"}])
    assert "ws" in live.cache
    w = WorkspaceLive(live, "ws")
    gateway.enabled.side_effect = None
    gateway.enabled.return_value = Mock(
        call=Mock(side_effect=lambda op, args=None: {"rows": [], "next_page": None, "sub_cursor": "c", "sub": "s"})
    )
    w.snapshot_open()
    assert "ws" not in live.cache


def test_host_outage_is_resync_not_revoked(monkeypatch):
    """Heartbeat path: unavailable host retries with backoff; the tab stays alive."""
    import orch_dashboard.live as module

    monkeypatch.setattr(module, "HEARTBEAT", 0.0)
    monkeypatch.setattr(module, "BACKOFF", (0.01,))

    async def run():
        link = FakeLink()
        link.call = Mock(
            side_effect=lambda op, args=None: {
                "sub_cursor": "c",
                "sub": "s",
                "rows": [],
                "next_page": None,
                "items": [],
            }
        )
        live, gateway, auth, token = fake_live(link)
        down = {"on": False}

        def check(ws, items):
            if down["on"]:
                raise fail("host.unavailable")
            return items

        live.checked = check
        link.next_event = lambda: None
        w, tab = live.attach("ws", token)
        for _ in range(50):
            if tab.queue.qsize():
                break
            await asyncio.sleep(0.01)
        assert tab.queue.get_nowait()[0] == "ready"
        down["on"] = True
        await asyncio.sleep(0.2)
        down["on"] = False
        await asyncio.sleep(0.2)
        events = []
        while not tab.queue.empty():
            events.append(tab.queue.get_nowait()[0])
        assert "revoked" not in events and events.count("resync") >= 2 and not w.task.done()
        await live.close()

    asyncio.run(run())


def test_live_timing_with_stub(stub, p1, gateway):
    """An append reaches six streams fast, with host calls independent of the stream count."""
    import time

    from orch_dashboard.hostlink import HostLink

    async def run(count):
        auth, token = authenticated()
        live = Live(gateway, auth)
        uid = p1.new_ticket(f"Timing {count}")
        streams = []
        for _ in range(count):
            w, tab = live.attach(stub.ws, token)
            streams.append(live.stream(w, tab))
        for stream in streams:
            assert "event: ready" in await asyncio.wait_for(anext(stream), 5)
        counted = []
        original = HostLink.call
        HostLink.call = lambda self, op, args=None, **kw: (counted.append(op), original(self, op, args, **kw))[1]
        try:
            p1.tick()
            start = time.monotonic()
            p1.log(uid, "hello")
            frames = await asyncio.wait_for(asyncio.gather(*(next_items(s) for s in streams)), 10)
            elapsed = time.monotonic() - start
        finally:
            HostLink.call = original
        assert all("items" in f for f in frames)
        await live.close()
        return elapsed, counted

    async def next_items(stream):
        while True:
            frame = await anext(stream)
            if "event: items" in frame:
                return frame

    t1, c1 = asyncio.run(run(1))
    t6, c6 = asyncio.run(run(6))
    print(f"timing 1 stream {t1:.2f}s 6 streams {t6:.2f}s")
    assert t6 < 2.0, (t1, t6, c1, c6)
    assert c6.count("ticket.get") <= 2 and "ticket.query" not in c6 and len(c6) <= len(c1) + 2


def test_one_subscription_per_workspace_and_item_identity():
    async def run():
        auth, token = authenticated()
        link = Mock()
        link.call.return_value = {"sub": "sub_a", "sub_cursor": "cursor"}
        frames = [
            {"sub": "sub_a", "items": [{"id": "1.e", "jpos": 1}, {"id": "1.n", "jpos": 1}]},
            {"sub": "sub_a", "items": [{"id": "1.e", "jpos": 1}]},
        ]
        link.next_event.side_effect = lambda: frames.pop(0) if frames else None
        live = Live(Mock(enabled=Mock(return_value=link)), auth)
        w, tab = live.attach("ws", token)
        w2, tab2 = live.attach("ws", token)
        assert w is w2
        for _ in range(100):
            if tab.queue.qsize() >= 2:
                break
            await asyncio.sleep(0.01)
        assert tab.queue.get_nowait()[0] == "ready"
        assert len(tab.queue.get_nowait()[1]) == 2
        assert sum(c.args[0] == "sub.open" for c in link.call.call_args_list) == 1
        await live.close()
        with suppress(asyncio.CancelledError):
            await w.task

    asyncio.run(run())


def test_asgi_stream_ready_auth_headers_and_logout(gateway):
    """Exercise actual routing/middleware/StreamingResponse without an HTTP listener."""
    from orch_dashboard.app import create_app

    async def run():
        auth, token = authenticated()
        app = create_app(43210, gateway=gateway, auth=auth)
        ws = next(iter(gateway._pins()))
        messages = []
        sent = False
        disconnect = asyncio.Event()

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if b"event: ready" in message.get("body", b""):
                auth.logout(token)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "query_string": f"ws={ws}".encode(),
            "root_path": "",
            "headers": [(b"host", b"127.0.0.1:43210"), (b"authorization", f"Bearer {token}".encode())],
            "server": ("127.0.0.1", 43210),
            "client": ("127.0.0.1", 1234),
        }
        try:
            await asyncio.wait_for(app(scope, receive, send), 5)
            start = messages[0]
            assert start["status"] == 200
            headers = dict(start["headers"])
            assert headers[b"content-type"].startswith(b"text/event-stream")
            assert headers[b"cache-control"] == b"no-store"
            assert b"content-security-policy" in headers
            assert any(b"event: ready" in m.get("body", b"") for m in messages)
            assert not app.state.live.workspaces[ws].tabs
        finally:
            await app.state.live.close()

    asyncio.run(run())


def test_logout_interrupts_blocked_send():
    from orch_dashboard.live import SessionStream

    async def run():
        auth, token = authenticated()
        closed = asyncio.Event()

        async def body():
            try:
                yield "event: ready\ndata: {}\n\n"
            finally:
                closed.set()

        async def send(message):
            if message["type"] == "http.response.body":
                auth.logout(token)
                await asyncio.Event().wait()

        async def receive():
            await asyncio.Event().wait()

        response = SessionStream(body(), auth, token)
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), 1)
        assert closed.is_set()

    asyncio.run(run())


def test_cancel_during_open_closes_late_subscription():
    import threading

    async def run():
        started, finish = threading.Event(), threading.Event()
        link = Mock()

        def call(op, args=None):
            if op == "ticket.query":
                return {"sub_cursor": "snapshot"}
            if op == "sub.open":
                started.set()
                finish.wait(2)
                return {"sub": "sub_late"}
            return {}

        link.call.side_effect = call
        auth, token = authenticated()
        live = Live(Mock(enabled=Mock(return_value=link)), auth)
        w, _ = live.attach("ws", token)
        await asyncio.to_thread(started.wait, 2)
        assert started.is_set()
        w.task.cancel()
        await asyncio.sleep(0)
        finish.set()
        with suppress(asyncio.CancelledError):
            await w.task
        link.call.assert_any_call("sub.close", {"sub": "sub_late"})

    asyncio.run(run())


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("peer.denied", "revoked"),
        ("addon.disabled", "revoked"),
        ("not_found", "revoked"),
        ("host.unavailable", "resync"),
        ("timeout", "resync"),
        ("conn.overloaded", "resync"),
    ],
)
def test_only_real_revocation_is_revoked(code, expected):
    async def run():
        live, gateway, auth, token = fake_live(FakeLink(fail_with=fail(code)))
        w = WorkspaceLive(live, "ws")
        tab = Tab(token)
        w.tabs.add(tab)
        w.emit("items", [{"id": "1.e", "uid": "u", "key": "KNOWN-0001"}])
        assert f"event: {expected}" in await anext(live.stream(w, tab))

    asyncio.run(run())

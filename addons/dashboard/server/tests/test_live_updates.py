import asyncio
from contextlib import suppress
from unittest.mock import Mock

import pytest

from orch_dashboard.auth import Auth
from orch_dashboard.errors import HostError, fail
from orch_dashboard.live import Live, Tab, WorkspaceLive


def authenticated():
    auth = Auth()
    token = auth.exchange(auth.launch(43210)["code"])[0]["token"]
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


def test_visibility_at_drain_and_gone():
    async def run():
        auth, token = authenticated()
        gateway = Mock()

        def call(ws, op, args=None):
            return {
                "whoami": {"person": "p"},
                "member.list": {"members": [{"person": "p"}]},
                "ticket.query": {"rows": [], "next_page": None},
            }[op]

        gateway.call.side_effect = call
        live = Live(gateway, auth)
        w = WorkspaceLive(live, "ws")
        tab = Tab(token)
        w.tabs.add(tab)
        w.emit(
            "items", [{"id": "1.e", "uid": "hidden", "key": "SECRET-0001"}, {"id": "1.g", "uid": "known", "gone": True}]
        )
        stream = live.stream(w, tab)
        result = await anext(stream)
        assert "SECRET" not in result and '"gone":true' in result
        gateway.call.side_effect = fail("peer.denied")
        w.emit("items", [{"id": "2.e", "key": "SECRET-0002"}])
        assert "event: revoked" in await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(run())


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

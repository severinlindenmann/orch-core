"""R3 T4 (orch-core #1591; spec D89, contract 10.2c "Inside a session"): the in-process stream adapter. Frames in, the
app's answer frames out, no socket. The real app answers where the route matters; a spy app where only the
scope or the timing matters."""

# ruff: noqa: F811

import asyncio
import json
import threading
import time

from adapter_kit import (
    DEVICE,
    OTHER,
    PERSON,
    STREAM,
    App,
    Wire,
    head,
    spy_app,
    ws_head,
)
from device_kit import app, click_start, fake, passkey_store, ws  # noqa: F401

from orch_dashboard import remote_device as rd
from orch_dashboard.errors import HostError
from orch_dashboard.remote_device import BODY, END, HEAD, RESET, WS_CLOSE, WS_OPEN, WS_TEXT, frame
from orch_dashboard.remote_identity import REMOTE_STREAM, RemoteIdentity


def run(coro):
    return asyncio.run(coro)


# --- the round trip --------------------------------------------------------------------------------------------------
def test_a_get_goes_through_the_real_app_and_answers_frames(app, auth, ws):
    async def main():
        w = Wire(app, auth, ws)
        w.open()
        w.request(1, "GET", "/api/workspaces")
        await w.until(lambda: w.response(1) is not None)
        status, headers, body = w.response(1)
        assert status == 200 and json.loads(body)[0]["id"] == ws
        assert "set-cookie" not in headers and "content-length" not in headers

    run(main())


def test_the_app_is_called_with_the_stream_scope_and_nothing_from_the_device(app, auth, ws):
    seen = []

    async def main():
        w = Wire(App(spy_app(seen), auth), auth, ws)
        w.open()
        w.request(1, "POST", "/api/w/x/y", "a=1", [("content-type", "application/json")], b"{}")
        await w.until(lambda: w.response(1) is not None)
        (scope,) = seen
        assert scope["orch_listener"] == "remote-device" and scope["scheme"] == "https"
        assert (b"host", b"orch-remote") in scope["headers"] and not [k for k, _ in scope["headers"] if k == b"origin"]
        assert scope["query_string"] == b"a=1" and scope["raw_path"] == b"/api/w/x/y"
        assert scope["state"]["remote"] == RemoteIdentity(STREAM, ws, DEVICE, PERSON, "work")
        assert auth.stream_of(scope["state"]["token"]) == STREAM
        assert scope["received"] == b"{}"
        # the token never appears in a frame the device gets
        raw = b"".join(p for items in w.frames.values() for _, p in items)
        assert scope["state"]["token"].encode() not in raw

    run(main())


def test_the_contextvar_is_set_for_the_whole_call(app, auth, ws):
    got = []

    async def inner(scope, receive, send):
        got.append(REMOTE_STREAM.get())
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def main():
        w = Wire(App(inner, auth), auth, ws)
        w.open()
        w.request(1, "GET", "/x")
        await w.until(lambda: w.response(1) is not None)
        assert got == [STREAM] and REMOTE_STREAM.get() is None

    run(main())


# --- I1: identity comes from the host's frame only --------------------------------------------------------------------
def test_identity_from_host_frame_only(app, auth, ws):
    seen = []

    async def main():
        w = Wire(App(spy_app(seen), auth), auth, ws)
        w.open()
        hdrs = [("x-level", "terminals"), ("x-person", "p_" + "9" * 32), ("x-forwarded-user", "owner")]
        w.request(1, "GET", "/api/w/x/today", headers=hdrs, level="view")
        await w.until(lambda: w.response(1) is not None)
        assert seen[0]["state"]["remote"].level == "view" and seen[0]["state"]["remote"].person == PERSON
        # a frame whose identity names another person or workspace closes the stream (the host never does that)
        w.send(HEAD, 3, head("GET", "/x"), identity=w.identity(person="p_" + "9" * 32))
        await w.until(lambda: w.closes)
        assert w.closes == [(STREAM, "malformed")]

    run(main())


def test_the_real_app_refuses_a_write_at_view_whatever_the_headers_say(app, auth, ws):
    async def main():
        w = Wire(app, auth, ws)
        w.open()
        w.request(
            1, "POST", f"/api/w/{ws}/wiki/create", headers=[("content-type", "application/json"), ("x-level", "work")],
            body=b"{}", level="view",
        )  # fmt: skip
        await w.until(lambda: w.response(1) is not None)
        status, _, body = w.response(1)
        assert status == 403 and json.loads(body)["error"]["code"] == "remote.level"

    run(main())


# --- limits and errors ------------------------------------------------------------------------------------------------
def test_bad_frame_closes_malformed(app, auth, ws):
    async def main():
        for bad in (b"{", b'{"headers":[],"method":"GET","path":"/x"}', head("TRACE", "/x"), head("GET", "/a/../b"),
                    head("GET", "/x", "", [("Host", "x")]), head("GET", "/x", "", [("content-length", "1")]),
                    head("GET", "/x", "", [("origin", "x")]), head("GET", "/x", "a b")):  # fmt: skip
            w = Wire(app, auth, ws)
            w.open()
            token = w.term.streams[STREAM].token
            w.send(HEAD, 1, bad)
            await w.until(lambda: w.closes)
            assert w.closes == [(STREAM, "malformed")] and STREAM not in w.term.streams, bad
            await w.settle()
            assert auth.stream_of(token) is None  # nothing it minted works any more
            auth.drop_stream(STREAM)

    run(main())


def test_socket_frames_before_the_101_or_on_a_plain_request_close_malformed(app, auth, ws):
    async def main():
        w = Wire(App(spy_app([]), auth), auth, ws)
        w.open()
        w.send(WS_OPEN, 1, ws_head("/term"))
        w.send(WS_TEXT, 1, b"early")  # the app has not accepted: no 101 has left
        await w.until(lambda: w.closes)
        assert w.closes == [(STREAM, "malformed")]
        w2 = Wire(App(spy_app([]), auth), auth, ws)
        w2.open(OTHER)
        w2.request(1, "GET", "/x", sid=OTHER, end=False)
        w2.send(WS_TEXT, 1, b"x", sid=OTHER)  # a socket frame on a plain request
        await w2.until(lambda: w2.closes)
        assert w2.closes == [(OTHER, "malformed")]

    run(main())


def test_a_frame_that_is_not_base64_or_too_short_closes_malformed(app, auth, ws):
    async def main():
        w = Wire(app, auth, ws)
        w.open()
        w.term.handle({"t": "frame", "stream_id": STREAM, "bytes": "!!"})
        await w.until(lambda: w.closes)
        assert w.closes == [(STREAM, "malformed")]

    run(main())


def test_exchange_and_ws_limits_busy(app, auth, ws):
    seen = []

    async def main():
        w = Wire(App(spy_app(seen), auth), auth, ws)
        w.open()
        for i in range(1, 9):  # eight requests waiting for their END
            w.request(i, "POST", "/x", end=False)
        w.request(9, "POST", "/x", end=False)
        await w.until(lambda: w.frames[(STREAM, 9)])
        assert w.frames[(STREAM, 9)] == [(RESET, b"remote.busy")]
        assert seen == []  # nothing ran: none of them is complete
        # WebSockets: two open, the third is busy
        w2 = Wire(App(spy_app([]), auth), auth, ws)
        w2.open(OTHER)
        for i in (1, 2, 3):
            w2.send(WS_OPEN, i, ws_head("/term"), sid=OTHER)
        await w2.until(lambda: w2.frames[(OTHER, 3)])
        assert w2.frames[(OTHER, 3)] == [(RESET, b"remote.busy")]
        # a finished exchange frees its place
        w.send(END, 1)
        await w.until(lambda: w.response(1) is not None)
        w.request(10, "GET", "/x")
        await w.until(lambda: w.response(10) is not None)

    run(main())


def test_a_body_over_one_mib_is_reset_too_large(app, auth, ws):
    seen = []

    async def main():
        w = Wire(App(spy_app(seen), auth), auth, ws)
        w.open()
        w.send(HEAD, 1, head("POST", "/x"))
        for _ in range((1 << 20) // 65000 + 2):
            w.send(BODY, 1, b"x" * 65000)
        w.send(END, 1)
        await w.until(lambda: w.frames[(STREAM, 1)])
        assert w.frames[(STREAM, 1)] == [(RESET, b"too_large")] and seen == []

    run(main())


def test_a_request_without_its_end_is_reset_after_the_timeout(app, auth, ws):
    seen = []

    async def main():
        w = Wire(App(spy_app(seen), auth), auth, ws, request_s=0.1)
        w.open()
        w.request(1, "POST", "/x", body=b"half", end=False)
        await w.until(lambda: w.frames[(STREAM, 1)])
        assert w.frames[(STREAM, 1)] == [(RESET, b"timeout")]
        w.send(END, 1)  # a late END of a reset exchange is dropped
        await w.settle()
        assert seen == [] and w.closes == []

    run(main())


def test_stream_closed_before_request_end_never_dispatches_body(app, auth, ws):
    seen = []

    async def main():
        w = Wire(App(spy_app(seen), auth), auth, ws)
        w.open()
        token = w.term.streams[STREAM].token
        w.request(1, "POST", "/api/w/x/agents/start", body=b'{"ticket":"A"}', end=False)
        w.closed()  # the client went away: the route never acts on a partial request
        w.send(END, 1)
        await w.settle()
        assert seen == [] and w.frames == {} or not any(w.frames.values())
        assert auth.stream_of(token) is None and STREAM not in w.term.streams

    run(main())


def test_remote_device_other_workspace_path_refused(app, auth, ws):
    async def main():
        w = Wire(app, auth, ws)
        w.open()
        other = "0" * 32
        for n, path in enumerate((f"/api/w/{other}/today", f"/api/w/{other}/runs/r_x/attach"), 1):
            w.request(n, "GET", path)
        w.request(4, "GET", "/api/events", "ws=" + other)
        w.request(5, "GET", f"/api/w/{ws}/today")
        await w.until(lambda: all(w.response(n) for n in (1, 2, 4, 5)))
        for n in (1, 2, 4):
            assert w.response(n)[0] == 404, n
        assert w.response(5)[0] != 404

    run(main())


def test_outside_bearers_are_answered_400_and_the_stream_stays(app, auth, ws):
    seen = []

    async def main():
        w = Wire(App(spy_app(seen), auth), auth, ws)
        w.open()
        for n, header in enumerate(
            (("authorization", "Bearer x"), ("cookie", "a=b"), ("x-setup-token", "t"), ("x-orch-anything", "1")), 1
        ):
            w.request(n, "GET", "/x", headers=[header])
        w.send(WS_OPEN, 9, ws_head("/term", headers=[("cookie", "a=b")]))
        await w.until(lambda: all(w.response(n) for n in (1, 2, 3, 4, 9)))
        for n in (1, 2, 3, 4, 9):
            status, _, body = w.response(n)
            assert status == 400 and json.loads(body)["error"]["code"] == "remote.bearer"
        assert seen == [] and w.closes == []  # refused, never stripped; the stream stays
        w.request(10, "GET", "/x")
        await w.until(lambda: w.response(10) is not None)

    run(main())


def test_set_cookie_never_reaches_the_device(app, auth, ws):
    async def main():
        hdrs = [(b"set-cookie", b"a=b"), (b"content-length", b"2"), (b"X-Thing", b"v"), (b"bad", b"line\nbreak")]
        w = Wire(App(spy_app([], headers=hdrs), auth), auth, ws)
        w.open()
        w.request(1, "GET", "/x")
        await w.until(lambda: w.response(1) is not None)
        _, headers, _ = w.response(1)
        assert headers == {"content-type": "text/plain", "x-thing": "v"}

    run(main())


# --- streaming --------------------------------------------------------------------------------------------------------
def test_sse_streams_through(app, auth, ws):
    async def main():
        release = asyncio.Event()

        async def sse(scope, receive, send):
            await receive()
            await send(
                {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]}
            )
            await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
            await release.wait()
            await send({"type": "http.response.body", "body": b"data: 2\n\n" + b"x" * 70000, "more_body": False})

        w = Wire(App(sse, auth), auth, ws)
        w.open()
        w.request(1, "GET", "/api/events")
        await w.until(lambda: BODY in w.kinds(1))
        assert w.kinds(1) == [HEAD, BODY]  # the first event left before the response ended
        assert w.term.active == 1
        release.set()
        await w.until(lambda: w.response(1) is not None)
        body = w.response(1)[2]
        assert body.startswith(b"data: 1\n\ndata: 2\n\n") and len(body) == 9 + 9 + 70000
        assert all(len(p) <= rd.PAYLOAD_MAX for k, p in w.frames[(STREAM, 1)] if k == BODY)
        assert w.term.active == 0

    run(main())


def test_a_device_that_stops_reading_closes_the_stream_busy(app, auth, ws):
    async def main():
        async def flood(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            for _ in range(200):
                await send({"type": "http.response.body", "body": b"y", "more_body": True})

        w = Wire(App(flood, auth), auth, ws, busy_s=0.1)
        w.open()
        w.request(1, "GET", "/x")
        end = time.monotonic() + 3
        while not w.term.closes and time.monotonic() < end:  # nobody takes the frames
            await asyncio.sleep(0.02)
        w.pump()
        assert w.closes == [(STREAM, "remote.busy")] and STREAM not in w.term.streams
        assert len(w.frames[(STREAM, 1)]) <= rd.OUT_MAX + 1

    run(main())


# --- WebSocket --------------------------------------------------------------------------------------------------------
def test_websocket_text_round_trip_and_close(app, auth, ws):
    async def main():
        w = Wire(App(spy_app([]), auth), auth, ws)
        w.open()
        w.send(WS_OPEN, 1, ws_head("/term"))
        await w.until(lambda: w.kinds(1) == [HEAD])
        assert json.loads(w.frames[(STREAM, 1)][0][1]) == {"headers": [], "status": 101}
        w.send(WS_TEXT, 1, b"hello")
        await w.until(lambda: WS_TEXT in w.kinds(1))
        assert w.frames[(STREAM, 1)][-1] == (WS_TEXT, b"hello")
        w.send(WS_CLOSE, 1, (1000).to_bytes(2, "big"))
        await w.until(lambda: WS_CLOSE in w.kinds(1))
        assert w.frames[(STREAM, 1)][-1] == (WS_CLOSE, (1000).to_bytes(2, "big"))
        await w.settle()
        assert 1 not in w.term.streams[STREAM].exchanges  # both closed: the exchange is over

    run(main())


def test_websocket_refused_by_the_app_is_an_ordinary_403(app, auth, ws):
    async def main():
        w = Wire(app, auth, ws)  # the real app: a work-level stream has no /term
        w.open()
        w.send(WS_OPEN, 1, ws_head("/term"))
        await w.until(lambda: w.response(1) is not None)
        assert w.response(1)[0] == 403

    run(main())


# --- closing ----------------------------------------------------------------------------------------------------------
def test_closed_stream_cancels_inflight_and_drops_tokens(app, auth, ws):
    state = {}

    async def main():
        async def slow(scope, receive, send):
            await receive()
            state["token"] = scope["state"]["token"]
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise

        w = Wire(App(slow, auth), auth, ws)
        w.open()
        w.request(1, "GET", "/x")
        w.send(WS_OPEN, 2, ws_head("/term"))
        await asyncio.sleep(0.05)
        assert auth.stream_of(state["token"]) == STREAM
        w.closed()
        await w.settle()
        assert state.get("cancelled") is True
        assert auth.stream_of(state["token"]) is None and not auth.valid(state["token"])
        assert STREAM not in w.term.streams and w.term.active == 0
        assert not any(t for t in w.term._tasks if not t.done())
        w.send(HEAD, 5, head("GET", "/x"))  # a frame of a closed stream is dropped
        await w.settle()
        assert w.closes == [] and not w.frames.get((STREAM, 5))

    run(main())


def test_streams_are_independent(app, auth, ws):
    seen = []

    async def main():
        w = Wire(App(spy_app(seen), auth), auth, ws)
        w.open(STREAM)
        w.open(OTHER)
        w.request(1, "GET", "/x", sid=STREAM)
        w.request(1, "GET", "/x", sid=OTHER)
        await w.until(lambda: w.response(1, STREAM) and w.response(1, OTHER))
        tokens = {s["state"]["token"] for s in seen}
        assert len(tokens) == 2
        w.closed(STREAM)
        await w.settle()
        assert auth.stream_of(seen[1]["state"]["token"]) == OTHER

    run(main())


def test_a_second_open_of_a_live_stream_changes_nothing(app, auth, ws):
    async def main():
        w = Wire(app, auth, ws)
        w.open()
        token = w.term.streams[STREAM].token
        w.open()
        assert w.term.streams[STREAM].token == token

    run(main())


# --- the host connection ----------------------------------------------------------------------------------------------
class FakeLink:
    """The host end of `remote.attach`: frames it will hand out, and what the adapter sent back."""

    def __init__(self):
        self.mu = threading.Condition()
        self.frames = []
        self.sent = []
        self.closed_by_adapter = []
        self.calls = 0
        self.lapse = False
        self.closed = False

    def push(self, *msgs):
        with self.mu:
            self.frames += msgs
            self.mu.notify_all()

    def call(self, op, args):
        assert op == "remote.attach"
        with self.mu:
            self.calls += 1
            if self.lapse:
                self.lapse = False
                raise HostError({"code": "invalid.input", "message": "attach again", "retryable": False}, 5)
            self.sent += args["send"]
            self.closed_by_adapter += args["close"]
            if not self.frames and args["wait_ms"]:
                self.mu.wait(args["wait_ms"] / 1000)
            out, self.frames = self.frames[:8], self.frames[8:]
        return {"attach_id": "at_" + "0" * 16, "frames": out}

    def close(self):
        self.closed = True


def test_the_poll_hands_frames_to_the_app_and_takes_the_answers_back(app, auth, ws):
    from base64 import urlsafe_b64decode

    async def main():
        link = FakeLink()
        term = rd.Terminator(app, auth, asyncio.get_running_loop())
        att = rd.Attachment(ws, term, lambda w: link, wait_ms=200, active_ms=20)
        att.start()
        try:
            open_ = {
                "t": "open", "stream_id": STREAM, "api": 1, "workspace_id": ws, "device_id": DEVICE, "person": PERSON,
                "level": "work",
            }  # fmt: skip
            link.push(open_)
            from orch.crypto.encoding import b64u

            ident = {"workspace_id": ws, "device_id": DEVICE, "person": PERSON, "level": "work"}
            for kind, payload in ((HEAD, head("GET", "/api/workspaces")), (END, b"")):
                link.push({"t": "frame", "stream_id": STREAM, "bytes": b64u(frame(kind, 1, payload)),
                           **({"identity": ident} if kind == HEAD else {})})  # fmt: skip
            end = time.monotonic() + 5
            while time.monotonic() < end:
                with link.mu:
                    kinds = [urlsafe_b64decode(s["bytes"] + "==")[0] for s in link.sent]
                if END in kinds:
                    break
                await asyncio.sleep(0.02)
            assert kinds[0] == HEAD and kinds[-1] == END
            assert {s["stream_id"] for s in link.sent} == {STREAM}
        finally:
            att.stop()
            att.join(3)

    run(main())


def test_a_lapsed_lease_drops_every_stream_and_the_attachment_ends(app, auth, ws):
    async def main():
        link = FakeLink()
        term = rd.Terminator(app, auth, asyncio.get_running_loop())
        att = rd.Attachment(ws, term, lambda w: link, wait_ms=50, active_ms=20)
        link.push({"t": "open", "stream_id": STREAM, "api": 1, "workspace_id": ws, "device_id": DEVICE,
                   "person": PERSON, "level": "work"})  # fmt: skip
        att.start()
        end = time.monotonic() + 3
        while STREAM not in term.streams and time.monotonic() < end:
            await asyncio.sleep(0.02)
        token = term.streams[STREAM].token
        link.lapse = True
        att.join(3)
        assert not att.is_alive() and link.closed
        await asyncio.sleep(0.1)
        assert term.streams == {} and auth.stream_of(token) is None

    run(main())


def test_the_poll_is_short_while_an_exchange_is_open_and_long_when_idle(app, auth, ws):
    waits = []

    class Spy(FakeLink):
        def call(self, op, args):
            waits.append(args["wait_ms"])
            return super().call(op, args)

    async def main():
        link = Spy()
        term = rd.Terminator(app, auth, asyncio.get_running_loop())
        att = rd.Attachment(ws, term, lambda w: link, wait_ms=300, active_ms=30)
        att.start()
        await asyncio.sleep(0.05)
        term.active = 1
        await asyncio.sleep(0.4)
        att.stop()
        att.join(3)
        assert 300 in waits and 30 in waits

    run(main())


def test_the_manager_attaches_one_thread_per_workspace(app, auth, ws, gateway):
    async def main():
        links = []

        def factory(w):
            links.append(w)
            return FakeLink()

        devices = rd.RemoteDevices(app, gateway, link_factory=factory, scan_s=0.05)
        devices.start(asyncio.get_running_loop())
        await asyncio.sleep(0.3)
        await asyncio.to_thread(devices.stop)
        assert links == [ws]  # one per pinned workspace, started once while it lives

    run(main())


def test_the_adapter_imports_no_http_client():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(rd.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    assert not names & {"http", "httpx", "requests", "urllib", "aiohttp", "socket", "urllib3", "websockets", "uvicorn"}


def test_frame_constants_are_the_hosts(app):
    from orch.host.remote import session

    names = ("HEAD", "BODY", "END", "RESET", "WS_OPEN", "WS_TEXT", "WS_BINARY", "WS_CLOSE", "PING", "PONG")
    assert tuple(getattr(rd, n) for n in names) == (
        session.HEAD, session.BODY, session.END, session.RESET, session.WS_OPEN, session.WS_TEXT, session.WS_BINARY,
        session.WS_CLOSE, session.PING, session.PONG,
    )  # fmt: skip
    assert (rd.MAX_HTTP, rd.MAX_WS, rd.OUT_MAX, rd.HEAD_MAX, rd.REQUEST_BODY_MAX) == (
        session.MAX_HTTP,
        session.MAX_WS,
        session.OUT_MAX,
        session.HEAD_MAX,
        session.BODY_MAX,
    )

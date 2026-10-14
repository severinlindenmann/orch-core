"""M7a rules that need no host: the close-code table, the browser tickets, the WebSocket checks before `accept`, the
first message, the output queue, the input sink and the routes' input models (section 12.6.6)."""

import asyncio
import json
import queue

import pytest
from conftest import ORIGIN, PORT
from starlette.testclient import WebSocketDenialResponse, WebSocketDisconnect

from orch_dashboard import terminals
from orch_dashboard.terminals import CLOSE_TABLE, Closed, OutQueue, TermSocket, Tickets, close_code

WS_HEADERS = {"origin": ORIGIN, "host": f"127.0.0.1:{PORT}"}
RUN = "r_01M4Y46B485AXD4CBEJZ15Z1TV"


def headers(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "Content-Type": "application/json"}


# -- the close-code table ---------------------------------------------------------------------------------------------

TABLE = {
    1000: ["exited", "run.stopped", "cancelled"],
    4400: ["protocol", "invalid.input"],
    4401: ["session.ended"],
    4403: ["person.removed", "not_visible", "ticket_invalid", "op.not_allowed", "not_found", "runner.disabled"],
    4409: ["writer_busy", "term.writer_busy"],
    4422: ["term.not_attachable"],
    4429: ["overflow", "rate.limited", "run.limit", "attach.limit", "quota.host"],
    4503: ["runner.lost", "runner.changed", "runner.restarting", "host.restarting", "runner.unavailable",
           "host.unavailable"],
}  # fmt: skip


@pytest.mark.parametrize("code,names", TABLE.items())
def test_close_codes_are_the_spec_table(code, names):
    for name in names:
        assert close_code(name) == code
        assert close_code("host.error", name) == code  # the catalog code behind host.error decides
    assert set(names) <= CLOSE_TABLE[code]


def test_other_codes_are_4500_or_4503_when_retryable():
    assert close_code("internal") == 4500
    assert close_code("host.error", "launch.invalid") == 4500
    assert close_code("host.error", "something.new", retryable=True) == 4503
    assert close_code("host.error", "timeout") == 4503


def test_end_message_shape():
    end = Closed("host.error", code="attach.limit", message="too many", close=4429).end()
    assert end == {"t": "end", "reason": "host.error", "code": "attach.limit", "message": "too many",
                   "retry_after_ms": 1000}  # fmt: skip
    assert Closed("exited").end() == {"t": "end", "reason": "exited"}
    assert Closed("overflow").end()["retry_after_ms"] == 1000


def test_host_refusal_keeps_only_plain_messages():
    from orch_dashboard.errors import HostError

    closed = terminals.host_closed(HostError({"code": "term.not_attachable", "message": "gone‮", "retryable": False}))
    assert closed.close == 4422 and closed.code == "term.not_attachable" and closed.message is None
    closed = terminals.host_closed(
        HostError(
            {"code": "rate.limited", "message": "slow down", "retryable": True, "detail": {"retry_after_ms": 700}}
        )
    )
    assert closed.close == 4429 and closed.end()["retry_after_ms"] == 700 and closed.message == "slow down"


# -- browser tickets --------------------------------------------------------------------------------------------------


def test_ticket_is_single_use_hashed_and_bound():
    tickets = Tickets()
    raw, ttl = tickets.mint("tok", "ws", RUN, "write", True, 5)
    assert ttl <= 30 and len(raw) == 22  # 128 bits, base64url
    assert raw not in json.dumps({k: vars(v) for k, v in tickets.items.items()})  # only its hash is kept
    intent = tickets.redeem(raw)
    assert (intent.token, intent.ws, intent.run, intent.mode, intent.take, intent.from_) == ("tok", "ws", RUN,
                                                                                           "write", True, 5)  # fmt: skip
    assert tickets.redeem(raw) is None  # single use


def test_ticket_expires_and_is_bound_to_the_server_generation():
    now = [1000.0]
    tickets = Tickets(clock=lambda: now[0])
    raw, _ = tickets.mint("tok", "ws", RUN, "read", False, None)
    now[0] += 30.01
    assert tickets.redeem(raw) is None
    raw, _ = tickets.mint("tok", "ws", RUN, "read", False, None)
    tickets.generation = "other"  # a restarted server (a new generation) refuses an older ticket
    assert tickets.redeem(raw) is None


def test_ten_tickets_per_minute_per_session_and_logout_forgets():
    now = [0.0]
    tickets = Tickets(clock=lambda: now[0])
    minted = [tickets.mint("tok", "ws", RUN, "read", False, None) for _ in range(10)]
    assert all(minted) and tickets.mint("tok", "ws", RUN, "read", False, None) is None
    assert tickets.mint("other", "ws", RUN, "read", False, None) is not None
    now[0] += 60.01
    assert tickets.mint("tok", "ws", RUN, "read", False, None) is not None
    tickets.forget("tok")
    assert all(i.token != "tok" for i in tickets.items.values())


def test_attach_route_mints_without_the_host_and_refuses_bad_input(client, token, stub):
    ws = stub.ws
    res = client.post(f"/api/w/{ws}/runs/{RUN}/attach", content="{}", headers=headers(token))
    assert res.status_code == 200 and set(res.json()) == {"ticket", "expires_in_ms"}
    assert res.json()["expires_in_ms"] <= 30000
    assert client.post(f"/api/w/{ws}/runs/bad/attach", content="{}", headers=headers(token)).status_code in (400, 404)
    bad = client.post(f"/api/w/{ws}/runs/{RUN}/attach", content='{"mode":"admin"}', headers=headers(token))
    assert bad.status_code == 400 and "admin" not in bad.text
    extra = client.post(f"/api/w/{ws}/runs/{RUN}/attach", content='{"x":1}', headers=headers(token))
    assert extra.status_code == 400
    assert client.post(f"/api/w/{ws}/runs/{RUN}/attach", content="{}", headers={"Origin": ORIGIN,
                       "Content-Type": "application/json"}).status_code == 401  # fmt: skip
    for _ in range(9):
        client.post(f"/api/w/{ws}/runs/{RUN}/attach", content="{}", headers=headers(token))
    limited = client.post(f"/api/w/{ws}/runs/{RUN}/attach", content="{}", headers=headers(token))
    assert limited.status_code == 503 and limited.json()["error"]["code"] == "rate.limited"


def test_csp_allows_only_this_servers_websocket(client):
    csp = client.get("/").headers["content-security-policy"]
    assert f"connect-src 'self' ws://127.0.0.1:{PORT};" in csp
    assert "script-src 'self';" in csp and "style-src 'self';" in csp and "ws:;" not in csp and "localhost" not in csp


# -- the WebSocket before accept --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,hdrs",
    [
        ("/term", {"host": f"localhost:{PORT}", "origin": ORIGIN}),
        ("/term", {"host": f"127.0.0.1:{PORT}"}),  # no Origin
        ("/term", {"host": f"127.0.0.1:{PORT}", "origin": "null"}),
        ("/term", {"host": f"127.0.0.1:{PORT}", "origin": f"http://localhost:{PORT}"}),
        ("/term", {**WS_HEADERS, "sec-websocket-protocol": "chat"}),
        ("/term?x=1", WS_HEADERS),
        ("/api/term", WS_HEADERS),
    ],
)
def test_websocket_is_refused_before_accept(client, path, hdrs):
    with pytest.raises((WebSocketDisconnect, WebSocketDenialResponse)):
        with client.websocket_connect(path, headers=hdrs):
            pass


def close_of(client, send):
    with client.websocket_connect("/term", headers=WS_HEADERS) as ws:
        send(ws)
        texts = []
        while True:
            msg = ws.receive()
            if msg["type"] == "websocket.close":
                return msg["code"], texts
            texts.append(msg.get("text"))


def test_first_message_rules(client, token, stub):
    assert close_of(client, lambda ws: ws.send_bytes(b"\x00x"))[0] == 4400  # binary before binding
    assert close_of(client, lambda ws: ws.send_text("x" * 5000))[0] == 4400  # over 4 KiB
    assert close_of(client, lambda ws: ws.send_text("not json"))[0] == 4400
    assert close_of(client, lambda ws: ws.send_text('{"ticket":"a","x":1}'))[0] == 4400
    code, texts = close_of(client, lambda ws: ws.send_text('{"ticket":"AAAAAAAAAAAAAAAAAAAAAA"}'))
    assert code == 4401 and texts == []  # refused ticket: no end message
    # A ticket of a logged-out session is refused too.
    raw = client.post(f"/api/w/{stub.ws}/runs/{RUN}/attach", content="{}", headers=headers(token)).json()["ticket"]
    client.post("/auth/logout", headers=headers(token))
    code, texts = close_of(client, lambda ws: ws.send_text(json.dumps({"ticket": raw})))
    assert code == 4401 and texts == []


def test_first_message_must_come_within_five_seconds(client, monkeypatch):
    monkeypatch.setattr(terminals, "FIRST_MESSAGE_S", 0.05)
    assert close_of(client, lambda ws: None)[0] == 4400


# -- the output queue -------------------------------------------------------------------------------------------------


def test_out_queue_drops_oldest_output_into_one_gap_and_keeps_controls():
    async def run():
        q = OutQueue(limit=10)
        q.put_out(0, b"aaaa")
        q.put({"t": "writer", "writer": False})
        q.put_out(4, b"bbbb")
        q.put_out(8, b"cccc")  # 12 > 10: the oldest output (0..4) becomes a gap
        q.put_out(12, b"dddd")  # again: 8..12 dropped... the gap 0..4 sits before the control frame
        items = [await q.get() for _ in range(len(q.items))]
        return items

    items = asyncio.run(run())
    kinds = [i[0] for i in items]
    assert kinds.count("ctl") == 1 and items[kinds.index("ctl")][1]["t"] == "writer"
    gaps = [(i[1], i[2]) for i in items if i[0] == "gap"]
    outs = [i[1] for i in items if i[0] == "out"]
    assert gaps and sum(b - a for a, b in gaps) + 4 * len(outs) == 16 and outs[-1] == 12


def test_out_queue_ends_with_overflow_when_controls_pile_up(monkeypatch):
    monkeypatch.setattr(terminals, "CONTROL_LIMIT", 3)

    async def run():
        q = OutQueue()
        for _ in range(3):
            q.put({"t": "resize", "cols": 80, "rows": 24})
        with pytest.raises(Closed) as caught:
            q.put({"t": "resize", "cols": 80, "rows": 24})
        return caught.value

    closed = asyncio.run(run())
    assert closed.reason == "overflow" and closed.close == 4429


# -- the input sink ---------------------------------------------------------------------------------------------------


class FakePump:
    def __init__(self):
        self.inbox = queue.SimpleQueue()
        self.pending = 0

    def sent(self):
        out = []
        while not self.inbox.empty():
            out.append(self.inbox.get_nowait())
        return out


def socket_for(mode="write", writer=True):
    sock = TermSocket(None, None, None, None, None)
    sock.read_only, sock.writer = mode == "read", writer
    sock.pump = FakePump()
    return sock


def test_read_attachment_and_lost_lease_drop_input():
    for sock in (socket_for("read", False), socket_for("write", False)):
        sock.on_binary(b"\x00ls\n")
        sock.on_binary(b"\x01\x1b[1;1R")
        assert sock.pump.sent() == [] and sock.final is None


def test_unknown_tag_or_empty_binary_is_protocol():
    for data in (b"", b"\x02x"):
        sock = socket_for()
        sock.on_binary(data)
        assert sock.final.reason == "protocol" and sock.final.close == 4400


def test_tag_one_is_rate_limited_four_per_second_burst_eight():
    sock = socket_for()
    for _ in range(20):
        sock.on_binary(b"\x01\x1b[1;1R")
    assert len(sock.pump.sent()) == 8
    sock.bucket_at -= 0.5  # half a second later: two more
    for _ in range(5):
        sock.on_binary(b"\x01\x1b[1;1R")
    assert len(sock.pump.sent()) == 2
    for _ in range(30):
        sock.on_binary(b"\x00k")  # key input (tag 0) is not limited here (the runner's rate applies)
    assert len(sock.pump.sent()) == 30


def test_resize_rules():
    async def run():
        sock = socket_for()
        sock.on_text('{"t":"resize","cols":19,"rows":24}')  # out of range: refused
        sock.on_text('{"t":"resize","cols":100,"rows":30}')
        sock.on_text('{"t":"resize","cols":101,"rows":30}')
        sock.on_text('{"t":"resize","cols":102,"rows":31}')  # coalesced: the last one
        await asyncio.sleep(0.05)
        first = sock.pump.sent()
        sock.on_text('{"t":"resize","cols":110,"rows":31}')
        await asyncio.sleep(0.05)
        early = sock.pump.sent()  # within 250 ms of the last: waits
        await asyncio.sleep(0.3)
        later = sock.pump.sent()
        reader = socket_for("read", False)
        reader.on_text('{"t":"resize","cols":100,"rows":30}')
        await asyncio.sleep(0.05)
        bad = socket_for()
        bad.on_text('{"t":"resize","cols":"100","rows":30}')
        return first, early, later, reader.pump.sent(), bad.final

    first, early, later, reader, bad = asyncio.run(run())
    assert first == [("resize", 102, 31)] and early == [] and later == [("resize", 110, 31)]
    assert reader == [] and bad.close == 4400


def test_runner_frames_map_to_browser_messages_without_internals():
    async def run():
        sock = socket_for()
        sock.post(("frame", {"t": "writer", "writer": False, "lease_gen": 7}, False))
        sock.post(("frame", {"t": "throttle", "retry_ms": 300}, False))
        sock.post(("frame", {"t": "ack", "input_id": 3}, False))
        sock.post(("frame", {"t": "exit", "code": 0, "signal": None}, False))
        sock.post(("frame", {"t": "end", "reason": "exited"}, True))
        return [i[1] for i in sock.out.items]

    frames = asyncio.run(run())
    assert frames[0] == {"t": "writer", "writer": False}  # never the lease generation
    assert frames[1] == {"t": "input_paused", "retry_ms": 300}
    assert frames[2] == {"t": "exit", "code": 0, "signal": None}
    assert frames[3] == {"t": "_end", "reason": "exited", "lost": True}  # input_lost goes out before the end


def test_ready_for_the_browser_carries_no_internals():
    ready = terminals._ready({"t": "ready", "start": 0, "offset": 9, "cols": 80, "rows": 24, "writer": True,
                              "lease_gen": 4, "state": "running"})  # fmt: skip
    assert "lease_gen" not in ready and ready["offset"] == 9

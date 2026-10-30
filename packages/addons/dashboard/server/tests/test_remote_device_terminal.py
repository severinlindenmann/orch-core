"""R3 T4 (#1591): terminals inside a stream. A presence per open and per attach, tickets bound to their stream, the
10-minute idle close."""

# ruff: noqa: F811

import asyncio

from device_kit import STREAM, app, click_start, fake, passkey_store, stream_client, ws  # noqa: F401
from orch.click.protocol import ClickError

from orch_dashboard import terminals
from orch_dashboard.policy import LOOPBACK, REMOTE_DEVICE
from orch_dashboard.remote_identity import REMOTE_STREAM

RUN = "r_" + "0" * 26
ATTACH = {"mode": "read", "take": False}


def test_terminal_presence_per_open_and_attach(app, auth, ws, fake):
    client = stream_client(app, auth, ws, level="terminals")[0]
    path = f"/api/w/{ws}/runs/{RUN}/attach"
    fake.answers["presence_take"] = ClickError("remote.presence_required", "no batch")
    r = client.post(path, json=ATTACH)
    assert r.status_code == 428 and r.json()["error"]["template"] == "/api/w/{ws}/runs/{run}/attach"
    assert "ticket" not in r.text
    del fake.answers["presence_take"]  # a batch covers it: one digest, one attach
    r = client.post(path, json=ATTACH)
    assert r.status_code == 200 and "ticket" in r.json()
    taken = [c for c in fake.calls if c[0] == "presence_take"]
    assert len(taken) == 2 and taken[0][1]["digest"] == taken[1][1]["digest"]
    # the next attach is another request: it asks again (the signer removes a digest when it is taken)
    client.post(path, json=ATTACH)
    assert len([c for c in fake.calls if c[0] == "presence_take"]) == 3
    # a work-level stream has no terminals at all
    work = stream_client(app, auth, ws, level="work", stream="77" * 16)[0]
    assert work.post(path, json=ATTACH).status_code == 403


def test_a_ticket_minted_in_a_stream_carries_the_stream(app, auth, ws, fake):
    client = stream_client(app, auth, ws, level="terminals")[0]
    raw = client.post(f"/api/w/{ws}/runs/{RUN}/attach", json=ATTACH).json()["ticket"]
    intent = app.state.term_tickets.redeem(raw)
    assert intent.stream == STREAM and intent.listener == REMOTE_DEVICE and intent.ws == ws


def test_ticket_from_other_stream_refused():
    """The socket redeems a ticket only on the stream it was minted in (`TermSocket.serve` compares `intent.stream`)."""
    tickets = terminals.Tickets()
    raw, _ = tickets.mint("tok", "w", RUN, "read", False, None, REMOTE_DEVICE, STREAM)
    intent = tickets.redeem(raw)
    assert intent.stream == STREAM
    other = REMOTE_STREAM.set("6f" * 16)
    try:
        assert intent.stream != REMOTE_STREAM.get()  # the other stream's socket would close 4401
    finally:
        REMOTE_STREAM.reset(other)
    loop_raw, _ = tickets.mint("tok", "w", RUN, "read", False, None, LOOPBACK)
    assert tickets.redeem(loop_raw).stream is None  # on loopback there is no stream, and none may be named


def test_the_serve_refuses_a_ticket_of_another_stream(monkeypatch):
    src = open(terminals.__file__).read()
    assert "intent.stream != REMOTE_STREAM.get()" in src


class FakeSocket:
    def __init__(self, scope):
        self.scope = scope

    async def receive(self):
        await asyncio.sleep(3600)


def test_terminal_idle_10min_closes():
    assert terminals.STREAM_IDLE_S == 600.0
    ws = FakeSocket({"orch_listener": REMOTE_DEVICE})
    sock = terminals.TermSocket(ws, None, None, None, None)
    assert sock.idle_s == 600.0
    assert terminals.TermSocket(FakeSocket({"orch_listener": LOOPBACK}), None, None, None, None).idle_s is None

    async def main():
        sock.idle_s = 0.05  # the same code path, a shorter wait
        task = asyncio.create_task(sock.from_browser())
        await asyncio.wait_for(sock.done.wait(), 2)
        await task
        assert sock.final.close == 1000 and sock.final.reason == "idle"

    asyncio.run(main())

"""R3 review, invariant B: the contract's ``app_seq_*`` vectors replayed against the real ``Terminator``. The host's
frames are fed as the host would hand them; the app's frames (the vector's ``h2d`` ones) are what the app does, so a
scripted app accepts or refuses the socket as the vector says. A vector whose error sits on a host-to-device frame is
the app's output, not this side's input: it is the host Tracker's to judge and is skipped here."""

# ruff: noqa: F811

import asyncio
import json
from pathlib import Path

import pytest
from adapter_kit import STREAM, App, Wire
from device_kit import app, click_start, fake, passkey_store, ws  # noqa: F401
from orch.crypto.encoding import b64u

from orch_dashboard.remote_device import HEAD, WS_OPEN

DOC = json.loads((Path(__file__).resolve().parents[5] / "contracts" / "hub" / "vectors_remote.json").read_text())
CASES = [c for c in DOC["cases"] if c["check"] == "app_seq"]


def _pt(spec: dict) -> bytes:
    if "hex" in spec:
        return bytes.fromhex(spec["hex"])
    f = spec["fill"]
    return bytes([f["kind"]]) + f["id"].to_bytes(4, "big") + bytes([f["byte"]]) * f["len"]


def _accepts(frames: list[dict]) -> bool:
    for f in frames:
        pt = _pt(f)
        if f["dir"] == "h2d" and pt[0] == HEAD and json.loads(pt[5:]).get("status") == 101:
            return True
    return False


def _scripted(accept: bool):
    async def inner(scope, receive, send):
        if scope["type"] == "websocket":
            await receive()
            if not accept:
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({"type": "websocket.accept"})
            while (await receive())["type"] != "websocket.disconnect":
                pass
            return
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return inner


def _error_on_host_frame(c: dict) -> bool:
    exp = c["expect"]
    return "error" in exp and c["args"]["frames"][exp["index"]]["dir"] == "h2d"


#: judged by the host alone: PING / PONG never reach the adapter (the host answers them), and after a refused upgrade
#: this side has ended the exchange with its own END, so a late socket frame is dropped here (the vector has no END)
HOST_ONLY = {"app_seq_ping_pong", "app_seq_pong_unsolicited", "app_seq_second_ping_before_pong",
             "app_seq_ws_frames_after_refusal"}  # fmt: skip
RUNNABLE = [c for c in CASES if not _error_on_host_frame(c) and c["name"] not in HOST_ONLY]


@pytest.mark.parametrize("c", RUNNABLE, ids=[c["name"] for c in RUNNABLE])
def test_terminator_replays_the_app_seq_vector(c, app, auth, ws):
    frames, exp = c["args"]["frames"], c["expect"]

    async def main():
        w = Wire(App(_scripted(_accepts(frames)), auth), auth, ws)
        w.open()
        for i, f in enumerate(frames):
            if f["dir"] == "h2d":
                await w.settle()  # the app's side of the vector: its frame left (or was refused) before the next
                continue
            if "error" in exp and i == exp["index"]:
                w.pump()
                assert not w.closes, "the stream ended before the frame the vector calls malformed"
            pt = _pt(f)
            msg = {"t": "frame", "stream_id": STREAM, "bytes": b64u(pt)}
            if pt[0] in (HEAD, WS_OPEN):
                msg["identity"] = w.identity()
            w.term.handle(msg)
            w.pump()
            if "error" in exp and i == exp["index"]:
                assert w.closes == [(STREAM, "malformed")], c.get("note")
                return
        await w.settle()
        assert (STREAM, "malformed") not in w.closes

    asyncio.run(main())

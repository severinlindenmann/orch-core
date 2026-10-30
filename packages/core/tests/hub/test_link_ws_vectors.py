"""R6 V0 (#1594): core replays the shared ``contracts/hub/vectors_link_ws.json`` against its own code, written from the
protocol text (D79: no hub import): the address grammar (``orch.hubclient.linkaddr``), ``hub_where`` with ``ws(s)://``
addresses (``orch.hubclient.where``), the connect-link endpoint, the frame codec and receiver and the client side of the
upgrade (``orch.hubclient.tlslink.wsframe``). The hub-side upgrade checks are the hub's; they are named here so a new
check kind cannot go unnoticed."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import pytest

from orch.hubclient import HubRefused
from orch.hubclient.linkaddr import parse_link_address
from orch.hubclient.tlsadapter import parse_addr
from orch.hubclient.tlslink import wsframe as W
from orch.hubclient.tlslink.errors import LinkClosed
from orch.hubclient.tlslink.transport import Pipe, WsPipe
from orch.hubclient.where import check_hub_where

from .vec import CONTRACT_DIR

DOC = json.loads((CONTRACT_DIR / "vectors_link_ws.json").read_text())
HUB_ONLY = {"check_request", "build_response", "build_refusal"}  # the hub accepts; core only dials
CORE = {
    "address", "connect_endpoint", "hub_where", "frame_encode", "frame_decode", "frame_stream", "accept_key",
    "build_request", "check_response",
}  # fmt: skip
MINE = [c for c in DOC["cases"] if c["check"] in CORE]


def test_no_unknown_check_kind_and_required_present() -> None:
    assert {c["check"] for c in DOC["cases"]} == CORE | HUB_ONLY
    names = {c["name"] for c in DOC["cases"]}
    assert set(DOC["required"]) <= names
    assert len(MINE) >= 200


def test_constants_match() -> None:
    k = DOC["constants"]
    assert (k["path"], k["subprotocol"], k["max_frame_payload"], k["max_send_chunk"], k["max_head"]) == (
        W.LINK_PATH, W.SUBPROTOCOL, W.MAX_FRAME, W.MAX_SEND_CHUNK, W.MAX_HEAD,
    )  # fmt: skip
    assert (k["ping_after_s"], k["idle_close_s"], k["close_wait_s"]) == (W.PING_AFTER_S, W.IDLE_CLOSE_S, W.CLOSE_WAIT_S)


def _endpoint(ep: Any) -> str:
    """The connect link's endpoint: ``local`` or an address of the grammar."""
    return "local" if ep == "local" else parse_link_address(ep).text


def _fill(a: dict[str, Any], key: str) -> bytes:
    b = bytes.fromhex(a[key])
    if "fill" in a:
        b += bytes.fromhex(a["fill"]["byte"]) * a["fill"]["n"]
    return b


def replay(c: dict[str, Any]) -> Any:  # noqa: C901 - one branch per check kind
    a, chk = c["args"], c["check"]
    try:
        if chk == "address":
            x = parse_link_address(a["text"])
            return {"ok": {"scheme": x.scheme, "host": x.host, "port": x.port, "sni": x.sni,
                           "host_header": x.host_header}}  # fmt: skip
        if chk == "connect_endpoint":
            return {"ok": _endpoint(a["endpoint"])}
        if chk == "hub_where":
            return {"ok": check_hub_where(a["answer"])}
    except HubRefused as e:
        return {"error": e.code}
    if chk == "frame_encode":
        payload = _fill(a, "payload_hex")
        try:
            b = W.encode_frame(a["opcode"], payload, mask=bytes.fromhex(a["mask_hex"]) if a["mask_hex"] else None,
                               fin=a["fin"])  # fmt: skip
        except ValueError:
            return {"error": "refused"}
        if "fill" in a:
            return {"ok": {"head_hex": b[: len(b) - len(payload)].hex(), "sha256": hashlib.sha256(b).hexdigest()}}
        return {"ok": {"hex": b.hex()}}
    if chk == "frame_decode":
        try:
            got = W.decode_frame(_fill(a, "hex"), sender=a["sender"])
        except W.WsError as e:
            return {"error": "ws", "close": e.code, "reason": e.reason}
        if got is None:
            return {"ok": "incomplete"}
        f, used = got
        if len(f.payload) > 512:
            return {"ok": {"fin": f.fin, "opcode": f.opcode, "payload_len": len(f.payload),
                           "payload_sha256": hashlib.sha256(f.payload).hexdigest(), "used": used}}  # fmt: skip
        return {"ok": {"fin": f.fin, "opcode": f.opcode, "payload_hex": f.payload.hex(), "used": used}}
    if chk == "frame_stream":
        r = W.Receiver(a["sender"])
        data, answers = b"", []
        try:
            for ch in a["chunks_hex"]:
                d, ans = r.feed(bytes.fromhex(ch))
                data += d
                answers += [[op, p.hex()] for op, p in ans]
        except W.WsError as e:
            return {"error": "ws", "close": e.code, "reason": e.reason}
        return {"ok": {"data_hex": data.hex(), "answers": answers, "ended": r.ended, "closed": r.closed}}
    if chk == "accept_key":
        return {"ok": W.accept_key(a["key"])}
    if chk == "build_request":
        hh = parse_link_address(a["address"]).host_header
        assert hh is not None
        return {"ok": W.build_request(hh, a["key"]).decode("ascii")}
    if chk == "check_response":
        try:
            W.check_response(a["head"].encode("utf-8"), a["key"])
        except W.HandshakeFailed as e:
            return {"error": "failed", "reason": e.reason}
        return {"ok": None}
    raise AssertionError(chk)


@pytest.mark.parametrize("c", MINE, ids=[c["name"] for c in MINE])
def test_link_ws_case(c: dict[str, Any]) -> None:
    assert replay(c) == c["expect"]


def test_receiver_one_byte_at_a_time() -> None:
    for c in (c for c in MINE if c["check"] == "frame_stream"):
        whole = b"".join(bytes.fromhex(h) for h in c["args"]["chunks_hex"])
        args = {"sender": c["args"]["sender"], "chunks_hex": [whole[i : i + 1].hex() for i in range(len(whole))]}
        assert replay({**c, "args": args}) == c["expect"], c["name"]


def test_parse_addr_takes_the_whole_grammar() -> None:
    """The dial path takes every form of the grammar (T-A): the parsed address names the transport."""
    a = parse_addr("[::1]:8105")
    assert (a.scheme, a.host, a.port) == ("tcp", "::1", 8105)
    for t, scheme, port in (("wss://hub-link.example.com", "wss", 443), ("ws://10.0.0.5:8106", "ws", 8106)):
        got = parse_addr(t)
        assert (got.scheme, got.port) == (scheme, port) and got == parse_link_address(t)
    for t in ("http://h:80", "ws://h/x", "h"):
        with pytest.raises(HubRefused):
            parse_addr(t)


class ScriptedPipe(Pipe):
    """A pipe that hands out scripted chunks and records what is written (the real WebSocket layer runs on it)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks, self.written = list(chunks), b""

    def poll(self, timeout: float) -> bytes | None:
        if not self.chunks:
            raise LinkClosed("eof")
        return self.chunks.pop(0)

    def write(self, data: bytes, deadline: float) -> None:
        self.written += data

    def close(self) -> None:
        pass


def _written_frames(raw: bytes) -> list[list[Any]]:
    out, buf = [], raw
    while buf:
        got = W.decode_frame(buf, sender="client")  # every frame this side wrote is masked
        assert got is not None
        f, used = got
        out.append([f.opcode, f.payload.hex()])
        buf = buf[used:]
    return out


def test_real_ws_layer_replays_build_request_and_check_response(monkeypatch: pytest.MonkeyPatch) -> None:
    for c in (c for c in MINE if c["check"] == "build_request"):
        a = c["args"]
        monkeypatch.setattr(W, "new_key", lambda key=a["key"]: key)
        pipe = ScriptedPipe([b"HTTP/1.1 400 x\r\n\r\n"])
        with pytest.raises(LinkClosed):
            WsPipe.connect(pipe, parse_link_address(a["address"]), time.monotonic() + 2)
        assert pipe.written.decode() == c["expect"]["ok"], c["name"]
    for c in (c for c in MINE if c["check"] == "check_response"):
        a = c["args"]
        monkeypatch.setattr(W, "new_key", lambda key=a["key"]: key)
        pipe = ScriptedPipe([a["head"].encode("utf-8")])
        try:
            WsPipe.connect(pipe, parse_link_address("wss://hub-link.example.com"), time.monotonic() + 2)
            got: Any = {"ok": None}
        except LinkClosed as e:
            got = {"error": "failed", "reason": str(e).removeprefix("ws_")}
        assert got == c["expect"], c["name"]


def test_real_ws_layer_replays_the_server_streams() -> None:
    for c in (c for c in MINE if c["check"] == "frame_stream" and c["args"]["sender"] == "server"):
        pipe = WsPipe(ScriptedPipe([bytes.fromhex(h) for h in c["args"]["chunks_hex"]]))
        data, err = b"", None
        try:
            while True:
                try:
                    got = pipe.poll(0.1)
                except LinkClosed as e:
                    if str(e) not in ("eof", "ws_closed"):
                        err = str(e)
                    break
                data += got or b""
        except Exception as e:  # noqa: BLE001
            raise AssertionError(c["name"]) from e
        exp = c["expect"]
        if "error" in exp:
            assert err == "ws_" + exp["reason"], c["name"]
            continue
        ok = exp["ok"]
        assert err is None and data.hex() == ok["data_hex"], c["name"]
        sent = _written_frames(pipe._inner.written)  # type: ignore[attr-defined]
        assert [x for x in sent if x[0] in (W.PONG,)] == [list(a) for a in ok["answers"] if a[0] == W.PONG], c["name"]
        assert pipe._ended is (ok["ended"]), c["name"]


def test_new_key_is_fresh_and_valid() -> None:
    import base64

    k1, k2 = W.new_key(), W.new_key()
    assert k1 != k2 and len(base64.b64decode(k1)) == 16 and len(k1) == 24

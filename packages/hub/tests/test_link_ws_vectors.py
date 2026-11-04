"""R6 V0 (#1594): the committed ``contracts/hub/vectors_link_ws.json`` is exactly what its generator builds, and every
case (link address grammar, ``hub_where`` with ``ws(s)://`` addresses, connect-link endpoint, the WebSocket frame codec
and receiver, the upgrade request and response) gives its expected result under ``orch_hub.contract.linkws`` and
``orch_hub.contract.where``. An unknown ``check`` or a missing required case fails the run: nothing is skipped."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from orch_hub.contract import linkws as L
from orch_hub.contract.hub import Refused
from orch_hub.contract.where import check_hub_where

VEC = Path(__file__).resolve().parents[3] / "contracts" / "hub"
PATH = VEC / "vectors_link_ws.json"
DOC = json.loads(PATH.read_text())
CASES = DOC["cases"]
CHECKS = {
    "address", "connect_endpoint", "hub_where", "frame_encode", "frame_decode", "frame_stream", "accept_key",
    "build_request", "build_response", "build_refusal", "check_request", "check_response",
}  # fmt: skip


def test_vector_link_ws_file_is_generated_byte_for_byte() -> None:
    spec = importlib.util.spec_from_file_location("make_vectors_link_ws", VEC / "make_vectors_link_ws.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert json.dumps(mod.build(), indent=1, ensure_ascii=True) + "\n" == PATH.read_text()


def test_vector_link_ws_checks_and_required() -> None:
    assert {c["check"] for c in CASES} == CHECKS
    assert set(DOC["required"]) <= {c["name"] for c in CASES}
    assert len(CASES) >= 250


def test_vector_link_ws_constants_match_reference() -> None:
    k = DOC["constants"]
    assert (k["path"], k["subprotocol"], str(k["ws_version"]), k["ws_guid"]) == (
        L.LINK_PATH, L.SUBPROTOCOL, L.WS_VERSION, L.WS_GUID,
    )  # fmt: skip
    assert (k["max_frame_payload"], k["max_send_chunk"], k["max_control_payload"], k["max_head"]) == (
        L.MAX_FRAME, L.MAX_SEND_CHUNK, L.MAX_CONTROL, L.MAX_HEAD,
    )  # fmt: skip
    assert (k["ping_after_s"], k["idle_close_s"], k["close_wait_s"], k["max_address"]) == (
        L.PING_AFTER_S, L.IDLE_CLOSE_S, L.CLOSE_WAIT_S, L.MAX_ADDRESS,
    )  # fmt: skip
    assert k["default_ports"] == L.DEFAULT_PORT


def _wire(a: dict[str, Any]) -> bytes:
    b = bytes.fromhex(a["hex"])
    if "fill" in a:
        b += bytes.fromhex(a["fill"]["byte"]) * a["fill"]["n"]
    return b


def _payload(a: dict[str, Any]) -> bytes:
    b = bytes.fromhex(a["payload_hex"])
    if "fill" in a:
        b += bytes.fromhex(a["fill"]["byte"]) * a["fill"]["n"]
    return b


def run(c: dict[str, Any]) -> Any:
    a, chk = c["args"], c["check"]
    try:
        if chk == "address":
            x = L.parse_link_address(a["text"])
            return {"ok": {"scheme": x.scheme, "host": x.host, "port": x.port, "sni": x.sni,
                           "host_header": x.host_header}}  # fmt: skip
        if chk == "connect_endpoint":
            return {"ok": L.check_connect_endpoint(a["endpoint"])}
        if chk == "hub_where":
            return {"ok": check_hub_where(a["answer"])}
    except Refused as e:
        return {"error": e.code}
    if chk == "frame_encode":
        try:
            b = L.encode_frame(a["opcode"], _payload(a), mask=bytes.fromhex(a["mask_hex"]) if a["mask_hex"] else None,
                               fin=a["fin"])  # fmt: skip
        except ValueError:
            return {"error": "refused"}
        if "fill" in a:
            return {"ok": {"head_hex": b[: len(b) - len(_payload(a))].hex(), "sha256": hashlib.sha256(b).hexdigest()}}
        return {"ok": {"hex": b.hex()}}
    if chk == "frame_decode":
        try:
            got = L.decode_frame(_wire(a), sender=a["sender"])
        except L.WsError as e:
            return {"error": "ws", "close": e.close, "reason": e.reason}
        if got is None:
            return {"ok": "incomplete"}
        f, used = got
        if len(f.payload) > 512:
            return {"ok": {"fin": f.fin, "opcode": f.opcode, "payload_len": len(f.payload),
                           "payload_sha256": hashlib.sha256(f.payload).hexdigest(), "used": used}}  # fmt: skip
        return {"ok": {"fin": f.fin, "opcode": f.opcode, "payload_hex": f.payload.hex(), "used": used}}
    if chk == "frame_stream":
        r = L.Receiver(a["sender"])
        data, answers = b"", []
        try:
            for ch in a["chunks_hex"]:
                d, ans = r.feed(bytes.fromhex(ch))
                data += d
                answers += [[op, p.hex()] for op, p in ans]
        except L.WsError as e:
            return {"error": "ws", "close": e.close, "reason": e.reason}
        return {"ok": {"data_hex": data.hex(), "answers": answers, "ended": r.ended, "closed": r.closed}}
    if chk == "accept_key":
        return {"ok": L.accept_key(a["key"])}
    if chk == "build_request":
        hh = L.parse_link_address(a["address"]).host_header
        assert hh is not None
        return {"ok": L.build_request(hh, a["key"]).decode("ascii")}
    if chk == "build_response":
        return {"ok": L.build_response(a["key"]).decode("ascii")}
    if chk == "build_refusal":
        return {"ok": L.build_refusal(a["status"]).decode("ascii")}
    if chk == "check_request":
        try:
            return {"ok": L.check_request(a["head"].encode("utf-8"))}
        except L.HandshakeRefused as e:
            return {"error": "refused", "status": e.status, "reason": e.reason}
    if chk == "check_response":
        try:
            L.check_response(a["head"].encode("utf-8"), a["key"])
            return {"ok": None}
        except L.HandshakeFailed as e:
            return {"error": "failed", "reason": e.reason}
    raise AssertionError(f"unknown check {chk}")


@pytest.mark.parametrize("c", CASES, ids=[c["name"] for c in CASES])
def test_vector_link_ws_case(c: dict[str, Any]) -> None:
    assert run(c) == c["expect"]


def test_vector_link_ws_receiver_byte_at_a_time() -> None:
    """Every ``frame_stream`` case gives the same result when its bytes arrive one at a time."""
    for c in (c for c in CASES if c["check"] == "frame_stream"):
        whole = b"".join(bytes.fromhex(h) for h in c["args"]["chunks_hex"])
        one = dict(
            c, args={"sender": c["args"]["sender"], "chunks_hex": [whole[i : i + 1].hex() for i in range(len(whole))]}
        )
        assert run(one) == c["expect"], c["name"]


def test_vector_link_ws_built_frames_decode_back() -> None:
    """What the encoder writes the decoder of the other side reads back (client masks, server does not)."""
    for n in (0, 1, 125, 126, 65535, 65536, L.MAX_SEND_CHUNK):
        p = bytes(i % 251 for i in range(n))
        f, _ = L.decode_frame(L.encode_frame(L.OP_BINARY, p, mask=b"\x01\x02\x03\x04"), sender="client")  # type: ignore[misc]
        assert (f.payload, f.opcode, f.fin) == (p, L.OP_BINARY, True)
        f, _ = L.decode_frame(L.encode_frame(L.OP_BINARY, p), sender="server")  # type: ignore[misc]
        assert f.payload == p

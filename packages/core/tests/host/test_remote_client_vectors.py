"""Remote access R5 (#1593, hub protocol §10.2d): the remote client's side of the hub's client endpoint. Core replays
every ``client_*`` case of the shared ``contracts/hub/vectors_remote.json`` with its own port below (written apart from
the hub's ``orch_hub.contract.remote.ClientConn``), so the R5 client (``orch.remoteclient``) has a reference it can
be checked against: the wire framing, every message shape in both directions, the order of a connection (and what the
receiver of a bad frame sends), and the hub's ``rail`` projection. The hub replays the same cases against its
reference and the ``endpoint: true`` sequences against its real ``RemoteEndpoint``."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from orch.canon.jcs import JcsError, dumps, loads_strict
from orch.crypto.encoding import EncodingError, unb64u
from orch.hubclient.custodial_shape import text_ok

CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "hub"
DOC: dict[str, Any] = json.loads((CONTRACT / "vectors_remote.json").read_text())
CASES = [c for c in DOC["cases"] if c["check"].startswith("client_")]
K = DOC["constants"]["client"]

# --- the port ---------------------------------------------------------------------------------------------------------

ALPN = "orch-remote/1"
FRAME_MAX = 96 * 1024
NOISE_MAX = 65535
B64_MAX = (NOISE_MAX * 4 + 2) // 3
RAIL_MAX, NAME_MAX = 100, 200
ERRORS = {"malformed", "no_session", "remote.busy", "unavailable"}
REFUSALS = {
    "malformed",
    "not_allowed",
    "unavailable",
    "remote.busy",
    "remote.off",
    "remote.device",
    "remote.no_code",
    "replay",
}
CLOSE = {
    "closed",
    "idle",
    "expired",
    "revoked",
    "handshake",
    "presence",
    "too_large",
    "malformed",
    "remote.off",
    "remote.device",
    "remote.busy",
    "remote.no_code",
    "remote.version",
}
HEX32 = re.compile(r"[0-9a-f]{32}")
IDS = {"session": re.compile(r"rd_[0-9a-f]{32}"), "enroll": re.compile(r"rc_[0-9a-f]{32}")}


class Bad(Exception):
    def __init__(self, code: str = "malformed") -> None:
        super().__init__(code)
        self.code = code


def _hex32(x: Any) -> bool:
    return isinstance(x, str) and HEX32.fullmatch(x) is not None


def _payload(wire: bytes) -> dict[str, Any]:
    if len(wire) < 4:
        raise Bad
    n = int.from_bytes(wire[:4], "big")
    if not 0 < n <= FRAME_MAX or len(wire) - 4 != n:
        raise Bad
    try:
        o = loads_strict(wire[4:])
    except (JcsError, UnicodeDecodeError, ValueError):
        raise Bad from None
    if not isinstance(o, dict):
        raise Bad
    return o


def _b(x: Any) -> None:
    if isinstance(x, str) and len(x) > B64_MAX:
        raise Bad("too_large")
    try:
        raw = unb64u(x)
    except (EncodingError, TypeError, ValueError):
        raise Bad from None
    if len(raw) == 0:
        raise Bad
    if len(raw) > NOISE_MAX:
        raise Bad("too_large")


def _rows_ok(rows: Any, more: Any) -> bool:
    if not isinstance(rows, list) or more not in (True, False) or len(rows) > RAIL_MAX:
        return False
    if more and len(rows) < RAIL_MAX:
        return False
    ids = []
    for r in rows:
        if not isinstance(r, dict) or sorted(r) != ["name", "online", "workspace_id"]:
            return False
        if not _hex32(r["workspace_id"]) or r["online"] not in (True, False) or type(r["online"]) is not bool:
            return False
        if r["name"] is not None and not text_ok(r["name"], NAME_MAX):
            return False
        ids.append(r["workspace_id"])
    return ids == sorted(set(ids))


SHAPES: dict[tuple[str, str], tuple[set[str], Any]] = {
    ("c2h", "hello"): (
        {"t", "v", "token"},
        lambda o: type(o["v"]) is int and o["v"] == 1 and isinstance(o["token"], str),
    ),
    ("c2h", "rail"): ({"t"}, lambda o: True),
    (
        "c2h",
        "open",
    ): (
        {"t", "workspace_id", "device_id", "kind"},
        lambda o: (
            _hex32(o["workspace_id"])
            and o["kind"] in IDS
            and isinstance(o["device_id"], str)
            and IDS[o["kind"]].fullmatch(o["device_id"]) is not None
        ),
    ),
    ("c2h", "data"): ({"t", "b"}, None),
    ("c2h", "close"): ({"t", "reason"}, lambda o: o["reason"] in CLOSE),
    ("h2c", "ready"): ({"t", "hub_id"}, lambda o: _hex32(o["hub_id"])),
    ("h2c", "error"): ({"t", "error"}, lambda o: isinstance(o["error"], str) and o["error"] in ERRORS),
    ("h2c", "rail"): ({"t", "workspaces", "more"}, lambda o: _rows_ok(o["workspaces"], o["more"])),
    ("h2c", "opened"): ({"t", "stream_id"}, lambda o: _hex32(o["stream_id"])),
    ("h2c", "refused"): ({"t", "error"}, lambda o: isinstance(o["error"], str) and o["error"] in REFUSALS),
    ("h2c", "data"): ({"t", "b"}, None),
    ("h2c", "close"): ({"t", "reason"}, lambda o: isinstance(o["reason"], str) and o["reason"] in CLOSE),
}


def check(direction: str, o: dict[str, Any]) -> None:
    t = o.get("t")
    shape = SHAPES.get((direction, t)) if isinstance(t, str) else None
    if shape is None or set(o) != shape[0]:
        raise Bad
    if shape[1] is None:
        _b(o["b"])
    elif not shape[1](o):
        raise Bad


#: (state, direction, t) -> next state. One connection, both directions, in arrival order.
MOVES = {
    ("hello", "c2h", "hello"): "auth",
    ("auth", "h2c", "ready"): "ready",
    ("auth", "h2c", "error"): "ended",
    ("ready", "c2h", "rail"): "rail_wait",
    ("ready", "c2h", "open"): "open_wait",
    ("rail_wait", "h2c", "rail"): "ready",
    ("rail_wait", "h2c", "error"): "ended",
    ("open_wait", "h2c", "opened"): "stream",
    ("open_wait", "h2c", "refused"): "ended",
    ("open_wait", "h2c", "error"): "ended",
    ("stream", "c2h", "data"): "stream",
    ("stream", "h2c", "data"): "stream",
    ("stream", "c2h", "close"): "client_closed",
    ("stream", "h2c", "close"): "ended",
    ("client_closed", "h2c", "data"): "client_closed",
    ("client_closed", "h2c", "close"): "ended",
}


class Conn:
    def __init__(self) -> None:
        self.state, self.rail_used = "hello", False

    def feed(self, direction: str, wire: bytes) -> str:
        o = _payload(wire)
        t = o.get("t")
        nxt = MOVES.get((self.state, direction, t)) if isinstance(t, str) else None
        if nxt is None or (direction == "c2h" and t == "rail" and self.rail_used):
            raise Bad
        check(direction, o)
        self.rail_used = self.rail_used or t == "rail"
        dropped = self.state == "client_closed" and t == "data"
        self.state = nxt
        return "dropped" if dropped else "ok"

    def answer(self, direction: str, code: str) -> dict[str, Any] | None:
        if self.state == "stream":
            return {"t": "close", "reason": code}
        if direction == "c2h" and self.state == "hello":
            return {"t": "error", "error": code}
        if direction == "c2h" and self.state == "ready":
            return {"t": "refused", "error": code}
        return None


def rail(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = []
    for r in sorted(rows, key=lambda r: r["workspace_id"]):
        name = r.get("name")
        if isinstance(name, str):
            name = name.replace("\n", " ").strip()
            raw = name.encode("utf-8")[:NAME_MAX].decode("utf-8", "ignore").strip()
            name = raw if text_ok(raw, NAME_MAX) else None
        else:
            name = None
        out.append({"workspace_id": r["workspace_id"], "name": name, "online": r.get("online") is True})
    return {"t": "rail", "workspaces": out[:RAIL_MAX], "more": len(out) > RAIL_MAX}


def wire(spec: dict[str, Any]) -> bytes:
    if "wire_hex" in spec:
        return bytes.fromhex(spec["wire_hex"])
    frame = dict(spec["frame"])
    rep = spec.get("b_repeat")
    if rep:
        frame["b"] = rep["unit"] * rep["n"] + rep["tail"]
    body = dumps(frame)
    return len(body).to_bytes(4, "big") + body


def run(c: dict[str, Any]) -> dict[str, Any]:
    a = c["args"]
    if c["check"] == "client_rail":
        return {"ok": rail(a["rows"])}
    if c["check"] == "client_frame":
        try:
            check(a["dir"], _payload(wire(a)))
        except Bad as e:
            return {"error": e.code}
        return {"ok": True}
    assert c["check"] == "client_seq", c["check"]
    conn, out = Conn(), []
    for i, f in enumerate(a["frames"]):
        try:
            out.append(conn.feed(f["dir"], wire(f)))
        except Bad as e:
            return {"error": e.code, "index": i, "answer": conn.answer(f["dir"], e.code)}
    return {"results": out, "end": conn.state}


# --- tests ------------------------------------------------------------------------------------------------------------


def test_vector_remote_client_constants() -> None:
    assert K["alpn"] == ALPN and K["version"] == 1
    assert (K["frame_max"], K["b64_max"], K["rail_max"], K["rail_name_max"]) == (FRAME_MAX, B64_MAX, RAIL_MAX, NAME_MAX)
    assert set(K["error_codes"]) == ERRORS and set(K["refused_codes"]) == REFUSALS
    assert set(DOC["constants"]["close_reasons"]) == CLOSE
    assert {(d, t): set(k) for d, m in K["messages"].items() for t, k in m.items()} == {
        k: v[0] for k, v in SHAPES.items()
    }
    assert (K["setup_ms"], K["open_wait_ms"]) == (30_000, 30_000)


def test_vector_remote_client_cases_cover_the_contract() -> None:
    names = {c["name"] for c in CASES}
    assert {c["check"] for c in CASES} == {"client_frame", "client_rail", "client_seq"}
    assert {n for n in DOC["required"] if n.startswith("client_")} <= names
    assert len(CASES) >= 140
    # every hub code a client must show plainly appears in a sequence the real hub produced
    shown = {
        f["frame"].get("error") or f["frame"].get("reason")
        for c in CASES
        if c["check"] == "client_seq" and c["args"]["endpoint"]
        for f in c["args"]["frames"]
        if f["dir"] == "h2c" and "frame" in f and f["frame"]["t"] in ("error", "refused", "close")
    }
    assert {"no_session", "unavailable", "not_allowed", "remote.off", "remote.device", "remote.busy"} <= shown
    assert {"remote.no_code", "replay", "remote.version"} <= shown


@pytest.mark.parametrize("c", CASES, ids=[c["name"] for c in CASES])
def test_vector_remote_client_case(c: dict[str, Any]) -> None:
    assert run(c) == c["expect"], c.get("note")

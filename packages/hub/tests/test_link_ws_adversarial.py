# ruff: noqa: F811
"""Opus R6 review: hub WebSocket pre-auth adversarial cases (proposed)."""

from __future__ import annotations

import socket
import time

from linkfakes import new_key, upgrade_request
from test_link_ws import _after_upgrade, _close_code, mk, ws_addr  # noqa: F401

from orch_hub.contract import linkws as W

M = b"\x01\x02\x03\x04"


def test_continuation_flood_pre_auth_hits_the_byte_budget(mk):
    h = mk()
    data = W.encode_frame(W.OP_BINARY, b"", mask=M, fin=False) + b"".join(
        W.encode_frame(W.OP_CONT, b"", mask=M, fin=False) for _ in range(12_000)
    )  # 72 kB of empty fragments, no TLS byte
    t0 = time.monotonic()
    _, out = _after_upgrade(h, data)
    assert _close_code(out) == 1008 and time.monotonic() - t0 < 4


def test_pong_flood_is_never_answered(mk):
    h = mk()
    data = b"".join(W.encode_frame(W.OP_PONG, b"x" * 125, mask=M) for _ in range(400))  # 52 kB, under budget
    _, out = _after_upgrade(h, data)
    assert b"\x8a" not in out[:2]  # nothing echoed


def test_drip_fed_head_is_cut_at_the_head_timeout(mk):
    h = mk(ws_head_timeout=1.0) if "ws_head_timeout" in mk.__code__.co_varnames else mk()
    s = socket.create_connection(ws_addr(h), timeout=3)
    try:
        req = upgrade_request("hub.example", new_key())
        t0 = time.monotonic()
        closed = False
        for b in req[:-1]:
            try:
                s.sendall(bytes([b]))
            except OSError:
                closed = True
                break
            time.sleep(0.05)
            if time.monotonic() - t0 > 8:
                break
        s.settimeout(1)
        try:
            closed = closed or s.recv(1) == b""
        except OSError:
            closed = True
        assert closed and time.monotonic() - t0 < 9
    finally:
        s.close()

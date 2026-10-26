"""Helpers of the router tests: v2 §11.1 envelopes (WSK-signed, opaque sealed body) and a hub with a router."""

from __future__ import annotations

import os
import struct

import pytest
from conftest import NOW, Hub

from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.link import Registry
from orch_hub.router import Limits, RouteQueue, Router
from orch_hub.router.envelope import SIG_LABEL


def envelope(
    frm: str = G.WS_A,
    to: str = G.WS_B,
    *,
    key: str = "wsk_a",
    env_id: bytes | None = None,
    body: bytes | None = None,
    flags: int = 0,
    cosig: bytes = b"",
    wxk_version: int = 1,
) -> bytes:
    """header(60) || u32(len) || body || sig(64) [|| u32 || cosig]; the body is random bytes (the hub cannot read it)."""
    body = os.urandom(200) if body is None else body
    header = (
        b"ORWX"
        + bytes([2, S.SUITE_ID, flags, 0])
        + (env_id or os.urandom(16))
        + bytes.fromhex(frm)
        + bytes.fromhex(to)
        + struct.pack(">I", wxk_version)
    )
    sig = S.sign(G.K.priv[key], SIG_LABEL + header + body)
    out = header + struct.pack(">I", len(body)) + body + sig
    if flags & 1:
        out += struct.pack(">I", len(cosig)) + cosig
    return out


class RouterHub(Hub):
    def __init__(self, *, queue_path: str = ":memory:", registry: Registry | None = None, limits=None, authz=None):
        self.registry = registry or Registry()
        self.queue = RouteQueue(queue_path, limits if limits is not None else Limits())
        self.router = Router(self.registry, self.queue, authz, lambda: NOW)
        super().__init__(registry=self.registry, router=self.router)

    def join(self, ws: str = G.WS_A):
        if ws == G.WS_A:
            c = self.open(first=True)
            c.request("join", self.join_body())
        else:
            c = self.open(G.WS_B, "wsk_b", first=True)
            c.request("join", self.join_body("invB", ws=G.WS_B, wsk="wsk_b"))
        return c

    def reopen(self, ws: str):
        return self.open(ws, "wsk_a" if ws == G.WS_A else "wsk_b")

    def close(self):
        super().close()
        self.queue.close()


@pytest.fixture
def rhub():
    made = []

    def make(**kw):
        h = RouterHub(**kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()

"""R2 through the loopback seam (11A.10 harness layer 1): :class:`BridgeLoop`'s threads against an in-process relay on
127.0.0.1 (the R1 :class:`SimRelay`, extended here with ``host/poll`` and the host's chunk POST): the hold, the poll,
the responder, two job threads, the cursor, the POSTs, and the worker contract ``stop()`` / ``join(timeout)``."""

from __future__ import annotations

import threading
import time

import pytest

from orch import crypto
from orch.relay import http
from orch.relay.bridge import BridgeLoop

from .bridgekit import World, open_chunk
from .helpers import OPEN_CONNS, WS, Holder, make_card
from .helpers import session as make_session
from .mock import MockRelay, Seen, json_reply
from .sim import SimRelay, _err


class BridgeSim(SimRelay):
    """SimRelay plus a non-consuming host queue (``items``), the cursor and the chunk store (P3-D9/D10/D27)."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.items: list[dict] = []
        self.cursor = 0
        self.chunks: dict[tuple[str, int], bytes] = {}
        self.posts = 0
        self.polls = 0

    def _route(self, s: Seen):
        p = s.path.split("/")
        if len(p) >= 5 and p[1:3] == ["v2", "workspaces"]:
            ws, rest = p[3], "/".join(p[4:])
            if s.method == "GET" and rest == "host/poll":
                if (r := self._auth(s, ws) or self._fence(s, ws)) is not None:
                    return r
                self.polls += 1
                self.cursor = max(self.cursor, int(s.query["after"]))
                gen, _ = self.holders[ws]
                self.holders[ws] = (gen, self.now + 40_000)  # a poll start renews the lease
                items = [i for i in self.items if i["n"] > self.cursor][:16]
                notices = {"revocations_cursor": 0, "cert_requests": [], "rotation_due": [], "unpublished": None}
                return json_reply(200, {"items": items, "notices": notices, "next": None})
            if s.method == "POST" and rest.startswith("bridge/"):
                if (r := self._auth(s, ws) or self._fence(s, ws)) is not None:
                    return r
                idx = int.from_bytes(s.body[72:80], "big")
                key = (rest.split("/")[1], idx)
                held = self.chunks.setdefault(key, s.body)
                self.posts += 1
                return json_reply(202, {}) if held == s.body else _err(409, "chunk_conflict")
        return super()._route(s)


@pytest.fixture
def env(tmp_path):
    m = MockRelay()
    sim = BridgeSim(m.origin(), owner_pk=b"")
    sim.cards[WS] = make_card(m.origin())
    m.fallback = sim
    w = World(tmp_path, start=False)
    sess = make_session(m.origin(), holder=Holder())
    conns: list[http.RelayConn] = []

    def conn():
        c = http.RelayConn(m.origin())
        conns.append(c)
        OPEN_CONNS.append(c)
        return c

    loop = BridgeLoop(w.bridge, sess, conn=conn, workspace_id=WS)
    yield m, sim, w, loop
    loop.stop()
    assert loop.join(10)
    w.close()
    m.close()


def _wait(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _item(sim, n, env):
    sim.items.append({
        "n": n, "route": "bridge", "device": env[24:40].hex(), "rid": env[40:56].hex(), "offer": None,
        "envelope": crypto.b64u(env),
    })  # fmt: skip


def test_the_loop_holds_polls_answers_and_acknowledges_in_order(env):
    m, sim, w, loop = env
    read = w.phone.request(w.wk, {"op": "whoami", "args": {}})
    write = w.phone.request(w.wk, w.new_ticket())
    pair = {"n": 3, "route": "pair", "device": "00" * 16, "rid": "11" * 16, "offer": "22" * 16, "envelope": "AAAA"}
    _item(sim, 1, read)
    _item(sim, 2, write)
    sim.items.append(pair)
    loop.start()
    assert _wait(lambda: len(sim.chunks) >= 2 and w.rids.cursor() >= 3)
    assert _wait(lambda: sim.cursor >= 3)  # the next poll acknowledged everything durable, the pair item included
    (r_chunk,) = [v for (rid, _), v in sim.chunks.items() if rid == read[40:56].hex()]
    assert open_chunk(r_chunk, w.wk, w.key.public_key)[1] == {"status": "ok"}
    assert len(w.host.log) == 1
    # the relay delivers item 2 again (non-consuming poll, a lost cursor): never run twice
    sim.cursor = 1
    assert _wait(lambda: sim.cursor >= 3)
    time.sleep(0.3)
    assert len(w.host.log) == 1 and len(w.host.runs) == 1


def test_stop_ends_every_thread_within_the_join_bound(env):
    m, sim, w, loop = env
    loop.start()
    assert _wait(lambda: sim.polls >= 1)
    t0 = time.monotonic()
    loop.stop()
    assert loop.join(5.0)
    assert time.monotonic() - t0 < 5.0
    assert not [t for t in threading.enumerate() if t.name.startswith("orch-relay-") and t.is_alive()]


def test_fenced_stops_the_worker(env):
    m, sim, w, loop = env
    loop.start()
    assert _wait(lambda: sim.polls >= 1)
    gen, until = sim.holders[WS]
    sim.holders[WS] = (gen + 1, until)  # another holder took over
    assert _wait(lambda: loop.error is not None)
    assert loop.join(10)


def test_the_loop_keeps_the_last_polls_notices_for_r4(env):
    """R4 (11A.9): ``BridgeLoop.notices`` is the ``notices`` object of the last poll, a copy, read-only for R4."""
    m, sim, w, loop = env
    assert loop.notices is None
    loop.start()
    assert _wait(lambda: sim.polls >= 1 and loop.notices is not None)
    assert loop.notices == {"revocations_cursor": 0, "cert_requests": [], "rotation_due": [], "unpublished": None}

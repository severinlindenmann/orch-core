"""R2 through the loopback seam (11A.10 harness layer 1): :class:`BridgeLoop`'s threads against an in-process relay on
127.0.0.1 (the R1 :class:`SimRelay`, extended here with ``host/poll`` and the host's chunk POST): the hold, the poll,
the responder, two job threads, the cursor, the POSTs, and the worker contract ``stop()`` / ``join(timeout)``."""

from __future__ import annotations

import threading
import time
import types

import pytest

from orch import crypto
from orch.net import http
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


# -- the aux thread's lease renewal survives failures (idle link: stale keep-alive on the renewal's connection) -------


class _RenewSession:
    """Just the renewal surface of a Session: ``renew`` raises the scripted errors in order, then succeeds."""

    def __init__(self, script: list[BaseException]) -> None:
        self.script, self.calls, self.transports = list(script), 0, []
        self.ok = threading.Event()

    def renew(self, *, deadline_s: float, transport=None):
        self.calls += 1
        self.transports.append(transport)
        if self.script:
            raise self.script.pop(0)
        self.ok.set()


def _aux_loop(monkeypatch, script):
    from orch.relay import bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "AUX_TICK_S", 0.01)
    sess = _RenewSession(script)
    clock = [0.0]
    loop = BridgeLoop(
        types.SimpleNamespace(sweep=lambda: None, retry_stuck=lambda: None, flush=lambda: None, alarm=lambda *a: None),
        sess, conn=lambda: object(), workspace_id=WS, mono=lambda: clock[0],
    )  # fmt: skip
    loop._last_poll = 0.0
    clock[0] = bridge_mod.RENEW_EVERY_S + 1  # a renewal is due: the long-poll has been silent for the whole interval
    return loop, sess, clock


def test_aux_renewal_retries_a_transport_failure_on_its_own_connection_and_keeps_the_thread(monkeypatch):
    err = http.TransportError("request", "POST /v2/workspaces/{ws}/host/hold")  # a stale keep-alive, reset on send
    loop, sess, clock = _aux_loop(monkeypatch, [err, err, err])  # attempt 1+2 fail, the next tick's attempt 1 fails
    t = threading.Thread(target=loop._aux_main, daemon=True)
    t.start()
    try:
        assert sess.ok.wait(5), "the renewal never succeeded: the aux thread died or gave up"
        assert t.is_alive(), "the aux thread must survive failed renewals"
        assert sess.calls == 4
        assert loop.error is None
        assert len({id(c) for c in sess.transports}) == 1 and sess.transports[0] is not None  # its own connection
        assert loop._last_poll == clock[0]  # renewed: the next one is a full interval away
    finally:
        loop._stop.set()
        t.join(5)


def test_aux_renewal_fenced_still_stops_the_worker(monkeypatch):
    from orch.relay.session import Fenced

    loop, sess, _ = _aux_loop(monkeypatch, [Fenced("fenced")])
    loop._spawn("aux", loop._aux_main)
    assert _wait(lambda: loop.error is not None, 5)
    assert isinstance(loop.error, Fenced)
    assert loop.join(5)


def test_aux_renewal_refusal_honours_retry_after_and_login_holdoff_waits_too(monkeypatch):
    from orch.relay import bridge as bridge_mod
    from orch.relay.retry import RelayRefused
    from orch.relay.session import LoginHeldOff

    refused = RelayRefused(429, "rate_limited", retry_after=7, route="POST hold")
    loop, sess, clock = _aux_loop(monkeypatch, [refused, LoginHeldOff("held")])
    t = threading.Thread(target=loop._aux_main, daemon=True)
    t.start()
    try:
        assert _wait(lambda: sess.calls == 1, 5)
        time.sleep(0.2)  # many ticks pass: no second attempt inside the 7 s Retry-After
        assert sess.calls == 1
        clock[0] += 7.5
        assert _wait(lambda: sess.calls == 2, 5)  # the login hold-off: the default backoff
        time.sleep(0.2)
        assert sess.calls == 2
        clock[0] += bridge_mod.RENEW_BACKOFF_S + 0.5
        assert sess.ok.wait(5) and sess.calls == 3
        assert t.is_alive() and loop.error is None
    finally:
        loop._stop.set()
        t.join(5)


def test_a_login_holdoff_is_transient_for_the_poster_and_the_poll_loop(monkeypatch):
    from orch.relay.session import LoginHeldOff

    class S:
        calls = 0

        def hold(self, **kw):
            pass

        def call(self, *a, **kw):
            S.calls += 1
            raise LoginHeldOff("held")

    bridge = types.SimpleNamespace(
        post=None, start=lambda: None, rids=types.SimpleNamespace(cursor=lambda: 0), alarm=lambda *a: None,
        sweep=lambda: None, retry_stuck=lambda: None, flush=lambda: None,
    )  # fmt: skip
    loop = BridgeLoop(bridge, S(), conn=lambda: object(), workspace_id=WS)  # type: ignore[arg-type]
    assert loop._poster(object())("d" * 16, "r" * 16, b"x") == "retry"
    monkeypatch.setattr(loop, "_spawn", lambda role, fn: None)  # only the poll loop runs here
    t = threading.Thread(target=loop._poll_main, daemon=True)
    t.start()
    try:
        assert _wait(lambda: S.calls >= 1, 5)
        time.sleep(1.3)  # the loop pauses 1 s after a failed poll and polls again: it did not stop
        assert t.is_alive() and loop.error is None and S.calls >= 2
    finally:
        loop.stop()
        t.join(5)

"""R6c: the pair thread (11A.15.2 "Threads") and R2's hand-off: ``handle`` returns at once, overflow of the 64-item
queue is a silent counted drop completed at once, ``call`` runs the internal API on the thread, a pair request is
answered while another job of the thread is slow only after it (one thread: the membership thread is another one),
and the bridge loop's cursor advances only over completed pair items."""

from __future__ import annotations

import threading
import time

import pytest

from orch.relay.bridge import BridgeLoop
from orch.relay.r6.pair import QUEUE_ITEMS, PairRefused, PairThread

from .helpers import WS
from .pairkit import Kit, Phone


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path)
    yield k
    k.close()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_handle_returns_at_once_and_the_thread_completes_the_item(kit):
    t = PairThread(kit.engine, name="orch-relay-r6p-test")
    t.start()
    try:
        h = t.call(lambda: kit.engine.offer())
        phone = Phone(h.link.native)
        env, rid = phone.envelope(phone.meta(), ts=kit.clock.ms)
        t.handle(7, env, rid, phone.device_id)
        assert _wait(lambda: t.completed(7))
        assert kit.rec(h.offer_id)["state"] == "pending" and kit.relay.answers
    finally:
        t.stop()
        assert t.join(5)


def test_call_raises_the_apis_refusal(kit):
    t = PairThread(kit.engine, name="orch-relay-r6p-test")
    t.start()
    try:
        with pytest.raises(PairRefused):
            t.call(lambda: kit.engine.offer(("operate",)))
    finally:
        t.stop()
        t.join(5)


def test_pair_queue_overflow_counted_drop_completed(kit):
    t = PairThread(kit.engine, name="orch-relay-r6p-test")  # not started: nothing drains the queue
    for n in range(1, QUEUE_ITEMS + 3):
        t.handle(n, b"x", "ab" * 16, None)
    assert not t.completed(QUEUE_ITEMS)  # queued
    assert t.completed(QUEUE_ITEMS + 1) and t.completed(QUEUE_ITEMS + 2)  # dropped, completed at once
    assert kit.engine.dropped == 2


def test_a_failing_job_never_stops_the_thread(kit):
    t = PairThread(kit.engine, name="orch-relay-r6p-test")
    t.start()
    try:
        t.post(lambda: 1 / 0)
        assert t.call(lambda: "alive") == "alive"
    finally:
        t.stop()
        t.join(5)


def test_the_membership_events_are_jobs_never_inline(kit):
    t = PairThread(kit.engine, name="orch-relay-r6p-test")
    ran = []
    kit.engine.membership_refused = lambda o, r: ran.append(threading.current_thread().name)  # type: ignore
    t.start()
    try:
        kit.membership.events.on_refused("ab" * 16, "x")  # from the membership thread
        assert _wait(lambda: ran == ["orch-relay-r6p-test"])
    finally:
        t.stop()
        t.join(5)


class _Rids:
    def __init__(self):
        self.c = 0

    def cursor(self):
        return self.c

    def set_cursor(self, n):
        self.c = n


class _Bridge:
    def __init__(self):
        self.rids = _Rids()


class _Pairing:
    def __init__(self):
        self.done: set[int] = set()
        self.forgot: list[int] = []

    def completed(self, n):
        return n in self.done

    def forget_below(self, n):
        self.forgot.append(n)


def test_the_cursor_advances_only_over_completed_pair_items():
    pairing = _Pairing()
    loop = BridgeLoop(_Bridge(), None, conn=lambda: None, workspace_id=WS, pairing=pairing)  # type: ignore[arg-type]
    loop._ack(1)  # a bridge item: durable when handed
    loop._pair_open.add(2)
    loop._ack(2)  # a pair item, queued on the pair thread
    loop._ack(3)
    assert loop.bridge.rids.cursor() == 1
    pairing.done.add(2)
    loop._advance()  # the aux step
    assert loop.bridge.rids.cursor() == 3 and pairing.forgot[-1] == 3


def test_without_pairing_pair_items_are_acknowledged_as_before():
    loop = BridgeLoop(_Bridge(), None, conn=lambda: None, workspace_id=WS)  # type: ignore[arg-type]
    loop._ack(1)
    loop._ack(2)
    assert loop.bridge.rids.cursor() == 2

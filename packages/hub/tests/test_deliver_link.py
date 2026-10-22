"""event_deliver over a real link (protocol.md §8.7): the host's reply decides, a drop keeps the event."""

from __future__ import annotations

import time

import pytest
from conftest import Hub
from test_deliver_queue import LOG, ev

from orch_hub.contract import vectorgen as G
from orch_hub.custodial.deliver import DeliveryQueue
from orch_hub.link import LinkError, tls


def until(pred, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def rig():
    q = DeliveryQueue(poll_s=0.02).start()
    h = Hub(deliveries=q)
    c = h.open(first=True)
    c.request("join", h.join_body())
    yield q, h, c
    h.close()
    q.close()


def answer(c, body, **kw):
    """Receive one event_deliver request and reply to it as the host would."""
    o = c.recv_reply()
    assert o["op"] == "event_deliver" and o["re"] is None
    c.send_raw(c.build("event_deliver", body, re=o["seq"], **kw))
    return o["body"]


def test_event_goes_out_over_the_link_and_an_empty_reply_ends_it(rig):
    q, _h, c = rig
    q(G.WS_A, LOG, ev())
    assert answer(c, {}) == {"log": LOG, "event": ev()}
    assert until(lambda: q.outcomes() and q.outcomes()[0]["state"] == "done")


def test_a_final_error_drops_it(rig):
    q, _h, c = rig
    seen = []
    q._notify = seen.append
    q(G.WS_A, LOG, ev())
    answer(c, {"error": "event_changed"})
    assert until(lambda: seen)
    assert seen[0]["outcome"] == "dropped" and seen[0]["code"] == "event_changed"


def test_a_dropped_link_keeps_the_event_for_the_next_link(rig):
    q, h, c = rig
    q(G.WS_A, LOG, ev())
    c.recv_reply()  # the request arrives, the host never answers
    c.close()
    assert until(lambda: q.pending() and q.pending()[0]["attempts"] == 1 and q.pending()[0]["last_code"])
    c2 = h.open()  # the next link: the resend waits out its 1 s backoff, then the same bytes arrive
    t0 = time.time()
    assert answer(c2, {}) == {"log": LOG, "event": ev()}
    assert time.time() - t0 >= 0.5
    assert until(lambda: q.outcomes() and q.outcomes()[0]["state"] == "done")


def test_the_host_cannot_send_event_deliver(rig):
    _q, _h, c = rig
    with pytest.raises(LinkError):
        c.request("event_deliver", {"log": LOG, "event": {}})


def _silent(c):
    c._t = 0.4
    with pytest.raises((LinkError, tls.LinkTimeout, tls.LinkClosed)):  # LinkTimeout / LinkError: no frame arrives
        c.recv_reply()


def test_a_link_with_a_rotated_out_key_gets_no_delivery(rig, monkeypatch):
    q, h, c = rig
    pin = h.registry.pin(G.WS_A)
    other = {**pin, "wsk_pub": G.K.b("wsk_b")}  # the pin moved to another key while this (old) link stayed open
    monkeypatch.setattr(h.registry, "pin", lambda ws: other)
    q(G.WS_A, LOG, ev())
    time.sleep(0.3)
    assert q.pending()[0]["attempts"] in (0, 1)
    _silent(c)
    assert q.outcomes() == []


def test_a_link_whose_binding_ended_gets_no_delivery(rig, monkeypatch):
    q, h, c = rig
    monkeypatch.setattr(h.registry, "mode", lambda ws: None)
    q(G.WS_A, LOG, ev())
    time.sleep(0.3)
    _silent(c)
    assert len(q.pending()) == 1


def test_a_stale_link_is_detached_at_the_next_dispatch(rig, monkeypatch):
    q, h, c = rig
    assert G.WS_A in q._links
    monkeypatch.setattr(h.registry, "mode", lambda ws: None)
    c.request("snapshot_get", {})
    assert until(lambda: G.WS_A not in q._links)

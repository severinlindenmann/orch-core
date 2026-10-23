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
    real = h.registry.binding_state
    monkeypatch.setattr(h.registry, "binding_state", lambda ws: {**real(ws), "status": "unbound"})
    q(G.WS_A, LOG, ev())
    time.sleep(0.3)
    _silent(c)
    assert len(q.pending()) == 1


def test_a_stale_link_is_detached_at_the_next_dispatch(rig, monkeypatch):
    q, h, c = rig
    assert G.WS_A in q._links
    real = h.registry.binding_state
    monkeypatch.setattr(h.registry, "binding_state", lambda ws: {**real(ws), "status": "unbound"})
    with pytest.raises(LinkError):
        c.request("snapshot_get", {})  # not_bound; the dispatch still detaches the stale link
    assert until(lambda: G.WS_A not in q._links)


def test_a_rebind_detaches_the_link_then_it_attaches_afresh(rig, monkeypatch):
    q, h, c = rig
    old = q._links[G.WS_A][0]
    real = h.registry.binding_state
    seq = real(G.WS_A)["binding_seq"]
    monkeypatch.setattr(h.registry, "binding_state", lambda ws: {**real(ws), "binding_seq": seq + 1})
    c.request("snapshot_get", {})  # the next dispatch sees binding_seq moved: detach, attach to the new binding
    assert old.dead and q._links[G.WS_A] and q._links[G.WS_A][0] is not old


def test_a_short_second_link_does_not_strand_the_main_links_queue(rig):
    """#51: the host's snapshot_get opens a second link while an event_deliver is in flight on the main one."""
    q, h, c = rig
    q(G.WS_A, LOG, ev())
    o = c.recv_reply()  # in flight on the main link; the host is busy fetching a snapshot on a second link
    assert o["op"] == "event_deliver"
    c2 = h.open()
    c2.request("snapshot_get", {})
    c2.close()
    assert until(lambda: len(q._links[G.WS_A]) == 1)  # the second link is gone, the main one is still attached
    c.send_raw(c.build("event_deliver", {}, re=o["seq"]))  # the host's ack arrives on the main link
    assert until(lambda: q.outcomes() and q.outcomes()[0]["state"] == "done")
    q(G.WS_A, LOG, ev(2))  # and later events flow
    assert answer(c, {})["event"]["id"] == ev(2)["id"]
    assert until(lambda: len(q.outcomes()) == 2)


def test_the_event_in_flight_on_a_closed_link_is_retried_on_the_remaining_link(rig):
    q, h, c = rig
    c2 = h.open()
    c2.request("snapshot_get", {})
    assert until(lambda: len(q._links[G.WS_A]) == 2)
    q._links[G.WS_A][0].strikes = 5  # the main link has been silent: the second one is tried first
    q(G.WS_A, LOG, ev())
    assert c2.recv_reply()["op"] == "event_deliver"
    c2.close()  # no reply, the link ends
    assert answer(c, {})["event"]["id"] == ev()["id"]  # after the 1 s backoff, on the remaining link
    assert until(lambda: q.outcomes() and q.outcomes()[0]["state"] == "done")


def test_a_reply_on_another_link_than_the_send_never_ends_the_event(rig):
    q, h, c = rig
    c2 = h.open()
    c2.request("snapshot_get", {})
    assert until(lambda: len(q._links[G.WS_A]) == 2)
    q(G.WS_A, LOG, ev())
    o = c.recv_reply()
    c2.send_raw(c2.build("event_deliver", {}, re=o["seq"]))  # same seq number, wrong link
    time.sleep(0.3)
    assert not q.outcomes() and len(q.pending()) == 1

"""R1 (#1589): link signatures bind the connection's TLS exporter and seq is strictly consecutive per connection."""

from __future__ import annotations

import pytest
from router_helpers import envelope, rhub  # noqa: F401

from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, cj
from orch_hub.contract.labels import L
from orch_hub.link import LinkError, framing, tls


def joined(hub):
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    return c


def refused(c, code):
    with pytest.raises(LinkError) as e:
        c.recv_reply()
    assert e.value.code == code
    with pytest.raises((tls.LinkClosed, tls.LinkTimeout)):
        framing.read_frame(c._c, first_timeout=2, rest_timeout=2)


def test_w2h_frame_replayed_on_a_new_connection_is_refused(hub):
    joined(hub).close()
    a = hub.open()
    frame = a.build("snapshot_get", {})  # frame 1 of connection A, recorded
    a.send_raw(frame)
    assert "snapshot" in a.recv_reply()["body"]
    seen = hub.registry.pin(G.WS_A)
    b = hub.open()  # a new connection of the same workspace, also at seq 0: the position matches
    b.send_raw(frame)
    refused(b, "bad_signature")
    assert hub.registry.pin(G.WS_A) == seen


def test_first_frame_must_be_seq_1(hub):
    c = hub.open(first=True)
    c.send_raw(c.build("join", hub.join_body(), seq=5))
    refused(c, "malformed")


@pytest.mark.parametrize("delta", [0, 2, -1], ids=["repeat", "skip", "backwards"])
def test_out_of_order_and_repeat_close_the_link(hub, delta):
    c = joined(hub)  # seq 1 used
    c.request("snapshot_get", {})  # seq 2 used
    c.send_raw(c.build("snapshot_get", {}, seq=c.seq + delta))  # 2 again, 4 (3 skipped), 1
    refused(c, "malformed")


def test_signed_without_binding_or_other_binding_is_bad_signature(hub):
    c = joined(hub)
    o = c.build("snapshot_get", {})["o"]
    old = {"o": o, "sig": b64u(S.sign(G.K.priv["wsk_a"], L["sig_hub_link"] + cj(o)))}  # the pre-R1 signature
    c.send_raw(old)
    refused(c, "bad_signature")
    c = hub.open()
    o = c.build("snapshot_get", {})["o"]
    other = {"o": o, "sig": b64u(S.sign(G.K.priv["wsk_a"], L["sig_hub_link"] + b"\x07" * 32 + cj(o)))}
    c.send_raw(other)
    refused(c, "bad_signature")


def test_reconnect_starts_at_1_and_works(hub):
    c = joined(hub)
    c.request("snapshot_get", {})
    c.close()
    c2 = hub.open()
    assert c2.seq == 0
    assert "snapshot" in c2.request("snapshot_get", {})


def test_two_concurrent_links_count_independently(hub):
    c = joined(hub)
    c2 = hub.open()
    for _ in range(3):
        assert "snapshot" in c.request("snapshot_get", {})
        assert "snapshot" in c2.request("snapshot_get", {})


def test_route_in_ack_replayed_on_a_new_connection_does_not_delete(rhub):  # noqa: F811
    h = rhub()
    a, b1 = h.join(G.WS_A), h.join(G.WS_B)
    sent = []
    real = b1.send_raw
    b1.send_raw = lambda obj: (sent.append(obj), real(obj))[1]
    a.route_out(envelope())
    b1.next_route_in()
    b1.ack_route_in()  # w2h seq 2 on b1 (after the join), re = the push's seq: recorded
    ack = sent[-1]
    b1.close()
    e2 = envelope()
    a.route_out(e2)
    b2 = h.reopen(G.WS_B)
    assert b2.next_route_in() == e2  # in flight on b2 with re = 1
    b2.seq = ack["o"]["seq"] - 1
    b2.send_raw(ack)
    refused(b2, "bad_signature")
    assert h.queue.depth(G.WS_B) == 1

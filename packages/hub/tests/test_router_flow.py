"""ws->ws routing over real links: byte-identical delivery, offline queue, ordering, acks, restart."""

# ruff: noqa: F811  (the `rhub` fixture is imported, then named as a test argument)
from __future__ import annotations

import threading

import pytest
from router_helpers import NOW, envelope, rhub  # noqa: F401

from orch_hub.contract import vectorgen as G
from orch_hub.link import Registry


def test_a_to_b_delivered_byte_identical(rhub):
    h = rhub()
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    env = envelope()
    assert a.route_out(env) == "forwarded"
    got = b.next_route_in()
    assert got == env  # the hub forwards the sender's exact bytes
    b.ack_route_in()
    assert _settle(h)


def _settle(h):
    import time

    for _ in range(50):
        if h.queue.depth(G.WS_B) == 0:
            return True
        time.sleep(0.05)
    return False


def test_cosigned_and_refusal_envelopes_pass_unchanged(rhub):
    h = rhub()
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    cos = envelope(flags=1, cosig=b'{"type":"device"}')
    ref = envelope(flags=2)
    a.route_out(cos)
    assert b.next_route_in() == cos
    b.ack_route_in()
    a.route_out(ref)
    assert b.next_route_in() == ref


def test_offline_recipient_is_queued_then_delivered_in_order(rhub):
    h = rhub()
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    b.close()  # B goes offline (its join link is closed)
    envs = [envelope() for _ in range(4)]
    for e in envs:
        assert a.route_out(e) == "queued"
    assert h.queue.depth(G.WS_B) == 4
    b2 = h.reopen(G.WS_B)
    for e in envs:
        assert b2.next_route_in() == e
        b2.ack_route_in()
    assert _settle(h)


def test_no_ack_no_next_and_resend_after_reconnect(rhub):
    h = rhub()
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    e1, e2 = envelope(), envelope()
    a.route_out(e1)
    a.route_out(e2)
    assert b.next_route_in() == e1
    with pytest.raises(Exception):  # noqa: B017  # no ack yet: e2 is not pushed (read times out)
        b._t = 0.5
        b.next_route_in()
    b.close()
    b2 = h.reopen(G.WS_B)
    assert b2.next_route_in() == e1  # at least once: the unacknowledged one comes again
    b2.ack_route_in()
    assert b2.next_route_in() == e2


def test_host_error_ack_drops_the_entry(rhub):
    h = rhub()
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    e1, e2 = envelope(), envelope()
    a.route_out(e1)
    a.route_out(e2)
    assert b.next_route_in() == e1
    b.ack_route_in(error="malformed")
    assert b.next_route_in() == e2


def test_same_envelope_twice_is_queued_once(rhub):
    h = rhub()
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    b.close()
    e = envelope()
    a.route_out(e)
    a.route_out(e)
    assert h.queue.depth(G.WS_B) == 1


def test_queue_survives_a_hub_restart(rhub, tmp_path):
    reg_path, q_path = str(tmp_path / "r.db"), str(tmp_path / "q.db")
    h = rhub(registry=Registry(reg_path), queue_path=q_path)
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    b.close()
    envs = [envelope() for _ in range(3)]
    for e in envs:
        a.route_out(e)
    h.close()
    h2 = rhub(registry=Registry(reg_path), queue_path=q_path)
    b2 = h2.reopen(G.WS_B)
    for e in envs:
        assert b2.next_route_in() == e
        b2.ack_route_in()


def test_many_concurrent_routes_while_the_recipient_reads(rhub):
    h = rhub()
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    envs = [envelope() for _ in range(25)]
    errs = []

    def send():
        try:
            for e in envs:
                a.route_out(e)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    t = threading.Thread(target=send)
    t.start()
    got = []
    for _ in envs:
        got.append(b.next_route_in())
        b.ack_route_in()
    t.join(10)
    assert not errs and got == envs


def test_ttl_expires_old_entries(rhub):
    from orch_hub.router import Limits

    h = rhub(limits=Limits(ttl_ms=1000))
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    b.close()
    a.route_out(envelope())
    assert h.queue.head(G.WS_B, NOW) is not None
    assert h.queue.head(G.WS_B, NOW + 5000) is None


def test_duplicate_handoff_is_one_inbox_item_and_one_route_in(rhub, tmp_path):
    import hashlib

    from orch_hub.inspect import _routes

    q_path = tmp_path / "route-queue.db"
    h = rhub(queue_path=str(q_path))
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    e = envelope()
    a.route_out(e)
    assert b.next_route_in() == e
    b.ack_route_in()
    assert _settle(h)
    a.route_out(e)  # the same handoff again, after delivery
    assert h.queue.depth(G.WS_B) == 0
    b._t = 0.5
    with pytest.raises(Exception):  # noqa: B017
        b.next_route_in()
    assert h.registry._db.execute("SELECT count FROM op_counts WHERE op='route_in'").fetchone() == (1,)
    (r,) = _routes(tmp_path)
    assert r == {"id": e[8:24].hex(), "from": G.WS_A, "to": G.WS_B, "sha256": hashlib.sha256(e).hexdigest()}

"""D12b item 3: strict per-link seq (G7) and the cap on unauthenticated connections (G8)."""

from __future__ import annotations

import socket
import time

import pytest

from orch_hub.contract import vectorgen as G
from orch_hub.link import LinkError, framing, tls


def joined(hub):
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    return c


def _closed(c) -> bool:
    try:
        framing.read_frame(c._c, first_timeout=2, rest_timeout=2)
    except tls.LinkClosed, tls.LinkTimeout:
        return True
    return False


def test_consecutive_seq_is_served(hub):
    c = joined(hub)
    for _ in range(3):
        assert "snapshot" in c.request("snapshot_get", {})


def test_a_repeated_seq_closes_the_link_before_any_effect(hub):
    c = joined(hub)
    c.request("snapshot_get", {})
    last = c.seq
    c.send_raw(c.build("snapshot_get", {}, seq=last))  # the previous seq again
    with pytest.raises(LinkError) as e:
        c.recv_reply()
    assert e.value.code == "malformed"
    assert _closed(c)


def test_a_skipped_seq_closes_the_link(hub):
    c = joined(hub)
    c.send_raw(c.build("snapshot_get", {}, seq=c.seq + 5))
    with pytest.raises(LinkError) as e:
        c.recv_reply()
    assert e.value.code == "malformed"
    assert _closed(c)


def test_a_replayed_join_is_not_applied_twice(hub):
    c = joined(hub)
    first = c.build("snapshot_get", {})
    c.send_raw(first)
    c.recv_reply()
    c.send_raw(first)  # byte-identical replay of a signed frame
    with pytest.raises(LinkError):
        c.recv_reply()
    assert _closed(c)


def test_first_frame_sets_the_baseline_and_a_new_link_may_continue_numbering(hub):
    c = hub.open(first=True)
    c.seq = 40  # a host numbers across reconnects (core HubLink); the first frame of a link is the baseline
    c.request("join", hub.join_body())
    assert "snapshot" in c.request("snapshot_get", {})
    c.close()
    c2 = hub.open()
    c2.seq = 44
    assert "snapshot" in c2.request("snapshot_get", {})
    assert "snapshot" in c2.request("snapshot_get", {})


def test_stalled_handshakes_cannot_take_every_slot(make_hub):
    h = make_hub(max_connections=4, max_unauthenticated=2, handshake_timeout=1.5, read_timeout=1.5)
    bound = joined(h)  # authenticated: no longer counts against the unauthenticated cap
    stall = [socket.create_connection(("127.0.0.1", h.port), timeout=5) for _ in range(2)]  # TCP only, no TLS bytes
    time.sleep(0.3)
    extra = socket.create_connection(("127.0.0.1", h.port), timeout=5)
    extra.settimeout(3)
    assert extra.recv(1) == b""  # over the unauthenticated cap: closed at once
    assert "snapshot" in bound.request("snapshot_get", {})  # the bound host is unaffected
    deadline = time.monotonic() + 8
    ok = None
    while time.monotonic() < deadline and ok is None:  # the stallers hit the handshake deadline and free their slots
        try:
            ok = h.open(ws=G.WS_A, first=False)
        except tls.LinkClosed, tls.LinkTimeout, OSError, LinkError:
            time.sleep(0.3)
    assert ok is not None
    for s in (*stall, extra):
        s.close()


def test_cap_is_released_after_auth(make_hub):
    h = make_hub(max_connections=8, max_unauthenticated=1)
    first = joined(h)
    others = [h.open(ws=G.WS_A, first=True) for _ in range(3)]  # more open authenticated links than the cap
    assert "snapshot" in first.request("snapshot_get", {})
    assert len(others) == 3


def _opens(h, **kw):
    """True when a new link authenticates; a refused one is closed before or during the handshake."""
    try:
        c = h.open(first=True, **kw)
    except tls.LinkClosed, tls.LinkTimeout, OSError, LinkError:
        return None
    return c


def test_self_asserted_links_keep_their_slot_until_the_join_pins_them(make_hub):
    h = make_hub(max_connections=8, max_unauthenticated=2, unjoined_idle_timeout=30, unjoined_lifetime=30)
    a, b = _opens(h), _opens(h)  # unpinned workspace: the key is self-asserted, nothing is proven yet
    assert a is not None and b is not None
    assert _opens(h) is None  # a third such link is over the cap, however many are opened
    a.request("join", h.join_body())  # the join pins this link's key: authenticated, slot released
    assert _opens(h) is not None


def test_unjoined_link_has_a_lifetime_peer_frames_cannot_reset(make_hub):
    h = make_hub(unjoined_idle_timeout=30, unjoined_lifetime=1.0)
    c = h.open(first=True)
    t0 = time.monotonic()
    closed = False
    while time.monotonic() - t0 < 4:
        try:
            c.request("join", h.join_body(invite="nope"))  # refused, the link stays unjoined; each frame is progress
        except LinkError:
            pass
        except tls.LinkClosed, tls.LinkTimeout, OSError:
            closed = True
            break
        time.sleep(0.2)
    assert closed and time.monotonic() - t0 < 3


def test_unjoined_link_ends_after_its_first_non_join_op(hub):
    c = hub.open(first=True)
    with pytest.raises(LinkError) as e:
        c.request("snapshot_get", {})
    assert e.value.code == "unknown_workspace"
    with pytest.raises((tls.LinkClosed, tls.LinkTimeout, OSError)):
        c.request("snapshot_get", {})


def test_a_failed_join_may_be_retried_inside_the_lifetime(hub):
    c = hub.open(first=True)
    with pytest.raises(LinkError):
        c.request("join", hub.join_body(invite="nope"))
    c.request("join", hub.join_body())

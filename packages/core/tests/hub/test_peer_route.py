"""C12: ws->ws over the hub router, two workspaces (in-process router with the contract's queue semantics)."""

from __future__ import annotations

import pytest

from orch.hubclient import wsenvelope as W
from orch.hubclient.errors import HubRefused
from orch.hubclient.peerservice import PeerService

from .routefake import RouteHub
from .test_wsenvelope import NOW, A, B, Side, pair


class Node:
    def __init__(self, side: Side, hub: RouteHub) -> None:
        self.side, self.hub, self.saves = side, hub, 0
        self.svc = PeerService(
            me=side.ws, sign=side.sign, wxk_for=lambda v: (side.wxk, None) if v == 1 else None, wxk_current=1,
            mailbox=side.mb, save=self._save, connect=lambda: hub.link(side.ws), now_ms=lambda: NOW,
        )  # fmt: skip

    def _save(self, mb: W.Mailbox) -> None:
        self.saves += 1
        W.Mailbox.from_json(mb.to_json())  # persistable at every save


def world() -> tuple[RouteHub, Node, Node]:
    a, b = pair()
    hub = RouteHub({A, B})
    return hub, Node(a, hub), Node(b, hub)


def drain(n: Node) -> list[W.Outcome]:
    out = []
    while (o := n.svc.pump(0)) is not None:
        out.append(o)
    return out


def test_route_send_inbox_reply_clears_waiting_on_peer() -> None:
    _, a, b = world()
    sent = a.svc.send_ticket(B, {"title": "do it"}, deadline_ms=NOW + 3_600_000)
    assert sent["state"] == "queued"
    assert a.svc.sent_status(sent["envelope_id"])["status"] == "waiting_on_peer"
    assert [o.action for o in drain(b)] == ["inbox"]
    (item,) = b.svc.inbox()
    assert item["envelope_sha256"] == sent["envelope_sha256"] and item["from_ws"] == A
    assert b.svc.accept(item["id"])["ticket"]["title"] == "do it"
    b.svc.send_result(item["id"], {"summary": "done"})
    assert [o.action for o in drain(a)] == ["deliver"]
    st = a.svc.sent_status(sent["envelope_id"])
    assert st["status"] == "answered" and st["peer_result"]["summary"] == "done" and st["peer_result"]["from_ws"] == B
    with pytest.raises(W.EnvelopeError) as e:
        b.svc.send_result(item["id"], {"summary": "again"})
    assert e.value.code == "already_replied"


def test_route_recipient_offline_gets_it_on_reconnect() -> None:
    hub, a, b = world()
    hub.down.add(B)
    sent = a.svc.send_ticket(B, {"title": "later"}, deadline_ms=NOW + 3_600_000)
    assert sent["state"] == "queued"  # A is up: the hub holds it
    with pytest.raises(OSError):
        b.svc.pump(0)
    hub.down.discard(B)
    assert [o.action for o in drain(b)] == ["inbox"]
    assert b.svc.inbox()[0]["envelope_sha256"] == sent["envelope_sha256"]


def test_route_sender_hub_down_keeps_outbox_and_resends_same_bytes() -> None:
    hub, a, b = world()
    hub.down.add(A)
    sent = a.svc.send_ticket(B, {"title": "x"}, deadline_ms=NOW + 3_600_000)
    assert sent["state"] == "outbox" and a.side.mb.outbox
    hub.down.discard(A)
    assert a.svc.flush() == [sent["envelope_id"]] and not a.side.mb.outbox
    assert [o.action for o in drain(b)] == ["inbox"]


def test_route_outbox_survives_a_host_restart_and_is_delivered_once() -> None:
    """V3 D78 (#1103): the unsent envelope is in the saved mailbox; a new process flushes the same bytes."""
    hub, a, b = world()
    hub.down.add(A)
    sent = a.svc.send_ticket(B, {"title": "survive"}, deadline_ms=NOW + 3_600_000)
    assert sent["state"] == "outbox"
    held = dict(a.side.mb.outbox)
    a.svc.close()
    a.side.mb = W.Mailbox.from_json(a.side.mb.to_json())  # the host restarted: only the saved file is left
    a2 = Node(a.side, hub)
    assert a2.side.mb.outbox == held
    hub.down.discard(A)
    assert a2.svc.flush() == [sent["envelope_id"]] and not a2.side.mb.outbox
    assert [o.action for o in drain(b)] == ["inbox"]
    assert a2.svc.flush() == []  # nothing left to resend: a second pass sends nothing
    assert len(b.svc.inbox()) == 1


def test_route_unpinned_sender_refused_and_acked_with_error() -> None:
    hub, a, b = world()
    a.svc.send_ticket(B, {"title": "x"}, deadline_ms=NOW + 3_600_000)
    b.side.mb.peers.clear()
    (o,) = drain(b)
    assert (o.action, o.code) == ("drop", "not_pinned")
    assert hub.acks == [(B, "not_pinned")] and hub.queues[B] == [] and not b.side.mb.inbox
    assert not b.side.mb.outbox  # an unpinned sender is never answered


def test_route_ack_only_after_save() -> None:
    hub, a, b = world()
    a.svc.send_ticket(B, {"title": "x"}, deadline_ms=NOW + 3_600_000)
    order: list[str] = []
    orig = b.svc._save
    b.svc._save = lambda mb: (order.append("save"), orig(mb))[1]
    link = b.svc._ensure()
    real_ack = link.ack_route_in
    link.ack_route_in = lambda error=None: (order.append("ack"), real_ack(error))[1]
    drain(b)
    assert order == ["save", "ack"]


def test_route_duplicate_delivery_makes_one_inbox_item() -> None:
    hub, a, b = world()
    sent = a.svc.send_ticket(B, {"title": "x"}, deadline_ms=NOW + 3_600_000)
    hub.queues[B].append(hub.queues[B][0])  # the hub redelivers (at least once)
    assert [o.action for o in drain(b)] == ["inbox", "duplicate"]
    assert len(b.svc.inbox()) == 1 and hub.acks == [(B, None), (B, None)] and sent


def test_route_unacked_entry_is_redelivered_after_a_dropped_link() -> None:
    hub, a, b = world()
    a.svc.send_ticket(B, {"title": "x"}, deadline_ms=NOW + 3_600_000)
    b.svc._ensure().next_route_in(0)  # taken but never acknowledged
    b.svc.drop_link()
    assert [o.action for o in drain(b)] == ["inbox"]


def test_route_one_mib_ok_and_over_cap_refused_before_send() -> None:
    hub, a, b = world()
    sent = a.svc.send_ticket(B, {"pad": "x" * (W.MAX_ENVELOPE - 700)}, deadline_ms=NOW + 3_600_000)
    assert [o.action for o in drain(b)] == ["inbox"]
    assert b.svc.inbox()[0]["envelope_sha256"] == sent["envelope_sha256"]
    before = len(hub.seen)
    with pytest.raises(W.EnvelopeError) as e:
        a.svc.send_ticket(B, {"pad": "x" * W.MAX_ENVELOPE}, deadline_ms=NOW + 3_600_000)
    assert e.value.code == "too_large" and len(hub.seen) == before and len(a.side.mb.sent) == 1


def test_route_hub_refusal_not_allowed_leaves_no_record() -> None:
    hub, a, _ = world()
    hub.denied.add((A, B))
    with pytest.raises(HubRefused) as e:
        a.svc.send_ticket(B, {"title": "x"}, deadline_ms=NOW + 3_600_000)
    assert e.value.code == "not_allowed" and not a.side.mb.sent and not a.side.mb.outbox


def test_route_peer_unknown_to_sender_refused_locally() -> None:
    _, a, _ = world()
    a.side.mb.peers.clear()
    with pytest.raises(W.EnvelopeError) as e:
        a.svc.send_ticket(B, {"title": "x"}, deadline_ms=NOW + 3_600_000)
    assert e.value.code == "unknown_peer"


def test_route_refusal_goes_back_through_the_outbox() -> None:
    hub, a, b = world()
    sent = a.svc.send_ticket(B, {"title": "x"}, deadline_ms=NOW + 3_600_000, depth=2)
    (o,) = drain(b)
    assert (o.action, o.code) == ("refuse", "depth_exceeded") and hub.acks == [(B, "depth_exceeded")]
    assert b.svc.flush()
    assert [o.action for o in drain(a)] == ["deliver"]
    st = a.svc.sent_status(sent["envelope_id"])
    assert st["peer_result"]["refusal"] == "depth_exceeded"


def test_route_in_after_the_binding_is_gone_is_not_stored_nor_acked() -> None:
    hub, a, b = world()
    a.svc.send_ticket(B, {"title": "x"}, deadline_ms=NOW + 3_600_000)
    open_gate = [True]
    b.svc._gate = lambda: open_gate[0]
    open_gate[0] = False  # unbound / rebound between the push and the store
    with pytest.raises(OSError):
        b.svc.pump(0)
    assert not b.side.mb.inbox and not b.side.mb.seen and hub.acks == [] and len(hub.queues[B]) == 1
    open_gate[0] = True
    assert [o.action for o in drain(b)] == ["inbox"]  # a later, valid binding still gets it (the hub kept it)


def test_route_a_failed_save_is_not_acked_and_the_redelivery_is_stored() -> None:
    hub, a, b = world()
    a.svc.send_ticket(B, {"title": "x"}, deadline_ms=NOW + 3_600_000)
    real, fail = b.svc._save, [True]

    def flaky(mb):
        if fail[0]:
            raise OSError("disk full")
        real(mb)

    b.svc._save = flaky
    with pytest.raises(OSError):
        b.svc.pump(0)
    assert hub.acks == [] and not b.side.mb.inbox and not b.side.mb.seen  # nothing recorded, hub keeps the entry
    fail[0] = False
    assert [o.action for o in drain(b)] == ["inbox"]  # stored, not dropped as a duplicate
    assert hub.acks == [(B, None)]


def test_status_of_a_stored_odd_result_cannot_crash() -> None:
    _, a, _ = world()
    a.side.mb.sent["ab" * 16] = {
        "to": B, "consumed": ["result"], "state": "answered",
        "reply": {"kind": "result", "from_ws": B, "late": False, "result": "just text"},
    }  # fmt: skip
    assert a.svc.sent_status("ab" * 16)["peer_result"]["summary"] is None


def test_concurrent_senders_and_the_receive_thread_share_one_link() -> None:
    """The flake class of develop CI (#998): the sender and the receive thread both found no link and both dialed; a
    hub that serves one link at a time then left one of them waiting until its timeout. One link per workspace."""
    import threading
    import time as _time

    _, a, _ = world()
    dials, gate = [], threading.Barrier(3)
    real = a.svc._connect

    def slow_connect():
        dials.append(1)
        _time.sleep(0.2)  # a handshake takes a while: the other callers arrive meanwhile
        return real()

    a.svc._connect = slow_connect
    links = []

    def go():
        gate.wait()
        links.append(a.svc._ensure())

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    gate.wait()
    for t in ts:
        t.join(5)
    assert len(dials) == 1 and links[0] is links[1]


def test_a_second_user_of_the_link_can_peek_without_dialing_and_drop_only_its_own_link() -> None:
    """V3 C14b: the log_sync sender shares the one link; it peeks (never dials) and drops only the link that failed."""
    _, a, _b = world()
    assert a.svc.current_link() is None  # peeking opens nothing
    first = a.svc._ensure()  # noqa: SLF001
    assert a.svc.current_link() is first
    a.svc.drop_link(only=object())  # not the current link: nothing happens
    assert a.svc.current_link() is first
    a.svc.drop_link(only=first)
    assert a.svc.current_link() is None
    second = a.svc._ensure()  # noqa: SLF001
    assert second is not first
    a.svc.drop_link(only=first)  # a stale handle must not drop the newer link
    assert a.svc.current_link() is second

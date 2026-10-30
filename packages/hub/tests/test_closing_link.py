"""#1463 (Opus F4): the link server under a removal. The allow list is default deny, the notice is pushed on a live current
plain link only, and the host's acknowledgement (a reply with ``re``) disturbs neither the router nor the delivery queue."""

from __future__ import annotations

import pytest
from conftest import Hub  # noqa: F401  (fixtures come from conftest)

from orch_hub.closing import LINK_ALLOWED
from orch_hub.contract import hub as H
from orch_hub.contract import vectorgen as G
from orch_hub.link.client import LinkError


class FakeClosing:
    def __init__(self, active=False):
        self._active = active
        self.sent_for: list[str] = []

    def active(self):
        return self._active

    def notice_for(self, ws):
        self.sent_for.append(ws)


def _joined(make_hub, closing, **kw):
    h = make_hub(closing=closing, **kw)
    c = h.open(first=True)
    c.request("join", h.join_body())
    return h, c


def test_a_listed_op_is_served_and_every_other_op_answers_hub_closing(make_hub):
    fc = FakeClosing()
    _h, c = _joined(make_hub, fc)
    fc._active = True
    assert "snapshot" in c.request("snapshot_get", {})
    assert c.request("closing_get", {}) == {"closing": None}
    empties = [op for op, (d, _m, spec) in H.LINK_OPS.items() if d == "w2h" and not spec and op not in LINK_ALLOWED]
    assert "space_list" in empties
    for op in empties:  # every listed w2h op that takes {} and is not on the allow list
        with pytest.raises(LinkError) as e:
            c.request(op, {})
        assert e.value.code == "hub_closing", op
    assert (
        {"snapshot_get", "closing_get"} <= LINK_ALLOWED
        and "join" not in LINK_ALLOWED
        and "route_out" not in LINK_ALLOWED
    )


def test_nothing_changes_while_the_hub_is_not_closing(make_hub):
    _h, c = _joined(make_hub, FakeClosing())
    with pytest.raises(LinkError) as e:
        c.request("space_list", {})
    assert e.value.code != "hub_closing"


def test_a_join_is_refused_while_closing(make_hub):
    fc = FakeClosing()
    fc._active = True
    h = make_hub(closing=fc)
    c = h.open(first=True)
    with pytest.raises(LinkError) as e:
        c.request("join", h.join_body())
    assert e.value.code == "hub_closing"


def _ack(c, req):
    """The host's reply to a pushed ``closing_notice`` (same op, ``re`` = the push's seq, body {})."""
    c.seq += 1
    c.send_raw(
        c._wsk.sign_link(
            {
                "v": 2,
                "suite": 2,
                "kind": "hub.link",
                "hv": 0,
                "hub_id": c.hub_id,
                "workspace_id": c.workspace_id,
                "dir": "w2h",
                "seq": c.seq,
                "ts_ms": c._now(),
                "op": "closing_notice",
                "re": req["seq"],
                "body": {},
            },
            c._c.exporter(),
        )
    )


def test_push_closing_reaches_a_live_current_link_and_the_ack_disturbs_nothing(make_hub):
    fc = FakeClosing()
    h, c = _joined(make_hub, fc)
    fc._active = True
    note = {"o": {"kind": "hub.closing"}, "sig": "x"}
    assert h.server.push_closing(G.WS_A, note) is True
    req = c.recv_reply()
    assert req["op"] == "closing_notice" and req["re"] is None and req["body"] == {"notice": note}
    _ack(c, req)
    # the link goes on, with correct sequence numbers, and nothing else was touched
    assert "snapshot" in c.request("snapshot_get", {})
    assert h.server.push_closing("ff" * 16, note) is False  # not a workspace with a live link


def test_push_closing_sends_nothing_after_the_link_is_gone(make_hub):
    h, c = _joined(make_hub, FakeClosing())
    c.close()
    import time

    time.sleep(0.5)
    assert h.server.push_closing(G.WS_A, {"o": {}, "sig": "x"}) is False

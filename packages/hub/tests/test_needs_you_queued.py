"""The approvals inbox exposes only the caller's pending signed events."""
import pytest
from webapi_helpers import WS, WS_OTHER, Web
from test_deliver_queue import ev, LOG, Port

from orch_hub.custodial.deliver import DeliveryQueue


@pytest.fixture
def w(tmp_path):
    world = Web(tmp_path)
    yield world
    world.close()


def test_no_delivery_dependency_is_an_empty_queue(w):
    status, body, _ = w.call("GET", "/me/needs-you", token=w.login(w.ann))
    assert status == 200
    assert body["queued"] == []


@pytest.mark.parametrize("online", [True, False])
def test_only_own_pending_events_in_accessible_workspaces(w, online):
    if not online:
        w.reg.add(WS, "full", status="unbound")
    queue = DeliveryQueue(":memory:", now_ms=lambda: 1234)
    w.api.d.delivery = queue
    try:
        for n, user, workspace in [(1, w.ann, WS), (2, w.admin, WS), (3, w.ann, WS_OTHER)]:
            event = ev(n)
            event["actor"]["id"] = "u_" + user
            queue(workspace, LOG, event)
        status, body, _ = w.call("GET", "/me/needs-you", token=w.login(w.ann))
        assert status == 200
        assert body["queued"] == [{"workspace_id": WS, "type": "gate.approved", "queued_ms": 1234}]
        assert set(body["queued"][0]) == {"workspace_id", "type", "queued_ms"}
        port = Port()
        link = queue.attach(WS, port)
        queue.pump()
        queue.on_reply(WS, link, {"op": "event_deliver", "re": port.seq, "body": {}})
        _, body, _ = w.call("GET", "/me/needs-you", token=w.login(w.ann))
        assert body["queued"] == []
    finally:
        queue.close()

"""#1463 (hub protocol 9.7, 10.2): the h2w ``closing_notice`` on the TLS link client. A queue of its own like
``route_in`` and ``event_deliver``: taken during a request, handed out once, acknowledged with ``{}`` or a code, bound
at ``MAX_QUEUED`` (refuse, never drop). The link checks the body shape only; the notice itself is the host's."""

from __future__ import annotations

import time

import pytest

from orch.hubclient.tlslink import framing
from orch.hubclient.tlslink.client import MAX_QUEUED, LinkError
from orch.hubclient.tlslink.tls import LinkClosed

from .test_link_client import make_hub, open_link  # noqa: F401 - fixture
from .tlshub import TlsHub


def push_notice(hub: TlsHub, body: dict) -> None:
    o = {
        "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": hub.hub_id, "workspace_id": hub.workspace_id,
        "dir": "h2w", "seq": hub.next_seq(), "ts_ms": int(time.time() * 1000), "op": "closing_notice", "re": None,
        "body": body,
    }  # fmt: skip
    framing.write_frame(hub._conns[-1], hub.sign_frame(o), timeout=5)  # noqa: SLF001


def test_a_notice_is_taken_during_a_request_handed_out_once_and_acked(make_hub) -> None:  # noqa: F811
    seen: list[dict] = []

    def on_request(hub: TlsHub, o: dict):
        if o["op"] == "snapshot_get":
            push_notice(hub, {"notice": {"o": {"kind": "hub.closing"}, "sig": "x"}})  # before the reply
            return hub.reply(o)
        seen.append(o)  # the host's ack
        return b""

    hub = make_hub(on_request=on_request)
    with open_link(hub) as link:
        link.call("snapshot_get", {})
        assert link.next_route_in(0) is None and link.next_event_deliver(0) is None  # not theirs
        assert link.next_closing_notice(0) == {"o": {"kind": "hub.closing"}, "sig": "x"}
        with pytest.raises(LinkError):  # one outstanding at most
            link.next_closing_notice(0)
    with open_link(hub) as link:
        link.call("snapshot_get", {})
        link.next_closing_notice(0)
        link.ack_closing_notice("stale_binding")
        deadline = time.time() + 3
        while not seen and time.time() < deadline:
            time.sleep(0.02)
    assert seen and seen[0]["op"] == "closing_notice" and seen[0]["re"] == 1
    assert seen[0]["body"] == {"error": "stale_binding"}


def test_an_ack_without_a_notice_is_refused(make_hub) -> None:  # noqa: F811
    hub = make_hub()
    with open_link(hub) as link, pytest.raises(LinkError):
        link.ack_closing_notice()


@pytest.mark.parametrize("body", [{}, {"notice": "x"}, {"notice": []}, {"notice": {}, "x": 1}, {"event": {}}])
def test_a_notice_with_a_bad_body_closes_the_link(make_hub, body) -> None:  # noqa: F811
    def on_request(hub: TlsHub, o: dict):
        push_notice(hub, body)
        return hub.reply(o)

    hub = make_hub(on_request=on_request)
    with open_link(hub) as link, pytest.raises(LinkError) as e:
        link.call("snapshot_get", {})
    assert e.value.code == "malformed"


def test_the_notice_queue_is_bounded_and_a_full_one_closes_the_link_with_queue_full(make_hub) -> None:  # noqa: F811
    def on_request(hub: TlsHub, o: dict):
        for _ in range(MAX_QUEUED + 1):
            push_notice(hub, {"notice": {}})
        return hub.reply(o)

    hub = make_hub(on_request=on_request)
    with open_link(hub) as link:
        with pytest.raises(LinkError) as e:
            link.call("snapshot_get", {})
        assert e.value.code == "queue_full"
        with pytest.raises(LinkClosed):
            link.next_closing_notice(0)

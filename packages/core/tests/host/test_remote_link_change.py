"""R4 T3 (#1592) host fixes. F3: a burst of hub stream frames while the host waits for a ``log_sync`` / ``route_out``
reply must close only that stream (``remote.busy``), never the shared link. F4: a new (or lost) hub link closes every
stream of the old one at once, so the per-device / per-workspace limits are free again and no frame goes out."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from orch.crypto.encoding import b64u
from orch.host.remote import pump
from orch.hubclient.tlslink import framing
from orch.hubclient.tlslink.client import MAX_QUEUED
from tests.host import human_helpers
from tests.host.remote_world import World
from tests.hub.test_link_client import open_link
from tests.hub.tlshub import TlsHub

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

SID = "ab" * 16
DEV = "rd_" + "cd" * 16


@pytest.fixture
def make_hub():
    from tests.hub.test_link_client import WSK_PUB

    hubs: list[TlsHub] = []

    def make(**kw: Any) -> TlsHub:
        h = TlsHub(wsk_pub=WSK_PUB, **kw).start()
        hubs.append(h)
        return h

    yield make
    for h in hubs:
        h.close()


def push(hub: TlsHub, op: str, body: dict[str, Any]) -> int:
    import time as _t

    o = {
        "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": hub.hub_id, "workspace_id": hub.workspace_id,
        "dir": "h2w", "seq": hub.next_seq(), "ts_ms": int(_t.time() * 1000), "op": op, "re": None, "body": body,
    }  # fmt: skip
    framing.write_frame(hub._conns[-1], hub.sign_frame(o), timeout=5)  # noqa: SLF001
    return o["seq"]


def data_body(sid: str, n: int) -> dict[str, Any]:
    return {"stream_id": sid, "seq": n, "bytes": b64u(b"x")}


# --- F3 ---------------------------------------------------------------------------------------------------------------


def test_stream_burst_during_a_call_closes_only_that_stream_and_keeps_the_link(make_hub) -> None:
    def on_request(hub: TlsHub, o: dict):
        if o["op"] == "snapshot_get":
            for n in range(1, MAX_QUEUED + 20):  # more than the queue holds, while the host waits for this reply
                push(hub, "stream_data", data_body(SID, n))
            return hub.reply(o)
        return b""

    hub = make_hub(on_request=on_request)
    with open_link(hub) as link:
        link.call("snapshot_get", {})  # no queue_full: the link survives the burst
        assert link.is_open
        assert link.take_stream_overflow() == [SID]  # named once, however many frames were lost
        assert link.take_stream_overflow() == []
        link.call("snapshot_get", {})  # still usable (log_sync, handoffs share it)


def test_stream_open_that_finds_the_queue_full_is_answered_busy(make_hub) -> None:
    seen: list[dict] = []

    def on_request(hub: TlsHub, o: dict):
        if o["op"] == "snapshot_get":
            for n in range(1, MAX_QUEUED + 1):
                push(hub, "stream_data", data_body(SID, n))
            push(hub, "stream_open", {"stream_id": "ef" * 16, "device_id": DEV, "kind": "session"})
            return hub.reply(o)
        seen.append(o)
        return b""

    hub = make_hub(on_request=on_request)
    with open_link(hub) as link:
        link.call("snapshot_get", {})
        deadline = time.time() + 3
        while not seen and time.time() < deadline:
            time.sleep(0.02)
        assert link.is_open
        assert [(o["op"], o["body"]) for o in seen] == [("stream_open", {"error": "remote.busy"})]
        assert seen[0]["re"] is not None


def test_route_in_overflow_still_closes_the_link(make_hub) -> None:
    """The change is for stream frames only: a full ``event_deliver`` queue keeps its refuse-never-drop rule."""
    from orch.hubclient.tlslink.client import LinkError
    from tests.hub.test_link_client import _push_event

    def on_request(hub: TlsHub, o: dict):
        for _ in range(MAX_QUEUED + 1):
            _push_event(hub, {"log": "workspace", "event": {"type": "x"}})
        return hub.reply(o)

    hub = make_hub(on_request=on_request)
    with open_link(hub) as link, pytest.raises(LinkError) as e:
        link.call("snapshot_get", {})
    assert e.value.code == "queue_full"


def test_pump_closes_the_overflowed_stream_busy_and_leaves_the_others(human, served) -> None:
    w = World(human, served)
    a, b = w.client(), w.client()
    assert a.connect()["t"] == "opened" and b.connect()["t"] == "opened"
    w.link.take_stream_overflow = lambda: [a.sid]
    w.pump()
    assert w.link.closes(a.sid) == ["remote.busy"] and w.link.closes(b.sid) == []
    assert not w.table().is_open(a.sid) and w.table().is_open(b.sid)


# --- F4 ---------------------------------------------------------------------------------------------------------------


def test_a_new_link_closes_every_stream_of_the_old_one_without_a_frame(human, served) -> None:
    from tests.host.remote_world import Link

    w = World(human, served)
    a, b = w.client(), w.client()
    a.connect()
    b.connect()
    assert w.table().count_device(w.did) == 2
    new = Link()
    pump.pump(w.service, new)
    assert w.table().count_device(w.did) == 0 and w.table().count_workspace() == 0
    assert new.sent == [] and new.answers == []  # nothing for ids the new link never opened
    assert w.sess(a.sid) is None or w.sess(a.sid).state == "closed"
    w.pump()  # the end is logged once
    assert w.log_types().count("remote.session_closed") == 2


def test_a_link_change_frees_the_per_device_limit(human, served) -> None:
    from tests.host.remote_world import Link

    w = World(human, served)
    for _ in range(4):
        assert w.client().open_stream() is None
    assert w.client().open_stream() == "remote.busy"  # 4 per device
    new = Link()
    pump.pump(w.service, new)
    w.link = new
    assert w.client().open_stream() is None


def test_the_same_link_keeps_its_streams(human, served) -> None:
    w = World(human, served)
    a = w.client()
    a.connect()
    w.pump()
    w.pump()
    assert w.table().is_open(a.sid)


def test_run_closes_the_streams_when_the_link_is_gone(human, served) -> None:
    w = World(human, served)
    a = w.client()
    a.connect()
    current: list[Any] = [w.link]
    stop = threading.Event()
    t = threading.Thread(target=pump.run, args=(w.service, lambda: current[0], stop, 0.01), daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        assert w.table().is_open(a.sid)
        current[0] = None
        deadline = time.time() + 3
        while w.table().is_open(a.sid) and time.time() < deadline:
            time.sleep(0.02)
        assert not w.table().is_open(a.sid)
    finally:
        stop.set()
        t.join(3)

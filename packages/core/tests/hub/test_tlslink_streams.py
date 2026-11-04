"""R2 T2b (#1590): remote access stream ops on the TLS link (hub contract 10.2c): h2w ``stream_open`` (request) and
one-way ``stream_data`` / ``stream_close`` in both directions; a w2h ``stream_open`` is ``unknown_op``."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from orch.canon.jcs import dumps as cj
from orch.crypto import suite
from orch.crypto.encoding import b64u
from orch.hubclient.tlslink import client as C
from orch.hubclient.tlslink import framing
from orch.hubclient.tlslink.client import LinkError

from .test_link_client import WSK, open_link, send
from .tlshub import TlsHub


@pytest.fixture
def make_hub() -> Iterator:
    hubs: list[TlsHub] = []

    def make(**kw: Any) -> TlsHub:
        h = TlsHub(wsk_pub=suite.public_bytes(WSK), **kw).start()
        hubs.append(h)
        return h

    yield make
    for h in hubs:
        h.close()


SID = "ab" * 16
DEV = "rd_" + "cd" * 16
CODE = "rc_" + "ef" * 16


def push(hub: TlsHub, op: str, body: dict, seq: int | None = None, **fields: Any) -> None:
    """One h2w push signed over the connection's exporter (R1); ``seq`` defaults to the hub's next consecutive one."""
    o = {
        "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": hub.hub_id, "workspace_id": hub.workspace_id,
        "dir": "h2w", "seq": hub.next_seq() if seq is None else seq, "ts_ms": int(time.time() * 1000),
        "op": op, "re": None, "body": body,
    }  # fmt: skip
    o.update(fields)
    framing.write_frame(hub._conns[-1], hub.sign_frame(o), timeout=5)  # noqa: SLF001


def opening(kind: str = "session", device: str = DEV) -> dict:
    return {"stream_id": SID, "device_id": device, "kind": kind}


def hub_that_pushes(make_hub, *frames: tuple[Any, ...]):
    got: list[dict] = []

    def on_request(hub: TlsHub, o: dict):
        if o["op"] == "snapshot_get":
            for frame in frames:  # (op, body[, seq]); they arrive before the reply
                push(hub, *frame)
            return hub.reply(o)
        got.append(o)  # the host's answers and one-way frames: no reply
        return b""

    return make_hub(on_request=on_request), got


def wait_for(got: list, n: int) -> None:
    end = time.time() + 3
    while len(got) < n and time.time() < end:
        time.sleep(0.02)


def test_stream_open_is_handed_out_and_answered_by_its_seq(make_hub) -> None:
    hub, got = hub_that_pushes(make_hub, ("stream_open", opening()))
    with open_link(hub) as link:
        link.call("snapshot_get", {})
        assert link.next_route_in(0) is None and link.next_event_deliver(0) is None  # not theirs
        assert link.next_stream(0) == ("stream_open", 1, opening())
        link.answer_stream_open(1, "remote.off")
        wait_for(got, 1)
        with pytest.raises(LinkError):  # answered once
            link.answer_stream_open(1)
    o = got[0]
    assert (o["op"], o["re"], o["dir"], o["body"]) == ("stream_open", 1, "w2h", {"error": "remote.off"})
    # an enroll stream names a code id
    hub2, got2 = hub_that_pushes(make_hub, ("stream_open", opening("enroll", CODE)))
    with open_link(hub2) as link:
        link.call("snapshot_get", {})
        assert link.next_stream(0)[2]["kind"] == "enroll"
        link.answer_stream_open(1)
        wait_for(got2, 1)
    assert got2[0]["body"] == {}


def test_one_way_frames_in_order_without_a_reply(make_hub) -> None:
    data = {"stream_id": SID, "seq": 1, "bytes": b64u(b"x" * 96)}
    close = {"stream_id": SID, "reason": "revoked"}
    hub, _ = hub_that_pushes(make_hub, ("stream_open", opening()), ("stream_data", data), ("stream_close", close))
    with open_link(hub) as link:
        link.call("snapshot_get", {})
        assert [link.next_stream(0)[0] for _ in range(3)] == ["stream_open", "stream_data", "stream_close"]
        assert link.next_stream(0) is None
        with pytest.raises(LinkError):  # an unanswered open of a frame that is not an open
            link.answer_stream_open(2)


@pytest.mark.parametrize(
    ("op", "body"),
    [
        ("stream_open", {**opening(), "x": 1}),
        ("stream_open", opening("enroll", DEV)),  # a device id on an enroll
        ("stream_open", opening("session", CODE)),  # a code id on a session
        ("stream_open", {**opening(), "stream_id": "AB" * 16}),
        ("stream_data", {"stream_id": SID, "seq": 0, "bytes": b64u(b"x")}),
        ("stream_data", {"stream_id": SID, "seq": 1, "bytes": b64u(b"")}),
        ("stream_data", {"stream_id": SID, "seq": 1, "bytes": b64u(bytes(65536))}),
        ("stream_data", {"stream_id": SID, "seq": True, "bytes": b64u(b"x")}),
        ("stream_data", {"stream_id": SID, "seq": 1, "bytes": "AB=="}),  # not canonical
        ("stream_close", {"stream_id": SID, "reason": "because"}),
        ("stream_close", {"stream_id": SID}),
    ],
)
def test_bad_stream_bodies_close_the_link(make_hub, op: str, body: dict) -> None:
    hub, _ = hub_that_pushes(make_hub, (op, body))
    with open_link(hub) as link:
        with pytest.raises(LinkError) as e:
            link.call("snapshot_get", {})
        assert e.value.code == "malformed"


def test_a_stream_frame_must_be_signed_by_the_hub_and_in_sequence(make_hub) -> None:
    def on_request(hub: TlsHub, o: dict):
        o2 = {
            "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": hub.hub_id, "workspace_id": hub.workspace_id,
            "dir": "h2w", "seq": hub.next_seq(), "ts_ms": int(time.time() * 1000), "op": "stream_close", "re": None,
            "body": {"stream_id": SID, "reason": "closed"},
        }  # fmt: skip
        # another key, over the right exporter
        framing.write_frame(hub._conns[-1], hub.sign_frame(o2, key=suite.generate_private_key()), timeout=5)  # noqa: SLF001
        return hub.reply(o)

    hub = make_hub(on_request=on_request)
    with open_link(hub) as link, pytest.raises(LinkError) as e:
        link.call("snapshot_get", {})
    assert e.value.code == "bad_signature"
    hub, _ = hub_that_pushes(
        make_hub,
        ("stream_close", {"stream_id": SID, "reason": "closed"}),
        ("stream_close", {"stream_id": SID, "reason": "closed"}, 1),  # the seq of the first frame again
    )
    with open_link(hub) as link, pytest.raises(LinkError) as e:  # the hub's seq never goes back
        link.call("snapshot_get", {})
    assert e.value.code == "malformed"


def test_a_stream_push_signed_without_the_exporter_is_refused(make_hub) -> None:
    def on_request(hub: TlsHub, o: dict):
        o2 = {
            "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": hub.hub_id, "workspace_id": hub.workspace_id,
            "dir": "h2w", "seq": hub.next_seq(), "ts_ms": int(time.time() * 1000), "op": "stream_close", "re": None,
            "body": {"stream_id": SID, "reason": "closed"},
        }  # fmt: skip
        sig = suite.sign(hub.hk_op, C.SIG_ROUTE + cj(o2))  # the pre-R1 form: label || cj(o), no exporter
        framing.write_frame(hub._conns[-1], {"o": o2, "sig": b64u(sig)}, timeout=5)  # noqa: SLF001
        return hub.reply(o)

    hub = make_hub(on_request=on_request)
    with open_link(hub) as link, pytest.raises(LinkError) as e:
        link.call("snapshot_get", {})
    assert e.value.code == "bad_signature"


def test_w2h_stream_open_unknown_op(make_hub) -> None:
    hub = make_hub()
    with open_link(hub) as link:
        with pytest.raises(LinkError) as e:
            send(link, "stream_open", opening())
        assert e.value.code == "unknown_op"
        with pytest.raises(LinkError) as e2:
            link.send_stream("stream_open", opening())
        assert e2.value.code == "unknown_op"
        with pytest.raises(LinkError) as e3:
            link.send_stream("route_in", {})
        assert e3.value.code == "unknown_op"
        assert link.is_open  # a refusal before sending keeps the link


def test_send_stream_is_one_way_with_the_links_next_seq(make_hub) -> None:
    got: list[dict] = []
    hub = make_hub(on_request=lambda h, o: got.append(o) or b"")
    with open_link(hub) as link:
        link.send_stream("stream_data", {"stream_id": SID, "seq": 1, "bytes": b64u(b"hi")})
        link.send_stream("stream_close", {"stream_id": SID, "reason": "remote.off"})
        wait_for(got, 2)
        with pytest.raises(LinkError):
            link.send_stream("stream_close", {"stream_id": SID, "reason": "nope"})
    assert [(o["op"], o["re"], o["dir"]) for o in got] == [("stream_data", None, "w2h"), ("stream_close", None, "w2h")]
    assert got[1]["seq"] == got[0]["seq"] + 1
    assert got[1]["body"] == {"stream_id": SID, "reason": "remote.off"}

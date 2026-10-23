import json
import os
import socket
import struct
import threading

import pytest

from orch_dashboard.errors import HostError
from orch_dashboard.hostlink import MAX_FRAME, HostLink, encode, receive

WS = "a" * 32


def welcome(**changes):
    return {
        "t": "welcome",
        "proto": 1,
        "host_uid": os.getuid(),
        "workspace": {"id": WS},
        "peer": {"uid": os.getuid(), "class": "person", "person": "p_test"},
        "caps": ["ops/1", "human/1"],
        "ops": {"whoami": 1, "human.prepare": 1},
        **changes,
    }


def test_partial_and_coalesced():
    a, b = socket.socketpair()
    wire = encode({"x": "é"}) + encode({"y": 2})

    def sender():
        with b:
            for byte in wire:
                b.sendall(bytes([byte]))

    t = threading.Thread(target=sender)
    t.start()
    with a:
        assert receive(a) == {"x": "é"}
        assert receive(a) == {"y": 2}
    t.join()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b'{"x":1,"x":2}',
        b'{"n":NaN}',
        b'{"n":1.0}',
        b'{"n":1e2}',
        b'{"n":9007199254740992}',
        b'{"n":Infinity}',
        b'{"n":9007199254740993}',
        b'{"x":"\xff"}',
        json.dumps({"a": [[[[[0]]]]]}).encode().replace(b"0", b"[" * 33 + b"0" + b"]" * 33),
    ],
)
def test_strict_json(raw):
    a, b = socket.socketpair()
    with a, b:
        b.sendall(struct.pack("!I", len(raw)) + raw)
        with pytest.raises(HostError) as err:
            receive(a)
        assert err.value.error["code"] == "frame.invalid"


def test_oversize_before_payload():
    a, b = socket.socketpair()
    with a, b:
        b.sendall(struct.pack("!I", MAX_FRAME + 1))
        with pytest.raises(HostError, match="frame.too_large"):
            receive(a)
    with pytest.raises(HostError, match="frame.too_large"):
        encode({"s": "a" * MAX_FRAME})


@pytest.mark.parametrize(
    "change",
    [
        dict(proto=2),
        dict(host_uid=-1),
        dict(workspace={"id": "b" * 32}),
        dict(peer={"uid": os.getuid(), "class": "agent", "person": None}),
        dict(peer={"uid": os.getuid(), "class": "person", "person": None}),
    ],
)
def test_handshake_pin_and_person(change):
    a, b = socket.socketpair()

    def host():
        with b:
            receive(b)
            b.sendall(encode(welcome(**change)))

    t = threading.Thread(target=host)
    t.start()
    with pytest.raises(HostError):
        HostLink(a, os.getuid(), WS)
    t.join()
    assert a.fileno() == -1


def test_errors_events_idem_and_result_unwrap():
    a, b = socket.socketpair()
    error = {
        "code": "role.denied",
        "message": "unchanged",
        "hint": "ask owner",
        "retryable": False,
        "retry_after_ms": 40,
        "fix": {"argv": ["orch", "status"]},
    }
    requests = []

    def host():
        with b:
            receive(b)
            b.sendall(encode(welcome()))
            req = receive(b)
            requests.append(req)
            b.sendall(encode({"t": "evt", "sub": "s", "items": []}))
            b.sendall(encode({"t": "res", "id": req["id"], "ok": False, "exit": 3, "error": error}))
            req = receive(b)
            b.sendall(
                encode(
                    {
                        "t": "res",
                        "id": req["id"],
                        "ok": True,
                        "result": {"data": {"person": "p_test"}, "lines": ["never parse this"]},
                    }
                )
            )

    t = threading.Thread(target=host)
    t.start()
    link = HostLink(a, os.getuid(), WS)
    with pytest.raises(ValueError):
        link.call("human.prepare", write=True)
    with pytest.raises(HostError) as err:
        link.call("human.prepare", {"op": "log"}, write=True, idem="logical-click")
    assert err.value.error == error and err.value.exit == 3
    assert requests[0]["opts"]["idem"] == "logical-click"
    assert len(link.events) == 1
    assert link.call("whoami") == {"person": "p_test"}
    link.close()
    t.join()


def test_frame_value_and_depth_boundaries():
    encode({"items": [0] * 65534})
    with pytest.raises(HostError, match="frame.invalid"):
        encode({"items": [0] * 65535})
    value = 0
    for _ in range(31):
        value = [value]
    encode({"nested": value})
    with pytest.raises(HostError, match="frame.invalid"):
        encode({"nested": [value]})


def test_human_v13_shapes_and_dry_run_opts():
    a, b = socket.socketpair()
    requests = []

    def host():
        with b:
            receive(b)
            b.sendall(encode(welcome(ops={"human.prepare": 1, "human.submit": 1, "human.pending": 1})))
            for _ in range(3):
                req = receive(b)
                requests.append(req)
                b.sendall(encode({"t": "res", "id": req["id"], "ok": True, "result": {"data": {}}}))

    t = threading.Thread(target=host)
    t.start()
    link = HostLink(a, os.getuid(), WS)
    try:
        link.call("human.prepare", {"op": "log", "args": {}, "handoff": True}, dry_run=True, idem="ignored")
        link.call("human.pending", {"request_id": "hr_demo"})
        with pytest.raises(ValueError, match="events"):
            link.call("human.submit", {"event": {}}, idem="test")
        link.call("human.submit", {"request_id": "hr_demo", "events": [{}]}, idem="test")
        assert requests[0]["opts"] == {"dry_run": True}
        assert "dry_run" not in requests[0]["args"]
        assert requests[1]["args"] == {"request_id": "hr_demo"}
        assert requests[2]["args"]["events"] == [{}]
    finally:
        link.close()
        t.join(2)


def test_hub_reads_are_plain_reads_decided_by_the_listed_op():
    """V3 A7: the host lists hub.status in `ops` but not `hub/1` in `caps`; reads need no idem."""
    a, b = socket.socketpair()
    requests = []

    def host():
        with b:
            receive(b)
            b.sendall(encode(welcome(ops={"hub.status": 1, "share.rules": 1})))
            req = receive(b)
            requests.append(req)
            b.sendall(encode({"t": "res", "id": req["id"], "ok": True, "result": {"data": {"signing": "full"}}}))

    t = threading.Thread(target=host)
    t.start()
    link = HostLink(a, os.getuid(), WS)
    try:
        assert link.supports("hub.status", "hub/1") and link.supports("share.rules", "hub/1")
        assert not link.supports("share.preview", "hub/1")  # not listed by the host
        assert link.call("hub.status") == {"signing": "full"}
        assert "opts" not in requests[0]  # a read: no idem
    finally:
        link.close()
        t.join(2)

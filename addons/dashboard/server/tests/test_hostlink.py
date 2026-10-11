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
        "caps": ["ops/1"],
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

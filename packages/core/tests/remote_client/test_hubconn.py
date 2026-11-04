"""R5: the hub connection of the remote client against the contract (§10.2d): order, exact keys, hub id, codes."""

from __future__ import annotations

import json

import pytest

from orch.remoteclient.codes import ClientError
from orch.remoteclient.hubconn import HubConn
from tests.remote_client.fakes import FakeWire
from tests.remote_client.vectors import DOC

HUB = "5204b6091c616bc615ddba3107e6255c"
WS = "882d742fa53e3cfdf21947a2c63286f2"
RD = "rd_6be572e1241672d2a48515fb6eca992b"
SID = "2edc92539206351c997ef280c35402ee"


def conn(*frames):
    w = FakeWire(list(frames))
    return w, HubConn(w, token="tok", hub_id=HUB)


def test_hello_ready_open_opened():
    w, c = conn({"t": "ready", "hub_id": HUB}, {"t": "opened", "stream_id": SID})
    assert c.hello() == HUB
    assert c.open(WS, RD, "session") == SID
    assert w.sent == [
        {"t": "hello", "v": 1, "token": "tok"},
        {"t": "open", "workspace_id": WS, "device_id": RD, "kind": "session"},
    ]


def test_ready_with_another_hub_id_aborts_before_anything_else_is_sent():
    w, c = conn({"t": "ready", "hub_id": "ab" * 16}, {"t": "opened", "stream_id": SID})
    with pytest.raises(ClientError) as e:
        c.hello()
    assert e.value.code == "remote_client.hub_mismatch"
    assert w.closed and w.sent == [{"t": "hello", "v": 1, "token": "tok"}]
    with pytest.raises(ClientError):
        c.open(WS, RD, "session")
    assert len(w.sent) == 1


@pytest.mark.parametrize(
    "frame",
    [
        {"t": "ready"},
        {"t": "ready", "hub_id": HUB, "x": 1},
        {"t": "ready", "hub_id": HUB.upper()},
        {"t": "opened", "stream_id": SID},
        {"t": "data", "b": "AA"},
        {"t": "rail", "workspaces": [], "more": False},
    ],
)
def test_bad_frame_before_ready_closes_the_connection(frame):
    w, c = conn(frame)
    with pytest.raises(ClientError) as e:
        c.hello()
    assert e.value.code == "remote_client.protocol" and w.closed


def test_hub_error_codes_surface():
    for code in ("no_session", "remote.busy", "unavailable", "malformed"):
        _, c = conn({"t": "error", "error": code})
        with pytest.raises(ClientError) as e:
            c.hello()
        assert e.value.code == code


def test_rail_once_and_rows_checked():
    rows = [{"workspace_id": WS, "name": "Acme", "online": True}]
    w, c = conn({"t": "ready", "hub_id": HUB}, {"t": "rail", "workspaces": rows, "more": False})
    c.hello()
    assert c.rail() == rows
    with pytest.raises(ClientError):
        c.rail()


@pytest.mark.parametrize(
    "rows",
    [
        [{"workspace_id": WS, "name": "A", "online": 1}],
        [{"workspace_id": WS, "name": "A", "online": True, "role": "x"}],
        [
            {"workspace_id": "b" * 32, "name": None, "online": True},
            {"workspace_id": "a" * 32, "name": None, "online": True},
        ],
        [{"workspace_id": WS, "name": " pad", "online": True}],
    ],
)
def test_rail_rows_out_of_contract_are_refused(rows):
    _, c = conn({"t": "ready", "hub_id": HUB}, {"t": "rail", "workspaces": rows, "more": False})
    c.hello()
    with pytest.raises(ClientError) as e:
        c.rail()
    assert e.value.code == "remote_client.protocol"


def test_refused_is_the_hubs_code():
    _, c = conn({"t": "ready", "hub_id": HUB}, {"t": "refused", "error": "remote.off"})
    c.hello()
    with pytest.raises(ClientError) as e:
        c.open(WS, RD, "session")
    assert e.value.code == "remote.off"


def test_open_pairing_is_checked_before_sending():
    _, c = conn({"t": "ready", "hub_id": HUB})
    c.hello()
    with pytest.raises(ClientError):
        c.open(WS, RD, "enroll")


def _stream(*after):
    w, c = conn({"t": "ready", "hub_id": HUB}, {"t": "opened", "stream_id": SID}, *after)
    c.hello()
    c.open(WS, RD, "session")
    return w, c


def test_data_and_close():
    w, c = _stream({"t": "data", "b": "AAEC"}, {"t": "close", "reason": "revoked"})
    assert c.recv(1) == ("data", b"\x00\x01\x02")
    assert c.recv(1) == ("close", "revoked")


@pytest.mark.parametrize(
    ("frame", "reason"),
    [
        ({"t": "opened", "stream_id": SID}, "malformed"),
        ({"t": "refused", "error": "unavailable"}, "malformed"),
        ({"t": "error", "error": "unavailable"}, "malformed"),
        ({"t": "rail", "workspaces": [], "more": False}, "malformed"),
        ({"t": "data", "b": ""}, "malformed"),
        ({"t": "data", "b": "AA=="}, "malformed"),
        ({"t": "data", "b": "A" * 90000}, "too_large"),
        ({"t": "data"}, "malformed"),
        ({"t": "close", "reason": "nope"}, "malformed"),
    ],
)
def test_bad_hub_frame_after_opened_closes_the_stream(frame, reason):
    w, c = _stream(frame)
    with pytest.raises(ClientError):
        c.recv(1)
    assert w.sent[-1] == {"t": "close", "reason": reason} and w.closed


def test_data_after_our_close_is_dropped_not_an_error():
    w, c = _stream({"t": "data", "b": "AAEC"}, {"t": "close", "reason": "closed"})
    c.send_close("closed")
    assert c.recv(1) == ("close", "closed")


def test_connection_end_is_close_closed():
    _, c = _stream("EOF")
    assert c.recv(1) == ("close", "closed")


def test_every_client_vector_h2c_frame_is_judged_like_the_contract():
    from orch.remoteclient import hubconn
    from tests.remote_client.vectors import cases

    n = 0
    for case in cases("client_frame"):
        a = case["args"]
        if a["dir"] != "h2c" or "frame" not in a or "b_repeat" in a:
            continue
        n += 1
        try:
            hubconn._shape(dict(a["frame"]))  # noqa: SLF001
            if a["frame"].get("t") == "data":
                hubconn._data_bytes(a["frame"]["b"])  # noqa: SLF001
            ok = True
        except hubconn._Bad:  # noqa: SLF001
            ok = False
        assert ok == (case["expect"] == {"ok": True}), (case["name"], json.dumps(case["expect"]))
    assert n > 15
    assert DOC["constants"]["client"]["alpn"] == "orch-remote/1"

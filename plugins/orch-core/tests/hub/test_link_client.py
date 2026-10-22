"""C5: the TLS link client against a test hub that plays protocol section 10 (``tlshub.TlsHub``)."""

from __future__ import annotations

import struct
import time
from collections.abc import Iterator
from typing import Any

import pytest
from OpenSSL import SSL

from orch.canon.jcs import dumps as cj
from orch.crypto import suite
from orch.crypto.encoding import b64u
from orch.hub.tlslink import client as C
from orch.hub.tlslink.client import LinkClient, LinkError
from orch.hub.tlslink.tls import LinkClosed, LinkTimeout, PinMismatch

from .tlshub import TlsHub

WSK = suite.generate_private_key()
WSK_PUB = suite.public_bytes(WSK)


def sign(msg: bytes) -> bytes:
    return suite.sign(WSK, msg)


@pytest.fixture
def make_hub() -> Iterator:
    hubs: list[TlsHub] = []

    def make(**kw: Any) -> TlsHub:
        h = TlsHub(wsk_pub=WSK_PUB, **kw).start()
        hubs.append(h)
        return h

    yield make
    for h in hubs:
        h.close()


def open_link(hub: TlsHub, **kw: Any) -> LinkClient:
    args: dict[str, Any] = {
        "tls_spki_sha256": hub.spki,
        "hub_id": hub.hub_id,
        "workspace_id": hub.workspace_id,
        "sign": sign,
        "hk_op_pub": hub.hk_op_pub,
        "timeout": 3.0,
    }
    args.update(kw)
    return LinkClient.open("127.0.0.1", hub.port, **args)


_seq = iter(range(1, 10_000))


def request(link: LinkClient, op: str = "snapshot_get", body: dict | None = None, **fields: Any) -> dict[str, Any]:
    o = {
        "v": 2,
        "suite": 2,
        "kind": "hub.link",
        "hv": 0,
        "hub_id": link.hub_id,
        "workspace_id": link.workspace_id,
        "dir": "w2h",
        "seq": next(_seq),
        "ts_ms": int(time.time() * 1000),
        "op": op,
        "re": None,
        "body": body or {},
    }
    o.update(fields)
    return {"o": o, "sig": b64u(sign(C.SIG_LINK + cj(o)))}


def test_happy_path_auth_and_one_request(make_hub) -> None:
    hub = make_hub()
    with open_link(hub) as link:
        assert hub.auth_verified.wait(3)
        assert "wsk_pub" not in hub.auth_frames[0]
        reply = link.send(request(link))
        assert reply["o"]["body"] == {"snapshot": {"stub": 1}}
        assert link.request("card_put", {"card": {}, "pk_pub": "x"}, sign=sign, seq=99) == {}
    assert hub.requests[0]["o"]["op"] == "snapshot_get"


def test_first_link_sends_wsk_pub_only_when_asked(make_hub) -> None:
    hub = make_hub()
    with open_link(hub, wsk_pub=WSK_PUB):
        assert hub.auth_frames[0]["wsk_pub"] == b64u(WSK_PUB)


def test_wrong_spki_is_refused_before_any_app_byte(make_hub) -> None:
    hub = make_hub()
    other = TlsHub(wsk_pub=WSK_PUB)
    with pytest.raises(PinMismatch):
        open_link(hub, tls_spki_sha256=other.spki)
    other.close()
    assert hub.conn_done.wait(3)
    assert bytes(hub.app_bytes) == b""


def test_malformed_pin_refused_without_dialing(make_hub) -> None:
    hub = make_hub()
    with pytest.raises(PinMismatch):
        open_link(hub, tls_spki_sha256="AAAA")
    assert not hub.conn_done.is_set() and bytes(hub.app_bytes) == b""


def test_tls12_hub_is_refused(make_hub) -> None:
    hub = make_hub(max_tls=SSL.TLS1_2_VERSION)
    with pytest.raises((LinkClosed, LinkTimeout)):
        open_link(hub)
    assert bytes(hub.app_bytes) == b""


def test_exporter_mismatch_gives_one_preauth_code_and_closes(make_hub) -> None:
    hub = make_hub()
    hub.exporter_override = b"\x01" * 32
    with pytest.raises(LinkError) as e:
        open_link(hub)
    assert e.value.code == "auth_failed"
    assert hub.conn_done.wait(3)


def test_challenge_for_another_hub_sends_nothing(make_hub) -> None:
    hub = make_hub()
    hub.challenge_hub_id = "00" * 16
    with pytest.raises(LinkError) as e:
        open_link(hub)
    assert e.value.code == "other_hub"
    assert hub.conn_done.wait(3)
    assert bytes(hub.app_bytes) == b""


def test_wrong_wsk_key_is_auth_failed(make_hub) -> None:
    hub = make_hub()
    other = suite.generate_private_key()
    with pytest.raises(LinkError) as e:
        open_link(hub, sign=lambda m: suite.sign(other, m))
    assert e.value.code == "auth_failed"


def bad_reply(kind: str):
    def on_request(hub: TlsHub, o: dict) -> dict:
        if kind == "sig":
            return hub.reply(o, key=suite.generate_private_key())
        if kind == "other_ws":
            return hub.reply(o, workspace_id="11" * 16)
        if kind == "other_hub":
            return hub.reply(o, hub_id="22" * 16)
        if kind == "stale":
            return hub.reply(o, ts_ms=int(time.time() * 1000) - 121_000)
        if kind == "re":
            return hub.reply(o, re=o["seq"] + 1)
        if kind == "dir":
            return hub.reply(o, dir="w2h")
        if kind == "op":
            return hub.reply(o, op="card_put", body={})
        if kind == "echo":  # an unknown op echoed back: only closed-list ops or the literal "error" are allowed
            return hub.reply(o, op="zzz", body={"error": "unknown_op"})
        if kind == "shape":
            return hub.reply(o, body={"snapshot": 1})
        if kind == "extra":
            return hub.reply(o, body={"snapshot": {}, "more": 1})
        if kind == "suite":
            return hub.reply(o, suite=1)
        if kind == "hv":
            return hub.reply(o, hv=1)
        raise AssertionError(kind)

    return on_request


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("sig", "bad_signature"),
        ("other_ws", "other_workspace"),
        ("other_hub", "other_hub"),
        ("stale", "stale_timestamp"),
        ("re", "malformed"),
        ("dir", "malformed"),
        ("op", "malformed"),
        ("echo", "malformed"),
        ("shape", "malformed"),
        ("extra", "malformed"),
        ("suite", "wrong_suite"),
        ("hv", "malformed"),
    ],
)
def test_bad_replies_are_refused_and_close_the_link(make_hub, kind: str, code: str) -> None:
    hub = make_hub(on_request=bad_reply(kind))
    link = open_link(hub)
    with pytest.raises(LinkError) as e:
        link.send(request(link))
    assert e.value.code == code
    with pytest.raises(LinkClosed):
        link.send(request(link))


def test_unknown_op_error_reply_keeps_the_link_open(make_hub) -> None:
    calls = []

    def on_request(hub: TlsHub, o: dict) -> dict:
        calls.append(o["op"])
        if len(calls) == 1:
            return hub.reply(o, op="error", body={"error": "unknown_op"})
        return hub.reply(o)

    hub = make_hub(on_request=on_request)
    with open_link(hub) as link:
        with pytest.raises(LinkError) as e:
            link.request("snapshot_get", {}, sign=sign, seq=1)
        assert e.value.code == "unknown_op"
        assert link.request("snapshot_get", {}, sign=sign, seq=2) == {"snapshot": {"stub": 1}}


def test_error_op_with_a_success_body_is_refused(make_hub) -> None:
    hub = make_hub(on_request=lambda h, o: h.reply(o, op="error", body={}))
    link = open_link(hub)
    with pytest.raises(LinkError) as e:
        link.send(request(link))
    assert e.value.code == "malformed"


def test_unsigned_error_frame_after_auth_raises_and_closes(make_hub) -> None:
    hub = make_hub()
    hub.on_request = lambda h, o: {"t": "error", "error": "stale_timestamp"}
    link = open_link(hub)
    with pytest.raises(LinkError) as e:
        link.send(request(link))
    assert e.value.code == "stale_timestamp"
    with pytest.raises(LinkClosed):
        link.send(request(link))


def test_oversize_frame_is_refused_from_the_header_alone(make_hub) -> None:
    hub = make_hub(on_request=lambda h, o: struct.pack(">I", 2 << 20))  # header only; the body never comes
    link = open_link(hub)
    started = time.monotonic()
    with pytest.raises(LinkError) as e:
        link.send(request(link))
    assert e.value.code == "malformed" and time.monotonic() - started < 2.5


def test_zero_length_and_non_object_frames_are_refused(make_hub) -> None:
    for raw in (struct.pack(">I", 0), struct.pack(">I", 2) + b"[]", struct.pack(">I", 3) + b"\xff\xfe\xfd"):
        hub = make_hub(on_request=lambda h, o, raw=raw: raw)
        link = open_link(hub)
        with pytest.raises(LinkError) as e:
            link.send(request(link))
        assert e.value.code == "malformed"


def test_read_timeout_closes_the_link(make_hub) -> None:
    hub = make_hub(on_request=lambda h, o: None)
    link = open_link(hub, timeout=0.4)
    with pytest.raises(LinkTimeout):
        link.send(request(link))
    with pytest.raises(LinkClosed):
        link.send(request(link))


def test_ops_outside_the_closed_list_are_never_sent(make_hub) -> None:
    hub = make_hub()
    with open_link(hub) as link:
        with pytest.raises(LinkError) as e:
            link.send(request(link, "sign_this"))
        assert e.value.code == "unknown_op"
        with pytest.raises(LinkError) as e:
            link.send(request(link, "event_deliver"))  # an h2w op
        assert e.value.code == "unknown_op"
        with pytest.raises(LinkError) as e:
            link.send(request(link, "wk_seal"))  # full mode only
        assert e.value.code == "wrong_mode"
        with pytest.raises(LinkError) as e:
            link.send(request(link, hub_id="33" * 16))
        assert e.value.code == "other_hub"
        with pytest.raises(LinkError) as e:
            link.send(request(link, workspace_id="33" * 16))
        assert e.value.code == "other_workspace"
        with pytest.raises(LinkError) as e:
            link.send(request(link, re=5))
        assert e.value.code == "malformed"
        assert hub.requests == []
        assert link.send(request(link))["o"]["op"] == "snapshot_get"  # and the link still works


def test_selective_ops_are_refused_in_full_mode(make_hub) -> None:
    hub = make_hub()
    with open_link(hub, mode="full") as link:
        with pytest.raises(LinkError) as e:
            link.send(request(link, "share_put", {"item_id": "x", "sealed": "y"}))
        assert e.value.code == "wrong_mode"


def test_unhashable_op_is_malformed_not_a_crash(make_hub) -> None:
    hub = make_hub()
    with open_link(hub) as link:
        with pytest.raises(LinkError) as e:
            link.send(request(link, op=["snapshot_get"]))
        assert e.value.code == "malformed"
        assert hub.requests == []


def test_overall_deadline_bounds_a_hub_that_never_answers(make_hub) -> None:
    hub = make_hub()
    hub.challenge_delay = 3.0
    started = time.monotonic()
    with pytest.raises(LinkTimeout):
        open_link(hub, timeout=0.5)
    assert time.monotonic() - started < 1.5

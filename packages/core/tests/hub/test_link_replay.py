"""R1 (#1589): every link signature binds the connection's TLS exporter, and the h2w ``seq`` is strictly consecutive per
connection (replies and pushes share one counter, the first frame is 1). The host closes the link on a violation."""

from __future__ import annotations

import time
from typing import Any

import pytest

from orch.canon.jcs import dumps as cj
from orch.crypto.encoding import b64u
from orch.hubclient.link import HubLink
from orch.hubclient.netconfig import NetSettings
from orch.hubclient.tlsadapter import TlsTransport
from orch.hubclient.tlslink.client import LinkError

from .test_link_client import WSK_PUB, make_hub, open_link, sign  # noqa: F401
from .tlshub import TlsHub


def _push(hub: TlsHub, seq: int) -> None:
    from orch.hubclient.tlslink import framing

    o = {
        "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": hub.hub_id, "workspace_id": hub.workspace_id,
        "dir": "h2w", "seq": seq, "ts_ms": int(time.time() * 1000), "op": "route_in", "re": None,
        "body": {"envelope": b64u(b"env")},
    }  # fmt: skip
    framing.write_frame(hub._conns[-1], hub.sign_frame(o), timeout=5)  # noqa: SLF001


def test_h2w_reply_replayed_on_a_new_connection_is_refused(make_hub) -> None:  # noqa: F811
    recorded: list[dict[str, Any]] = []

    def on_request(hub: TlsHub, o: dict) -> dict:
        if not recorded:
            recorded.append(hub.reply(o))
            return recorded[0]
        return recorded[0]  # the frame recorded on connection A, served for the same position on B

    hub = make_hub(on_request=on_request)
    with open_link(hub) as a:
        assert a.call("snapshot_get", {}) == {"snapshot": {"stub": 1}}
    with open_link(hub) as b, pytest.raises(LinkError) as e:
        b.call("snapshot_get", {})
    assert e.value.code == "bad_signature"
    assert not b.is_open


def test_h2w_reply_repeated_on_the_same_connection_is_refused(make_hub) -> None:  # noqa: F811
    def on_request(hub: TlsHub, o: dict) -> dict:
        return hub.reply(o, seq=1)  # right ``re``, but the seq of the first frame again

    hub = make_hub(on_request=on_request)
    with open_link(hub) as link:
        link.call("snapshot_get", {})
        with pytest.raises(LinkError) as e:
            link.call("snapshot_get", {})
        assert e.value.code == "malformed"
        assert not link.is_open


def test_h2w_push_and_reply_share_one_strict_counter(make_hub) -> None:  # noqa: F811
    def good(hub: TlsHub, o: dict) -> dict:
        _push(hub, hub.next_seq())  # 1
        return hub.reply(o)  # 2

    with open_link(make_hub(on_request=good)) as link:
        assert link.call("snapshot_get", {}) == {"snapshot": {"stub": 1}}

    def skip(hub: TlsHub, o: dict) -> dict:
        _push(hub, hub.next_seq())  # 1
        return hub.reply(o, seq=3)  # skips 2

    with open_link(make_hub(on_request=skip)) as link, pytest.raises(LinkError) as e:
        link.call("snapshot_get", {})
    assert e.value.code == "malformed"

    with open_link(make_hub(on_request=lambda hub, o: hub.reply(o, seq=2))) as link, pytest.raises(LinkError) as e:
        link.call("snapshot_get", {})  # the first h2w frame is 2
    assert e.value.code == "malformed"


def test_replayed_error_reply_cannot_fail_a_later_request(make_hub) -> None:  # noqa: F811
    recorded: list[dict[str, Any]] = []

    def on_request(hub: TlsHub, o: dict) -> dict:
        if not recorded:  # connection A: the hub refuses request 1 with an error reply, which is recorded
            recorded.append(hub.reply(o, op="error", body={"error": "forbidden_role"}))
            return recorded[0]
        if o["seq"] == 1:
            return hub.reply(o)  # connection B: request 1 is answered honestly
        return recorded[0]  # request 2: the recorded error frame (re 1, seq 1) is replayed

    hub = make_hub(on_request=on_request)
    with open_link(hub) as a, pytest.raises(LinkError) as e1:
        a.call("snapshot_get", {})
    assert e1.value.code == "forbidden_role"
    with open_link(hub) as b:
        b.call("snapshot_get", {})
        with pytest.raises(LinkError) as e2:
            b.call("snapshot_get", {})
        assert e2.value.code == "bad_signature"  # not "forbidden_role": it never reaches the request


def test_signed_without_or_over_another_exporter_is_refused(make_hub) -> None:  # noqa: F811
    from orch.crypto import suite
    from orch.hubclient.tlslink import client as C

    def old(hub: TlsHub, o: dict) -> dict:  # the pre-R1 signature: label || cj(o)
        r = hub.reply(o)
        return {"o": r["o"], "sig": b64u(suite.sign(hub.hk_op, C.SIG_ROUTE + cj(r["o"])))}

    for handler in (old, lambda hub, o: hub.reply(o, exporter=b"\x09" * 32)):
        with open_link(make_hub(on_request=handler)) as link, pytest.raises(LinkError) as e:
            link.call("snapshot_get", {})
        assert e.value.code == "bad_signature"


def test_reconnect_after_the_link_dropped_starts_at_1_and_works(make_hub) -> None:  # noqa: F811
    seen: list[int] = []

    def on_request(hub: TlsHub, o: dict) -> dict:
        seen.append(o["seq"])
        return hub.reply(o)

    hub = make_hub(on_request=on_request)
    t = TlsTransport(
        addr=f"127.0.0.1:{hub.port}", tls_spki_sha256=hub.spki, hub_id=hub.hub_id, workspace_id=hub.workspace_id,
        hk_op_pub=hub.hk_op_pub, wsk_pub=WSK_PUB, sign=sign, mode="selective", timeout=3.0, net=NetSettings(),
    )  # fmt: skip
    link = HubLink(
        t, hub_id=hub.hub_id, workspace_id=hub.workspace_id, hk_op_pub=hub.hk_op_pub, sign=sign, mode="selective",
        now_ms=lambda: int(time.time() * 1000),
    )  # fmt: skip
    try:
        link.call("snapshot_get", {})
        link.call("snapshot_get", {})
        t._drop()  # noqa: SLF001  # the connection ends (hub restart, network): the next call dials again
        assert link.call("snapshot_get", {}) == {"snapshot": {"stub": 1}}
    finally:
        t.close()
    assert seen == [1, 2, 1]


def test_failed_ack_signing_closes_the_link_and_takes_no_seq(make_hub) -> None:  # noqa: F811
    hub = make_hub(on_request=lambda h, o: h.reply(o))
    fail = {"on": False}

    def flaky(msg: bytes) -> bytes:
        if fail["on"]:
            fail["on"] = False
            raise RuntimeError("signer down")
        return sign(msg)

    with open_link(hub, sign=flaky) as link:
        _push(hub, hub.next_seq())
        assert link.next_route_in(wait=3.0) == b"env"
        fail["on"] = True
        with pytest.raises(RuntimeError):
            link.ack_route_in()
        assert not link.is_open  # fail closed: no frame was written, no number skipped on an open link
        assert link._seq == 0  # noqa: SLF001


def test_a_push_from_the_test_thread_never_overlaps_the_hub_threads_ssl_calls(make_hub) -> None:  # noqa: F811
    """Root cause of the ack-signing flake: the test thread's push and the hub thread's read hit one pyOpenSSL
    connection at the same time (not thread-safe), now and then killing the link with ``LinkClosed: Error``. The
    overlap is a timing accident, so this widens it: every SSL call of the hub connection sleeps briefly, and the
    test counts calls that ran concurrently. The hub's connection must serialise them."""
    import threading

    hub = make_hub(on_request=lambda h, o: b"" if o["op"] == "route_in" else h.reply(o))  # an ack gets no reply
    state = {"in": 0, "overlap": 0}
    guard = threading.Lock()

    class Slow:
        def __init__(self, c: Any) -> None:
            self._c = c

        def __getattr__(self, name: str) -> Any:
            fn = getattr(self._c, name)
            if name not in {"recv", "send"}:
                return fn

            def call(*a: Any, **k: Any) -> Any:
                with guard:
                    state["in"] += 1
                    state["overlap"] += state["in"] > 1
                try:
                    time.sleep(0.002)
                    return fn(*a, **k)
                finally:
                    with guard:
                        state["in"] -= 1

            return call

    with open_link(hub) as link:
        hub._conns[-1]._c = Slow(hub._conns[-1]._c)  # noqa: SLF001
        for _ in range(20):
            _push(hub, hub.next_seq())
            assert link.next_route_in(wait=3.0) == b"env"
            link.ack_route_in()
    assert state["overlap"] == 0

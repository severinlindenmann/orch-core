"""L2-D12b-4: one w2h seq allocator per link. ``HubLink`` calls and the acks of pushed ``route_in`` /
``event_deliver`` requests share the link's counter, so a strict hub (first frame sets the baseline, then exactly +1)
never sees a repeat."""

from __future__ import annotations

import threading
import time
from typing import Any

from orch.canon.jcs import dumps as cj
from orch.crypto import suite
from orch.crypto.encoding import b64u
from orch.hubclient import HubLink
from orch.hubclient.tlsadapter import TlsTransport
from orch.hubclient.tlslink import client as C
from orch.hubclient.tlslink import framing

from .tlshub import TlsHub

WSK = suite.generate_private_key()
WSK_PUB = suite.public_bytes(WSK)


def sign(msg: bytes) -> bytes:
    return suite.sign(WSK, msg)


def _now() -> int:
    return int(time.time() * 1000)


class StrictHub:
    """Replies to snapshot_get; pushes a route_in with each reply; fails the test on any seq that is not previous + 1
    on the same connection (the first frame of a connection sets the baseline)."""

    def __init__(self) -> None:
        self.seqs: list[int] = []
        self.bad: list[tuple[int, int]] = []
        self._conn_seen = -1
        self._last = 0
        self.hub: TlsHub | None = None
        self._push_n = 0

    def on_request(self, hub: TlsHub, o: dict) -> Any:
        conns = len(hub._conns)  # noqa: SLF001
        if conns != self._conn_seen:
            self._conn_seen, self._last = conns, o["seq"] - 1  # a new link: this frame is the baseline
        if o["seq"] != self._last + 1:
            self.bad.append((self._last, o["seq"]))
        self._last = o["seq"]
        self.seqs.append(o["seq"])
        if o["op"] == "route_in":  # the host's ack: no reply
            return b""
        self._push_n += 1
        h2w = {
            "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": hub.hub_id, "workspace_id": hub.workspace_id,
            "dir": "h2w", "seq": self._push_n, "ts_ms": _now(), "op": "route_in", "re": None,
            "body": {"envelope": b64u(b"env")},
        }  # fmt: skip
        sig = suite.sign(hub.hk_op, C.SIG_ROUTE + cj(h2w))
        framing.write_frame(hub._conns[-1], {"o": h2w, "sig": b64u(sig)}, timeout=5)  # noqa: SLF001
        return hub.reply(o)


def _setup() -> tuple[StrictHub, TlsHub, TlsTransport, HubLink]:
    s = StrictHub()
    hub = TlsHub(wsk_pub=WSK_PUB, on_request=s.on_request).start()
    s.hub = hub
    t = TlsTransport(
        addr=f"127.0.0.1:{hub.port}", tls_spki_sha256=hub.spki, hub_id=hub.hub_id, workspace_id=hub.workspace_id,
        hk_op_pub=hub.hk_op_pub, wsk_pub=WSK_PUB, sign=sign, mode="selective", timeout=3.0, now_ms=_now,
    )  # fmt: skip
    link = HubLink(
        t, hub_id=hub.hub_id, workspace_id=hub.workspace_id, hk_op_pub=hub.hk_op_pub, sign=sign, mode="selective",
        now_ms=_now,
    )  # fmt: skip
    return s, hub, t, link


def _ack_one(t: TlsTransport) -> None:
    c = t._client  # noqa: SLF001
    assert c is not None
    assert c.next_route_in(1.0) == b"env"
    c.ack_route_in()


def _wait(pred, n: float = 3.0) -> None:
    end = time.time() + n
    while not pred() and time.time() < end:
        time.sleep(0.02)
    assert pred()


def test_link_seq_call_ack_call_is_strictly_increasing() -> None:
    s, hub, t, link = _setup()
    try:
        link.call("snapshot_get", {})
        _ack_one(t)
        link.call("snapshot_get", {})
        _ack_one(t)
        link.call("snapshot_get", {})
        _wait(lambda: len(s.seqs) == 5)
        assert s.seqs == [1, 2, 3, 4, 5]
        assert s.bad == []
    finally:
        t.close()
        hub.close()


def test_link_seq_restarts_on_a_new_link_after_reconnect() -> None:
    s, hub, t, link = _setup()
    try:
        link.call("snapshot_get", {})
        _ack_one(t)
        link.call("snapshot_get", {})
        t._drop()  # noqa: SLF001  the link failed; the next call opens a new one
        link.call("snapshot_get", {})
        _ack_one(t)
        link.call("snapshot_get", {})
        _wait(lambda: len(s.seqs) == 6)
        assert s.seqs == [1, 2, 3, 1, 2, 3]
        assert s.bad == []
    finally:
        t.close()
        hub.close()


def test_link_seq_concurrent_calls_never_repeat_or_invert() -> None:
    s, hub, t, link = _setup()
    errs: list[BaseException] = []

    def go() -> None:
        try:
            for _ in range(5):
                link.call("snapshot_get", {})
        except BaseException as e:  # noqa: BLE001
            errs.append(e)

    try:
        ths = [threading.Thread(target=go) for _ in range(3)]
        [x.start() for x in ths]
        [x.join(20) for x in ths]
        assert errs == []
        assert s.seqs == list(range(1, 16))
        assert s.bad == []
    finally:
        t.close()
        hub.close()

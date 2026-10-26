"""C12 cross-repo: two core hosts' ws->ws through orch-hub's real router (a subprocess under the hub's own environment,
``ORCH_HUB_SRC``; skipped without it). Core never imports hub code."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from orch.crypto import suite
from orch.crypto.encoding import unb64u
from orch.hubclient import wsenvelope as W
from orch.hubclient.errors import HubRefused
from orch.hubclient.peerservice import PeerService
from orch.hubclient.tlslink.client import LinkClient

HUB_SRC = os.environ.get("ORCH_HUB_SRC", "")
pytestmark = pytest.mark.skipif(
    not (HUB_SRC and (Path(HUB_SRC) / "src" / "orch_hub" / "router").is_dir() and shutil.which("uv")),
    reason="ORCH_HUB_SRC not set to an orch-hub checkout with the router",
)


@pytest.fixture
def hub() -> Iterator[dict]:
    p = subprocess.Popen(
        ["uv", "run", "--project", HUB_SRC, "python", str(Path(__file__).with_name("hub_route_proc.py")), HUB_SRC],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = p.stdout.readline()
        if not line:
            pytest.skip("hub server did not start")
        yield json.loads(line)
    finally:
        p.stdin.close()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


class Host:
    def __init__(self, hub: dict, who: str) -> None:
        d = hub[who]
        self.hub, self.ws = hub, d["workspace_id"]
        self.wsk = suite.private_key_from_scalar(d["wsk_scalar"])
        self.wxk = suite.generate_private_key()
        self.mb = W.Mailbox()
        self._sent_bytes: dict[str, bytes] = {}
        self.join = d["join"]
        self.svc = PeerService(
            me=self.ws, sign=self.sign, wxk_for=lambda v: (self.wxk, None) if v == 1 else None, wxk_current=1,
            mailbox=self.mb, save=lambda m: W.Mailbox.from_json(m.to_json()), connect=self.open,
        )  # fmt: skip

    def sign(self, msg: bytes) -> bytes:
        return suite.sign(self.wsk, msg)

    def open(self, first: bool = False) -> LinkClient:
        link = self._open(first)
        orig = link.route_out

        def record(env: bytes, **kw: int) -> str:
            self._sent_bytes[env[8:24].hex()] = env
            return orig(env, **kw)

        link.route_out = record  # type: ignore[method-assign]
        return link

    def _open(self, first: bool) -> LinkClient:
        return LinkClient.open(
            "127.0.0.1", self.hub["port"], tls_spki_sha256=self.hub["spki"], hub_id=self.hub["hub_id"],
            workspace_id=self.ws, sign=lambda m: suite.sign(self.wsk, m), hk_op_pub=unb64u(self.hub["hk_op_pub"], 65),
            wsk_pub=suite.public_bytes(self.wsk) if first else None, timeout=10,
        )  # fmt: skip

    def do_join(self) -> None:
        with self.open(first=True) as c:
            c.request("join", self.join, sign=lambda m: suite.sign(self.wsk, m), seq=1)

    def pin(self, other: Host) -> None:
        self.mb.pin_peer(other.ws, suite.public_bytes(other.wsk), suite.public_bytes(other.wxk), 1)


@pytest.fixture
def two(hub: dict) -> Iterator[tuple[Host, Host]]:
    a, b = Host(hub, "a"), Host(hub, "b")
    a.do_join()
    b.do_join()
    a.pin(b)
    b.pin(a)
    yield a, b
    a.svc.close()
    b.svc.close()


def take(h: Host, n: int = 1, secs: float = 10.0) -> list[W.Outcome]:
    out, end = [], time.monotonic() + secs
    while len(out) < n and time.monotonic() < end:
        o = h.svc.pump(0.2)
        if o is not None:
            out.append(o)
    return out


NOW = lambda: int(time.time() * 1000)  # noqa: E731


def test_real_hub_send_inbox_reply_and_waiting_on_peer(two: tuple[Host, Host]) -> None:
    a, b = two
    sent = a.svc.send_ticket(b.ws, {"title": "hello"}, deadline_ms=NOW() + 3_600_000)
    assert sent["state"] in ("queued", "forwarded")
    assert a.svc.sent_status(sent["envelope_id"])["status"] == "waiting_on_peer"
    assert [o.action for o in take(b)] == ["inbox"]
    (item,) = b.svc.inbox()
    assert item["envelope_sha256"] == sent["envelope_sha256"]
    b.svc.send_result(item["id"], {"summary": "ok"})
    assert [o.action for o in take(a)] == ["deliver"]
    st = a.svc.sent_status(sent["envelope_id"])
    assert st["status"] == "answered" and st["peer_result"]["summary"] == "ok"


def test_real_hub_recipient_offline_then_delivered_on_reconnect(two: tuple[Host, Host]) -> None:
    a, b = two
    sent = a.svc.send_ticket(b.ws, {"title": "later"}, deadline_ms=NOW() + 3_600_000)
    assert sent["state"] == "queued"  # B has no live link: the hub holds it
    assert [o.action for o in take(b)] == ["inbox"]  # B connects now
    assert b.svc.inbox()[0]["envelope_sha256"] == sent["envelope_sha256"]


def test_real_hub_unpinned_sender_refused_and_acked_with_error(two: tuple[Host, Host]) -> None:
    a, b = two
    a.svc.send_ticket(b.ws, {"title": "x"}, deadline_ms=NOW() + 3_600_000)
    b.mb.peers.clear()
    (o,) = take(b)
    assert (o.action, o.code) == ("drop", "not_pinned")
    assert take(b, 1, 1.0) == []  # acknowledged with an error: the hub does not redeliver


def _last_bytes(h: Host, eid: str) -> bytes:
    """The exact bytes the host put on the wire (recorded by the wrapped link)."""
    return h._sent_bytes[eid]


def test_real_hub_duplicate_resend_gives_one_inbox_item(two: tuple[Host, Host]) -> None:
    a, b = two
    sent = a.svc.send_ticket(b.ws, {"title": "x"}, deadline_ms=NOW() + 3_600_000)
    again = a.svc._ensure().route_out(_last_bytes(a, sent["envelope_id"]))  # a resend of the identical bytes
    assert again in ("queued", "forwarded")
    take(b, 2, 3.0)
    assert len(b.svc.inbox()) == 1


def test_real_hub_one_mib_envelope_round_trips(two: tuple[Host, Host]) -> None:
    a, b = two
    sent = a.svc.send_ticket(b.ws, {"pad": "x" * (W.MAX_ENVELOPE - 700)}, deadline_ms=NOW() + 3_600_000)
    assert [o.action for o in take(b)] == ["inbox"]
    assert b.svc.inbox()[0]["envelope_sha256"] == sent["envelope_sha256"]


def test_real_hub_over_cap_refused_before_send(two: tuple[Host, Host]) -> None:
    a, _ = two
    with pytest.raises(W.EnvelopeError) as e:
        a.svc.send_ticket(two[1].ws, {"pad": "x" * W.MAX_ENVELOPE}, deadline_ms=NOW() + 3_600_000)
    assert e.value.code == "too_large" and not a.mb.sent


def test_real_hub_route_to_unjoined_workspace_is_not_allowed(two: tuple[Host, Host]) -> None:
    a, _ = two
    ghost = suite.generate_private_key()
    ghost_ws = "dd" * 16
    a.mb.pin_peer(ghost_ws, suite.public_bytes(ghost), suite.public_bytes(ghost), 1)
    with pytest.raises(HubRefused) as e:
        a.svc.send_ticket(ghost_ws, {"title": "x"}, deadline_ms=NOW() + 3_600_000)
    assert e.value.code == "not_allowed"

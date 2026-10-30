"""R14b: the host's display status (version, host, workspace name) lands in a REAL hub's registry after ``hub.connect``,
over the real TLS link (the hub runs in this process from packages/hub/src; no part of it is stubbed). It never landed
before because the TLS client's closed op list did not name ``status_put``, and the host swallowed the refusal."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from orch.host import hubops
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok

HUB_SRC = Path(__file__).resolve().parents[3] / "hub" / "src"
pytestmark = pytest.mark.skipif(not (HUB_SRC / "orch_hub" / "link").is_dir(), reason="packages/hub not present")

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


class _Invites:
    def check(self, invite, now_ms):
        return None

    def consume(self, invite, workspace_id):
        pass


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


@pytest.fixture
def real_hub(served):
    sys.path.insert(0, str(HUB_SRC))
    try:
        from orch_hub.keys import HubKeys
        from orch_hub.link import LinkServer, Registry
        from orch_hub.link.tls import TlsIdentity
        from orch_hub.link.wkseal import WkSeals
    finally:
        sys.path.remove(str(HUB_SRC))
    with tempfile.TemporaryDirectory(prefix="hs") as d:
        now = lambda: int(served.lab.ws.clock[0] * 1000)  # noqa: E731 - the lab's clock: the card must be valid then
        keys = HubKeys.init(Path(d) / "hub", now_ms=now)
        reg = Registry()
        seals = WkSeals(":memory:", hub_id=keys.hub_id, hxk_pub=keys.hxk_pub, exchange=keys.hxk_exchange, now_ms=now)
        srv = LinkServer.from_keys(
            keys,
            identity=TlsIdentity.generate(),
            registry=reg,
            invites=_Invites(),
            dev_mode=True,
            now_ms=now,
            wk_seals=seals,
        ).start()
        try:
            yield {"card": keys.card(), "addr": f"127.0.0.1:{srv.address[1]}", "registry": reg}
        finally:
            srv.close()


@pytest.mark.parametrize("mode", ["full", "selective"])
def test_registry_row_has_version_and_name_after_join(human, served, real_hub, monkeypatch, mode):
    import orch

    monkeypatch.setattr(hubops, "_workspace_name", lambda service: "Acme core")
    human.run("hub.join", {"hub_card": real_hub["card"], "mode": mode})
    args = {"invite": "inv", "hub_card": real_hub["card"], "addr": real_hub["addr"]}
    got = ok(served.client().req("hub.connect", args, idem=True))["data"]
    assert got["status"] == "bound"
    reg = real_hub["registry"]
    assert reg.status(served.wid)["version"] == orch.__version__
    # selective keeps host and name private (the hub then shows its friendly fallback, never an id)
    assert reg.display_name(served.wid) == ("Acme core" if mode == "full" else None)


def test_hub_update_request_becomes_a_self_clearing_notice(monkeypatch):
    """HUB-WS-04: the hub's admin asked for an update; the status_put reply carries only the version; the host keeps a
    notice while its own version is lower and drops it when it is not. The request is polled, never pushed."""
    from types import SimpleNamespace

    import orch

    monkeypatch.setattr(hubops, "_workspace_name", lambda service: "n")
    clock = [1000.0]
    service = SimpleNamespace(clock=lambda: clock[0])
    state = SimpleNamespace(hub_id="h1", mode="full")
    replies = [{}]
    calls = []

    class Link:
        def call(self, op, body):
            calls.append(op)
            return replies[0]

    hubops._send_status(service, state, Link())
    assert hubops._update_notice(service, state) is None and calls == ["status_put"]
    hubops._send_status(service, state, Link())
    assert calls == ["status_put"]  # same body, inside the poll time: not sent again
    replies[0] = {"update_requested": {"target_version": "99.0.0"}}
    clock[0] += hubops.STATUS_POLL_S + 1  # the next link use polls again
    hubops._send_status(service, state, Link())
    assert calls == ["status_put"] * 2
    assert hubops._update_notice(service, state) == {"target_version": "99.0.0"}
    monkeypatch.setattr(orch, "__version__", "99.1.0")  # updated: the notice clears itself
    assert hubops._update_notice(service, state) is None
    monkeypatch.setattr(orch, "__version__", "1.0.0")
    replies[0] = {}  # the hub dropped the request
    clock[0] += hubops.STATUS_POLL_S + 1
    hubops._send_status(service, state, Link())
    assert hubops._update_notice(service, state) is None


def test_the_status_put_reply_accepts_only_the_version_and_no_text():
    from orch.hubclient.tlslink.client import _REPLY

    ok_reply = _REPLY["status_put"]
    assert ok_reply({}) and ok_reply({"update_requested": {"target_version": "0.1.1"}})
    assert not ok_reply({"update_requested": {"target_version": "0.1.1", "text": "x"}})
    assert not ok_reply({"update_requested": {"target_version": "click <b>here</b>"}})
    assert not ok_reply({"update_requested": {"target_version": "1" * 33}})
    assert not ok_reply({"other": 1})

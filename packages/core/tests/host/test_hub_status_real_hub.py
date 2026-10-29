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

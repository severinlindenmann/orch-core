"""D14 adversarial (Opus review of orch-core#1171): a replayed older, looser policy is refused and the strict pin
stays; the same version with another policy is a conflict. (The None/raise shape is in test_snapshot.py.)"""

from __future__ import annotations

import pytest

from orch.hubclient import HubRefused
from orch.hubclient import snapshot as snap

from .test_snapshot import ADMIN, COMPANY_POLICY, NOW, WS, Rig

STRICT = {**COMPANY_POLICY, "markets": [], "ceiling": []}


def test_a_replayed_older_looser_policy_is_refused_and_the_strict_one_stays():
    r = Rig()
    loose = r.hub.set_snapshot(WS, {ADMIN: "admin"})  # default policy, version 2
    r.refresh()
    r.hub.set_snapshot(WS, {ADMIN: "admin"}, market_policy=STRICT)  # version 3
    r.refresh()
    r.hub.workspaces[WS].snapshot = loose
    with pytest.raises(HubRefused) as e:
        r.refresh()
    assert e.value.code == "stale_snapshot"
    version, pol = snap.hub_market_policy(r.state, now_ms=NOW, dev_mode=True)
    assert version == 3 and pol.markets == () and pol.ceiling == ()


def test_same_version_with_a_looser_policy_is_a_conflict():
    r = Rig()
    r.hub.set_snapshot(WS, {ADMIN: "admin"}, market_policy=STRICT)
    r.refresh()
    ws = r.hub.workspaces[WS]
    v = ws.snapshot["o"]["version"]
    ws.snapshot = r.hub._snapshot(WS, v, {ADMIN: "admin"}, [], None)  # same version, default (looser) policy
    with pytest.raises(HubRefused) as e:
        r.refresh()
    assert e.value.code == "snapshot_conflict"

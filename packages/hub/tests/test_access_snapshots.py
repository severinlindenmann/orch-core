"""Snapshots built from the access model: contract bytes, signing, versions across restarts, removal."""

from __future__ import annotations

import pytest

from orch_hub.access import Access, AccessSnapshots
from orch_hub.contract import hub as H
from orch_hub.contract.canon import cj
from orch_hub.contract.hub import Refused
from orch_hub.contract.vectorgen import snapshot as vec_snapshot
from orch_hub.keys import HubKeys

WS, WS2 = "a1" * 16, "b2" * 16
U1, U2 = "01" * 16, "02" * 16


class Clock:
    def __init__(self):
        self.t = 1_000_000

    def __call__(self):
        self.t += 1000
        return self.t


@pytest.fixture
def env(tmp_path):
    keys = HubKeys.init(tmp_path / "hub")
    clk = Clock()
    acc = Access(str(tmp_path / "access.db"))
    for u in (U1, U2):
        acc.add_user(u, "u" + u[:2])
    acc.add_group("g")
    acc.add_member("g", U1)
    acc.grant(WS, "g", "edit")
    snaps = AccessSnapshots(acc, keys, clk)
    yield keys, acc, snaps, tmp_path
    acc.close()


def verify(keys, signed, pinned=None, ws=WS):
    held = H.verify_card(keys.card(), now_ms=keys._now(), dev_mode=True, pinned_root=keys.hk_root_pub)
    return H.verify_snapshot(
        signed, held, workspace_id=ws, dev_mode=True, pinned=pinned, revoked_op_certs=keys.revoked_op_certs()
    )


def test_snapshot_verifies_and_matches_vector_shape(env):
    keys, _acc, snaps, _ = env
    s = snaps.snapshot(WS, "direct")
    o = verify(keys, s)
    assert o["roles"] == {U1: "edit"} and o["version"] == 1 and o["generation"] == 0 and o["dev"] is True
    assert o["revoked_channels"] == [] and o["op_cert_id"] == keys.op_cert()["o"]["cert_id"]
    assert set(o) == set(vec_snapshot()["o"]) and set(s) == {"o", "sig"}
    assert o["workspace_id"] == WS and o["hub_id"] == keys.hub_id


def test_unchanged_content_returns_identical_bytes(env):
    keys, _acc, snaps, _ = env
    a = snaps.snapshot(WS, "direct")
    b = snaps.snapshot(WS, "direct")
    assert cj(a) == cj(b)
    assert verify(keys, b, pinned=a["o"])["version"] == 1  # a no-op at the host, not snapshot_conflict


def test_version_bumps_per_workspace_on_relevant_changes_only(env):
    _keys, acc, snaps, _ = env
    v1 = snaps.snapshot(WS, "direct")["o"]
    other = snaps.snapshot(WS2, "direct")["o"]
    acc.add_member("g", U2)
    v2 = snaps.snapshot(WS, "direct")["o"]
    assert v2["version"] == 2 and v2["roles"] == {U1: "edit", U2: "edit"}
    assert snaps.snapshot(WS2, "direct")["o"]["version"] == other["version"] == 1  # WS2 unaffected
    acc.grant(WS, "g", "admin")
    assert snaps.snapshot(WS, "direct")["o"]["version"] == 3
    assert v1["version"] == 1


def test_versions_never_reused_after_restart_and_removal_shows(env, tmp_path):
    keys, acc, snaps, tmp = env
    v1 = snaps.snapshot(WS, "direct")
    acc.close()
    acc2 = Access(str(tmp / "access.db"))
    snaps2 = AccessSnapshots(acc2, HubKeys.open(tmp / "hub"), Clock())
    again = snaps2.snapshot(WS, "direct")
    assert cj(again) == cj(v1)  # same content, same stored bytes
    acc2.remove_user(U1)
    v2 = snaps2.snapshot(WS, "direct")["o"]
    assert v2["version"] == 2 and v2["roles"] == {}
    # the old snapshot cannot bring the user back
    with pytest.raises(Refused) as e:
        verify(keys, v1, pinned=v2)
    assert e.value.code == "stale_snapshot"
    acc2.close()


def test_group_removal_and_hub_admin_and_revoked_channels(env):
    keys, acc, snaps, _ = env
    acc.add_member("hub-admin", U2)
    acc.revoke_channel("cd" * 16)
    acc.revoke_channel("ab" * 16)
    o = verify(keys, snaps.snapshot(WS2, "direct"), ws=WS2)  # hub-admin has a role in a workspace with no grants
    assert o["roles"] == {U2: "admin"} and o["revoked_channels"] == ["ab" * 16, "cd" * 16]
    acc.remove_group("g")
    assert verify(keys, snaps.snapshot(WS, "direct"))["roles"] == {U2: "admin"}


def test_op_rotation_bumps_version_and_old_cert_is_stale(env):
    keys, _acc, snaps, _ = env
    a = snaps.snapshot(WS, "direct")
    keys.rotate_op()
    b = snaps.snapshot(WS, "direct")
    assert b["o"]["version"] == 2 and b["o"]["op_cert_id"] == keys.op_cert()["o"]["cert_id"]
    verify(keys, b, pinned=a["o"])
    with pytest.raises(Refused) as e:
        verify(keys, a)
    assert e.value.code in ("stale_op_cert", "bad_signature")


def test_fail_closed_on_store_error_and_revoked_op(env):
    keys, _acc, snaps, _ = env
    keys.revoke_op()
    with pytest.raises(Refused) as e:
        snaps.snapshot(WS, "direct")
    assert e.value.code == "unavailable"


def test_fail_closed_when_store_closed(env):
    _keys, acc, snaps, _ = env
    acc.close()
    with pytest.raises(Refused) as e:
        snaps.snapshot(WS, "direct")
    assert e.value.code == "unavailable"

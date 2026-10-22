"""``orch-hub users|groups|grants`` and ``orch-hub serve`` handing out the signed snapshot over a real link."""

from __future__ import annotations

import json

import pytest
from publish_helpers import FakeUpstream, binding
from test_publish_serve import link, start, stop

from orch_hub import cli
from orch_hub.contract import hub as H
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.keys import HubKeys

WS = G.WS_A


def run(state, capsys, *argv, code=0):
    rc = cli.main([argv[0], "--state", str(state), *argv[1:]])
    out = capsys.readouterr()
    assert rc == code, out.err
    return json.loads(out.out) if code == 0 else out.err


def held(keys):
    return H.verify_card(keys.card(), now_ms=keys._now(), dev_mode=True, pinned_root=keys.hk_root_pub)


def test_cli_roundtrip(tmp_path, capsys):
    st = tmp_path / "hub"
    HubKeys.init(st)
    ann = run(st, capsys, "users", "add", "ann")["user_id"]
    bob = run(st, capsys, "users", "add", "bob")["user_id"]
    assert HubKeys.open(st).uk_cert(ann)  # the id is the UK's user id
    run(st, capsys, "groups", "add", "devs")
    run(st, capsys, "groups", "add-member", "devs", ann)
    run(st, capsys, "groups", "add-member", "hub-admin", bob)
    run(st, capsys, "grants", "set", WS, "devs", "edit")
    roles = run(st, capsys, "grants", "roles", WS)
    assert roles == dict(sorted({ann: "edit", bob: "admin"}.items()))
    assert run(st, capsys, "grants", "list") == [{"workspace_id": WS, "group": "devs", "role": "edit"}]
    assert {u["name"] for u in run(st, capsys, "users", "list")} == {"ann", "bob"}
    assert run(st, capsys, "grants", "set", WS, "hub-admin", "read", code=1)
    run(st, capsys, "users", "remove", ann)
    assert run(st, capsys, "grants", "roles", WS) == {bob: "admin"}
    assert "unknown_group" in run(st, capsys, "grants", "set", WS, "nope", "read", code=1)
    run(st, capsys, "channels", "revoke", "ab" * 16)
    assert run(st, capsys, "channels", "list") == ["ab" * 16]
    assert (st / "access.db").stat().st_mode & 0o077 == 0


def test_cli_refuses_state_dir_that_is_a_symlink(tmp_path, capsys):
    real = tmp_path / "hub"
    HubKeys.init(real)
    (tmp_path / "ln").symlink_to(real)
    err = run(tmp_path / "ln", capsys, "groups", "list", code=1)
    assert "not_state_owner" in err


@pytest.fixture
def up():
    u = FakeUpstream()
    yield u
    u.close()


def test_serve_snapshot_get_over_real_link(tmp_path, capsys, up):
    st = tmp_path / "hub"
    HubKeys.init(st)
    ann = run(st, capsys, "users", "add", "ann")["user_id"]
    run(st, capsys, "groups", "add", "devs")
    run(st, capsys, "groups", "add-member", "devs", ann)
    run(st, capsys, "grants", "set", WS, "devs", "read")
    p, port, pin, hub_id = start(st, up, "inv1")
    try:
        keys = HubKeys.open(st)
        c = link(port, pin, hub_id, keys.op_signer.public, True)
        body = {
            "invite": "inv1",
            "card": G.v2_card(),
            "pk_pub": b64u(G.K.pub["pk_alice"]),
            "binding": binding(G.K.priv["wsk_a"], keys, WS),
        }
        assert c.request("join", body) == {}
        s1 = c.request("snapshot_get", {})["snapshot"]
        o1 = H.verify_snapshot(s1, held(keys), workspace_id=WS, dev_mode=True, pinned=None)
        assert o1["roles"] == {ann: "read"} and o1["version"] == 1
        assert c.request("snapshot_get", {})["snapshot"] == s1  # unchanged: identical bytes
        run(st, capsys, "grants", "set", WS, "devs", "admin")  # a separate process changes the store live
        o2 = H.verify_snapshot(
            c.request("snapshot_get", {})["snapshot"], held(keys), workspace_id=WS, dev_mode=True, pinned=o1
        )
        assert o2["roles"] == {ann: "admin"} and o2["version"] == 2
        c.close()
    finally:
        stop(p)
    # restart: the version continues
    p, port, pin, hub_id = start(st, up)
    try:
        c = link(port, pin, hub_id, HubKeys.open(st).op_signer.public, False)
        o3 = H.verify_snapshot(
            c.request("snapshot_get", {})["snapshot"], held(HubKeys.open(st)), workspace_id=WS, dev_mode=True, pinned=o2
        )
        assert o3["version"] == 2
        c.close()
    finally:
        stop(p)

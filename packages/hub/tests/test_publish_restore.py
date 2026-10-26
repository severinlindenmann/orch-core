"""Signed binding objects survive a restart: the registry persists them and HubPublish re-pushes them."""

from __future__ import annotations

import os

from publish_helpers import HOST, FakeUpstream, RecordingSync, host_request

from orch_hub.contract import keystore as KS
from orch_hub.contract import vectorgen as G
from orch_hub.link import Registry
from orch_hub.publish import HubPublish, PublishConfig


def test_registry_keeps_the_signed_objects_across_reopen(tmp_path, make_hub):
    path = str(tmp_path / "reg.db")
    h = make_hub(registry=Registry(path))
    c = h.open(first=True)
    c.request("join", h.join_body())
    assert h.registry.binding_objects() == {G.WS_A: G.binding()}
    c.request("binding_put", {"object": G.unbound()})
    assert h.registry.binding_objects() == {G.WS_A: G.unbound()}  # the newest accepted object wins
    h.registry.close()
    again = Registry(path)
    assert again.binding_objects() == {G.WS_A: G.unbound()}
    again.close()


def test_old_registry_file_without_the_column_is_migrated(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE workspace (workspace_id TEXT PRIMARY KEY, owner_person_id TEXT NOT NULL, wsk_pub TEXT NOT NULL,"
        " pk_pub TEXT NOT NULL, card TEXT NOT NULL, binding_state TEXT NOT NULL)"
    )
    db.commit()
    db.close()
    os.chmod(path, 0o600)  # a pre-existing file must already be private
    r = Registry(path)
    assert r.binding_objects() == {}
    r.close()


def test_restore_then_forward_pushes_card_and_binding(tmp_path):
    up = FakeUpstream()
    try:
        sync = RecordingSync()
        signer = KS.OpSigner(G.K.priv["hk_op"])
        fw = HubPublish(lambda: (signer, G.card(), G.HUB_ID), sync, PublishConfig(up.url, HOST))
        fw.restore({G.WS_A: G.binding()})
        assert [c[0] for c in sync.calls] == ["card", "binding"]
        assert fw.forward(G.WS_A, host_request(G.K.priv["wsk_a"], G.HUB_ID, G.WS_A))["status"] == 200
        # a restored unbound is final too
        fw2 = HubPublish(lambda: (signer, G.card(), G.HUB_ID), RecordingSync(), PublishConfig(up.url, HOST))
        fw2.restore({G.WS_A: G.unbound()})
        fw2.restore({G.WS_A: G.binding()})  # dropped: cannot replace the unbound
        assert fw2._bindings[G.WS_A]["o"]["kind"] == "hub.unbound"
    finally:
        up.close()

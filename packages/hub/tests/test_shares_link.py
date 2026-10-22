# ruff: noqa: F811  (the `shub` fixture is imported then named as an argument)
"""share_put / share_revoke over real links (protocol.md §10.2): selective only, replace, revoke, purge, caps."""

from __future__ import annotations

import pytest
from shares_helpers import IT1, IT2, OWNER, refused, sealed, shub  # noqa: F401

from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, unb64u
from orch_hub.link import LinkError
from orch_hub.shares import MAX_ITEM_BYTES, MAX_ITEMS, NotSpecified, open_shared_item


def test_put_replace_revoke(shub):
    h = shub()
    c = h.join()
    assert c.request("share_put", {"item_id": IT1, "sealed": sealed("a")}) == {}
    assert h.share_store.get(G.WS_A, IT1)[0] == unb64u(sealed("a"))
    # replace: the later put wins
    assert c.request("share_put", {"item_id": IT1, "sealed": sealed("b")}) == {}
    assert h.share_store.get(G.WS_A, IT1)[0] == unb64u(sealed("b"))
    assert h.share_store.usage(G.WS_A)[0] == 1
    # identical bytes again: idempotent, no extra log row
    n = len(h.share_store.log())
    assert c.request("share_put", {"item_id": IT1, "sealed": sealed("b")}) == {}
    assert len(h.share_store.log()) == n
    assert c.request("share_revoke", {"item_id": IT1}) == {}
    assert h.share_store.get(G.WS_A, IT1) is None
    assert c.request("share_revoke", {"item_id": IT1}) == {}  # a retried revoke is an idempotent {}
    assert [e["change"] for e in h.share_store.log()] == ["put", "replace", "revoke"]
    assert [e["change"] for e in h.events] == ["put", "replace", "revoke"]


def test_audit_log_holds_no_content(shub):
    h = shub()
    c = h.join()
    c.request("share_put", {"item_id": IT1, "sealed": sealed("secret-title")})
    for row in h.share_store.log():
        assert set(row) == {"ws", "item_id", "change", "digest", "count", "ts_ms"}
        assert "secret-title" not in repr(row)


def test_full_mode_refused(shub):
    h = shub()
    h.join("full")
    for op, body in (("share_put", {"item_id": IT1, "sealed": sealed("a")}), ("share_revoke", {"item_id": IT1})):
        c = h.open()  # a refusal at the link drops it: one fresh link per op
        c.send_raw(c.build(op, body))
        with pytest.raises(LinkError) as e:
            c.recv_reply()
        assert e.value.code == "wrong_mode"  # §10.2 step 3: refused at the link, never reaches the store
    assert h.share_store.usage(G.WS_A) == (0, 0)


def test_service_itself_refuses_full_mode(shub):
    """Defence in depth: even if the link layer let it through, the handler checks the live binding's mode."""
    from orch_hub.contract.hub import Refused

    h = shub()
    h.join("full")
    with pytest.raises(Refused) as e:
        h.svc.put(G.WS_A, {"item_id": IT1, "sealed": sealed("a")})
    assert e.value.code == "wrong_mode"
    with pytest.raises(Refused) as e:
        h.svc.put(G.WS_B, {"item_id": IT1, "sealed": sealed("a")})  # never joined
    assert e.value.code == "unknown_workspace"


def test_wk_seal_store_stays_empty_for_selective(shub):
    h = shub()
    c = h.join()
    c.request("share_put", {"item_id": IT1, "sealed": sealed("a")})
    c.send_raw(c.build("wk_seal", {"epoch": 1, "wrap": "AA"}))
    with pytest.raises(LinkError) as e:
        c.recv_reply()
    assert e.value.code == "wrong_mode"


def test_unbind_purges_every_item(shub):
    h = shub()
    c = h.join()
    c.request("share_put", {"item_id": IT1, "sealed": sealed("a")})
    c.request("share_put", {"item_id": IT2, "sealed": sealed("b")})
    assert h.share_store.usage(G.WS_A)[0] == 2
    assert c.request("binding_put", {"object": G.unbound(seq=1)}) == {}
    assert h.share_store.usage(G.WS_A) == (0, 0)
    last = h.share_store.log(G.WS_A)[-1]
    assert last["change"] == "unbind" and last["count"] == 2 and last["item_id"] is None
    assert [e["change"] for e in h.events][-1] == "unbind"
    refused(c, "share_put", {"item_id": IT1, "sealed": sealed("a")}, "not_bound")


def test_switch_to_full_purges(shub):
    h = shub()
    c = h.join()
    c.request("share_put", {"item_id": IT1, "sealed": sealed("a")})
    assert c.request("binding_put", {"object": G.binding(seq=2, mode="full")}) == {}
    assert h.share_store.usage(G.WS_A) == (0, 0)


def test_reconcile_after_restart_drops_orphans(shub, tmp_path):
    h = shub(store_path=str(tmp_path / "shares.db"))
    c = h.join()
    c.request("share_put", {"item_id": IT1, "sealed": sealed("a")})
    # simulate a crash between the unbind and the purge: flip the registry only
    h.registry.set_binding_state(G.WS_A, {"binding_seq": 1, "status": "unbound", "binding": None})
    assert h.share_store.usage(G.WS_A)[0] == 1
    h.svc.reconcile()
    assert h.share_store.usage(G.WS_A) == (0, 0)


def test_size_caps(shub):
    h = shub()
    c = h.join()
    refused(c, "share_put", {"item_id": IT1, "sealed": b64u(bytes(MAX_ITEM_BYTES + 1))}, "too_large")
    assert c.request("share_put", {"item_id": IT1, "sealed": b64u(bytes(MAX_ITEM_BYTES))}) == {}
    refused(c, "share_put", {"item_id": IT2, "sealed": ""}, "malformed")  # empty
    assert h.share_store.usage(G.WS_A) == (1, MAX_ITEM_BYTES)


def test_item_count_and_workspace_byte_caps(shub, monkeypatch):
    import orch_hub.shares.service as SV

    monkeypatch.setattr(SV, "MAX_ITEMS", 2)
    monkeypatch.setattr(SV, "MAX_WS_BYTES", 100)
    h = shub()
    c = h.join()
    c.request("share_put", {"item_id": IT1, "sealed": sealed("a", 40)})
    c.request("share_put", {"item_id": IT2, "sealed": sealed("b", 40)})
    refused(c, "share_put", {"item_id": G.fake16("it3"), "sealed": sealed("c", 10)}, "too_large")  # third item
    refused(c, "share_put", {"item_id": IT1, "sealed": sealed("z", 70)}, "too_large")  # 70 + 40 > 100
    assert c.request("share_put", {"item_id": IT1, "sealed": sealed("z", 60)}) == {}  # replace within the cap
    assert h.share_store.usage(G.WS_A) == (2, 100)
    assert MAX_ITEMS == 1000


def test_malformed_bodies(shub):
    h = shub()
    c = h.join()
    for body in ({"item_id": "nothex", "sealed": sealed("a")}, {"item_id": IT1}, {"item_id": IT1, "sealed": "!!"}):
        c.send_raw(c.build("share_put", body))
        with pytest.raises(LinkError) as e:
            c.recv_reply()
        assert e.value.code == "malformed"
        c = h.open()  # the link was dropped


def test_workspaces_are_separate(shub):
    h = shub()
    a = h.join()
    b = h.open(G.WS_B, "wsk_b", first=True)
    b.request("join", h.join_body("invB", G.WS_B, wsk="wsk_b"))
    a.request("share_put", {"item_id": IT1, "sealed": sealed("a")})
    b.request("share_put", {"item_id": IT1, "sealed": sealed("b")})  # same item id, other workspace
    assert h.share_store.get(G.WS_A, IT1)[0] != h.share_store.get(G.WS_B, IT1)[0]
    b.request("share_revoke", {"item_id": IT1})
    assert h.share_store.get(G.WS_A, IT1) is not None


def test_hub_stores_exactly_what_it_received(shub, tmp_path):
    marker = b"PLAINTEXT-MARKER-0123456789"
    raw = marker + bytes(range(64))  # stands in for an opaque sealed blob
    p = tmp_path / "shares.db"
    h = shub(store_path=str(p))
    c = h.join()
    c.request("share_put", {"item_id": IT1, "sealed": b64u(raw)})
    assert h.share_store.get(G.WS_A, IT1)[0] == raw  # byte for byte; no transformation, no extracted fields
    assert h.share_store.items(G.WS_A)[0].keys() == {"item_id", "sealed", "digest", "size", "stored_ms"}
    assert (p.stat().st_mode & 0o077) == 0  # private db


def test_open_shared_item_is_not_specified():
    with pytest.raises(NotSpecified):
        open_shared_item(b"anything")


def test_max_items_constant():
    assert MAX_ITEMS == 1000 and OWNER

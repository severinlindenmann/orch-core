# ruff: noqa: F811  (the `shub` fixture is imported then named as an argument)
"""share_put / share_revoke over real links (protocol.md §10.2, §10.2b): selective only, the version rule, tombstones,
purge, limits, and the `share` events on the hub audit chain."""

from __future__ import annotations

import pytest
from shares_helpers import IT1, IT2, IT3, OWNER, audit_records, body, refused, shub  # noqa: F401

import orch_hub.contract.phase4 as P4
from orch_hub.contract import vectorgen as G
from orch_hub.contract import vectorgen_p4 as V4
from orch_hub.contract.canon import b64u, unb64u
from orch_hub.contract.hub import Refused
from orch_hub.link import LinkError
from orch_hub.shares import MAX_ITEM_BYTES, MAX_ITEMS, open_shared_item


def test_limits_are_the_contracts():
    assert MAX_ITEM_BYTES == 16 * 1024 and MAX_ITEMS == 1000 and OWNER


def test_put_replace_stale_revoke_reshare(shub):
    h = shub()
    c = h.join()
    b1 = body(IT1, 1)
    assert c.request("share_put", b1) == {}
    assert h.share_store.get(G.WS_A, IT1)[0] == unb64u(b1["sealed"])
    n = len(h.share_store.log())
    assert c.request("share_put", b1) == {}  # same version, same bytes: idempotent, nothing logged or audited
    assert len(h.share_store.log()) == n and len(audit_records(h)) == 1
    b2 = body(IT1, 2, title="Still open")
    assert c.request("share_put", b2) == {}  # a higher version replaces
    assert h.share_store.get(G.WS_A, IT1)[0] == unb64u(b2["sealed"])
    assert h.share_store.usage(G.WS_A)[0] == 1
    refused(c, "share_put", b1, "stale_version")  # lower
    refused(c, "share_put", body(IT1, 2, title="Other"), "stale_version")  # equal version, other bytes
    assert h.share_store.held(G.WS_A, IT1)["item_version"] == 2
    assert c.request("share_revoke", {"item_id": IT1}) == {}
    assert h.share_store.get(G.WS_A, IT1) is None
    assert h.share_store.held(G.WS_A, IT1) == {
        "item_version": 2,
        "hash": P4.item_hash(V4.share_item(2, item_id=IT1, content={**V4.NEEDS_YOU, "title": "Still open"})),
        "revoked": True,
    }
    assert c.request("share_revoke", {"item_id": IT1}) == {}  # idempotent
    assert c.request("share_revoke", {"item_id": IT2}) == {}  # an id the hub never saw
    refused(c, "share_put", b2, "stale_version")  # a revoked item cannot be replayed at its old version
    refused(c, "share_put", b1, "stale_version")
    assert c.request("share_put", body(IT1, 3)) == {}  # shared again with a higher version
    assert h.share_store.usage(G.WS_A)[0] == 1
    assert [e["change"] for e in h.share_store.log()] == ["put", "replace", "revoke", "put"]


def test_every_accepted_change_is_a_share_event_on_the_chain(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body(IT1, 1))
    c.request("share_put", body(IT1, 2, title="v2"))
    c.request("share_put", body(IT1, 2, title="v2"))  # resend: no event
    c.request("share_revoke", {"item_id": IT1})
    c.request("share_revoke", {"item_id": IT1})  # idempotent: no event
    refused(c, "share_put", body(IT1, 1), "stale_version")  # refused: no event
    recs = audit_records(h)
    assert [(r["type"], ev["change"], ev["item_version"]) for r, ev in recs] == [
        ("share", "put", 1),
        ("share", "put", 2),
        ("share", "revoke", 2),
    ]
    for r, ev in recs:
        assert r["scope_tag"] is not None and ev["workspace_id"] == G.WS_A and ev["item_id"] == IT1
        assert len(unb64u(ev["nonce"])) == 32
    assert recs[0][1]["item_hash"] == P4.item_hash(V4.share_item(1, item_id=IT1)) and recs[2][1]["item_hash"] is None
    # the chain holds hashes and tags only: no title anywhere in a record
    assert all("tariffs" not in repr(r) for r, _ in recs)


def test_a_failing_audit_sink_refuses_the_change(shub):
    h = shub()
    c = h.join()

    def boom(_ev):
        raise RuntimeError("audit down")

    h.svc._on_change = boom
    with pytest.raises(LinkError) as e:
        c.request("share_put", body(IT1, 1))
    assert e.value.code == "internal"
    assert h.share_store.usage(G.WS_A) == (0, 0)  # audited BEFORE it commits: nothing stored


def test_audit_log_holds_no_content(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body(IT1, 1, title="secret-title"))
    for row in h.share_store.log():
        assert set(row) == {"ws", "item_id", "change", "digest", "count", "ts_ms"}
        assert "secret-title" not in repr(row)


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda: V4.share_body(V4.share_item(wsk="wsk_b")), "bad_signature"),
        (lambda: V4.share_body(V4.share_item(label="sig_space_item")), "bad_signature"),
        (lambda: V4.share_body(hxk="wxk_a"), "bad_wrap"),
        (lambda: V4.share_body(item_id=IT2), "bad_wrap"),  # the item moved to another id
        (lambda: {"item_id": V4.ITEM, "sealed": b64u(bytes(40))}, "bad_wrap"),
        (lambda: body(title="a\nb"), "malformed"),  # the text rule
        (lambda: body(title="x" * 201), "malformed"),
        (lambda: body(title="a" + chr(0xF8FF)), "malformed"),  # L2P4-3: private use, via the contract's text rule
        (lambda: body(title="ㅤ"), "malformed"),  # L2P4-3: Default_Ignorable "blank"
        (lambda: V4.share_body(V4.share_item(workspace_id=G.WS_B)), "bad_wrap"),
        (lambda: V4.share_body(V4.share_item(item_kind="ticket", content=V4.NEEDS_YOU)), "malformed"),
        (lambda: V4.share_body(V4.share_item(content={**V4.NEEDS_YOU, "extra": 1})), "malformed"),
        (lambda: {"item_id": V4.ITEM, "sealed": b64u(bytes(MAX_ITEM_BYTES + 1))}, "too_large"),
    ],
)
def test_refusals_store_nothing(shub, make, code):
    h = shub()
    c = h.join()
    refused(c, "share_put", make(), code)
    assert h.share_store.usage(G.WS_A) == (0, 0) and audit_records(h) == []


def test_dev_item_is_refused_at_a_real_hub(shub):
    h = shub()
    c = h.join()
    h.svc._dev = False
    refused(c, "share_put", body(), "dev_refused")


def test_item_cap(shub, monkeypatch):
    monkeypatch.setattr(P4, "SHARE_MAX_ITEMS", 2)
    h = shub()
    c = h.join()
    c.request("share_put", body(IT1, 1))
    c.request("share_put", body(IT2, 1))
    refused(c, "share_put", body(IT3, 1), "too_many_items")
    assert c.request("share_put", body(IT1, 2, title="replace at the cap")) == {}
    c.request("share_revoke", {"item_id": IT2})  # a tombstone is not a live item
    assert c.request("share_put", body(IT3, 1)) == {}
    refused(c, "share_put", body(IT2, 2), "too_many_items")  # re-sharing a revoked id counts as a new live item


def test_full_mode_refused(shub):
    h = shub()
    h.join("full")
    for op, b in (("share_put", body()), ("share_revoke", {"item_id": IT1})):
        c = h.open()  # a refusal at the link drops it: one fresh link per op
        c.send_raw(c.build(op, b))
        with pytest.raises(LinkError) as e:
            c.recv_reply()
        assert e.value.code == "wrong_mode"  # §10.2 step 3: refused at the link, never reaches the store
    assert h.share_store.usage(G.WS_A) == (0, 0)


def test_service_itself_refuses_full_mode(shub):
    """Defence in depth: even if the link layer let it through, the handler checks the live binding's mode."""
    h = shub()
    h.join("full")
    with pytest.raises(Refused) as e:
        h.svc.put(G.WS_A, body())
    assert e.value.code == "wrong_mode"
    with pytest.raises(Refused) as e:
        h.svc.put(G.WS_B, body())  # never joined
    assert e.value.code == "unknown_workspace"


def test_wk_seal_store_stays_empty_for_selective(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body())
    c.send_raw(c.build("wk_seal", {"epoch": 1, "wrap": "AA"}))
    with pytest.raises(LinkError) as e:
        c.recv_reply()
    assert e.value.code == "wrong_mode"


def test_unbind_purges_every_item_and_keeps_the_versions(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body(IT1, 1))
    c.request("share_put", body(IT2, 3))
    assert h.share_store.usage(G.WS_A)[0] == 2
    assert c.request("binding_put", {"object": G.unbound(seq=1)}) == {}
    assert h.share_store.usage(G.WS_A) == (0, 0)
    last = h.share_store.log(G.WS_A)[-1]
    assert last["change"] == "unbind" and last["count"] == 2 and last["item_id"] is None
    assert h.share_store.held(G.WS_A, IT2)["revoked"] and h.share_store.held(G.WS_A, IT2)["item_version"] == 3
    recs = audit_records(h)
    assert [ev["change"] for _r, ev in recs] == ["put", "put", "purge"]  # §10.5: one purge record, with the count
    _r, ev = recs[-1]
    assert (ev["item_id"], ev["item_version"], ev["item_hash"], ev["count"]) == (None, None, None, 2)
    assert ev["workspace_id"] == G.WS_A and len(unb64u(ev["nonce"])) == 32
    refused(c, "share_put", body(), "not_bound")


def test_switch_to_full_purges(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body())
    assert c.request("binding_put", {"object": G.binding(seq=2, mode="full")}) == {}
    assert h.share_store.usage(G.WS_A) == (0, 0)
    ev = audit_records(h)[-1][1]
    assert (ev["change"], ev["count"]) == ("purge", 1)


def test_purge_with_nothing_live_writes_no_record(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body())
    c.request("share_revoke", {"item_id": IT1})
    assert [e["change"] for _r, e in audit_records(h)] == ["put", "revoke"]
    c.request("binding_put", {"object": G.unbound(seq=1)})
    assert [e["change"] for _r, e in audit_records(h)] == ["put", "revoke"]  # nothing live: no purge record


def test_a_failing_audit_sink_refuses_the_purge(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body())
    c.request("share_put", body(IT2, 1))

    def boom(_ev):
        raise RuntimeError("audit down")

    h.svc._on_change = boom
    with pytest.raises(RuntimeError):
        h.svc.purge(G.WS_A)
    assert h.share_store.usage(G.WS_A)[0] == 2  # audited BEFORE it deletes: nothing purged
    h.svc._on_change = lambda ev: h.audit.share_change(ev)
    h.svc.purge(G.WS_A)
    assert h.share_store.usage(G.WS_A)[0] == 0
    assert audit_records(h)[-1][1]["count"] == 2


def test_revoke_record_fields_and_no_record_for_a_noop(shub):
    h = shub()
    c = h.join()
    c.request("share_revoke", {"item_id": IT1})  # unknown id: {} and no record
    assert audit_records(h) == []
    c.request("share_put", body(IT1, 5))
    c.request("share_revoke", {"item_id": IT1})
    c.request("share_revoke", {"item_id": IT1})
    (_p, put), (_r, rev) = audit_records(h)
    assert put["change"] == "put" and put["count"] is None
    assert (rev["change"], rev["item_version"], rev["item_hash"], rev["count"]) == ("revoke", 5, None, None)


def test_audit_failure_at_unbind_keeps_items_and_does_not_skip_the_other_steps(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body())
    good = h.svc._on_change

    def boom(_ev):
        raise RuntimeError("audit down")

    h.svc._on_change = boom
    assert c.request("binding_put", {"object": G.unbound(seq=1)}) == {}  # the unbind itself still completes
    assert h.registry.binding_state(G.WS_A)["status"] != "bound"
    assert h.share_store.usage(G.WS_A)[0] == 1  # audit before delete: the items stay
    assert h.svc.list_shared(OWNER, G.WS_A) == []
    h.svc.reconcile()  # still failing: not fatal
    assert h.share_store.usage(G.WS_A)[0] == 1
    h.svc._on_change = good
    h.svc.reconcile()  # the retry purges them, with a record
    assert h.share_store.usage(G.WS_A)[0] == 0
    assert audit_records(h)[-1][1]["change"] == "purge"


def test_failed_purge_is_retried_before_a_rebind_can_resurrect_items(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body())
    good = h.svc._on_change
    h.svc._on_change = lambda _ev: (_ for _ in ()).throw(RuntimeError("down"))
    c.request("binding_put", {"object": G.unbound(seq=1)})
    h.svc._on_change = good
    c.request("binding_put", {"object": G.binding(seq=2, mode="selective")})  # sync retries the purge first
    assert h.share_store.usage(G.WS_A)[0] == 0 and h.svc.list_shared(OWNER, G.WS_A) == []


def test_rebind_before_the_retry_does_not_resurrect_old_items(shub, tmp_path):
    h = shub(store_path=str(tmp_path / "shares.db"))
    c = h.join()
    c.request("share_put", body())
    good = h.svc._on_change
    h.svc._on_change = lambda _ev: (_ for _ in ()).throw(RuntimeError("down"))
    c.request("binding_put", {"object": G.unbound(seq=1)})
    assert h.share_store.pending() == {G.WS_A}
    c.request("binding_put", {"object": G.binding(seq=2, mode="selective")})  # rebound, sink still down
    assert h.svc.list_shared(OWNER, G.WS_A) == []  # the old binding's item is not served
    refused(c, "share_put", body(IT2, 1), "not_bound")
    # durable: a new service on the same store (a restart) still knows
    assert h.share_store.pending() == {G.WS_A}
    h.svc._on_change = good
    h.svc.reconcile()
    assert h.share_store.pending() == set() and h.share_store.usage(G.WS_A)[0] == 0
    assert h.svc.list_shared(OWNER, G.WS_A) == []
    assert c.request("share_put", body(IT2, 1)) == {}


def test_reconcile_after_restart_drops_orphans(shub, tmp_path):
    h = shub(store_path=str(tmp_path / "shares.db"))
    c = h.join()
    c.request("share_put", body())
    # simulate a crash between the unbind and the purge: flip the registry only
    h.registry.set_binding_state(G.WS_A, {"binding_seq": 1, "status": "unbound", "binding": None})
    assert h.share_store.usage(G.WS_A)[0] == 1
    h.svc.reconcile()
    assert h.share_store.usage(G.WS_A) == (0, 0)


def test_malformed_bodies(shub):
    h = shub()
    c = h.join()
    for b in ({"item_id": "nothex", "sealed": body()["sealed"]}, {"item_id": IT1}, {"item_id": IT1, "sealed": "!!"}):
        c.send_raw(c.build("share_put", b))
        with pytest.raises(LinkError) as e:
            c.recv_reply()
        assert e.value.code == "malformed"
        c = h.open()  # the link was dropped


def test_workspaces_are_separate(shub):
    h = shub()
    a = h.join()
    b = h.open(G.WS_B, "wsk_b", first=True)
    b.request("join", h.join_body("invB", G.WS_B, wsk="wsk_b"))
    a.request("share_put", body(IT1, 1))
    # B's item has the same id: signed by B's key and bound to B
    ib = V4.share_item(1, wsk="wsk_b", workspace_id=G.WS_B, item_id=IT1)
    b.request("share_put", V4.share_body(ib))
    assert h.share_store.get(G.WS_A, IT1)[0] != h.share_store.get(G.WS_B, IT1)[0]
    b.request("share_revoke", {"item_id": IT1})
    assert h.share_store.get(G.WS_A, IT1) is not None
    refused(a, "share_put", V4.share_body(ib), "bad_wrap")  # B's item replayed on A's link


def test_hub_stores_exactly_what_it_received(shub, tmp_path):
    p = tmp_path / "shares.db"
    h = shub(store_path=str(p))
    c = h.join()
    b = body(title="PLAINTEXT-MARKER-0123456789")
    c.request("share_put", b)
    assert h.share_store.get(G.WS_A, IT1)[0] == unb64u(b["sealed"])  # byte for byte
    assert h.share_store.items(G.WS_A)[0].keys() == {"item_id", "sealed", "digest", "size", "stored_ms", "item_version"}
    assert (p.stat().st_mode & 0o077) == 0  # private db
    h.share_store.close()
    assert b"PLAINTEXT-MARKER" not in p.read_bytes()  # sealed at rest: the title is nowhere in the file


def test_open_shared_item_verifies_signature_and_content():
    b = body(title="Open me")
    r = open_shared_item(
        unb64u(b["sealed"]),
        hub_id=G.HUB_ID,
        workspace_id=G.WS_A,
        item_id=IT1,
        hxk=G.K.priv["hxk"],
        wsk_pub=G.K.pub["wsk_a"],
        dev_mode=True,
    )
    assert r["item"]["content"]["title"] == "Open me" and r["hash"].startswith("sha256:")
    kw = {"hub_id": G.HUB_ID, "workspace_id": G.WS_A, "item_id": IT1, "hxk": G.K.priv["hxk"], "dev_mode": True}
    with pytest.raises(Refused) as e:
        open_shared_item(unb64u(b["sealed"]), wsk_pub=G.K.pub["wsk_b"], **kw)
    assert e.value.code == "bad_signature"
    with pytest.raises(Refused) as e:
        open_shared_item(unb64u(b["sealed"])[:-1] + b"\x00", wsk_pub=G.K.pub["wsk_a"], **kw)
    assert e.value.code == "bad_wrap"

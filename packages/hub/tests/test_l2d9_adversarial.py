# ruff: noqa: F811  (the `shub` fixture is imported then named as an argument)
"""L2 security review of D9 (orch-hub#23 @ 40ed27e), l2-d9. The ``test_d9_<n>_*`` cases name findings in
state/l2-review-p4.md "D9 hub#23"; the ``test_d9_ok_*`` cases pin what was checked and holds.

Cases marked FAILS-ON-HEAD assert the behaviour the finding asks for; they fail on 40ed27e."""

from __future__ import annotations

from shares_helpers import IT1, IT2, OWNER, audit_records, body, refused, shub  # noqa: F401

from orch_hub.contract import vectorgen as G


def _down(_ev):
    raise RuntimeError("audit down")


# --- what holds -----------------------------------------------------------------------------------------------------


def test_d9_ok_nothing_of_the_old_binding_leaks_across_a_rebind(shub):
    """Unbind with the sink down, rebind with the sink still down: the old item is never served, no put or revoke is
    taken; after the retry the old item is a tombstone, so a replay of the old put body is ``stale_version``."""
    h = shub()
    c = h.join()
    old = body(IT1, 1, title="old binding")
    c.request("share_put", old)
    good = h.svc._on_change
    h.svc._on_change = _down
    assert c.request("binding_put", {"object": G.unbound(seq=1)}) == {}
    assert c.request("binding_put", {"object": G.binding(seq=2, mode="selective")}) == {}
    assert h.registry.binding_state(G.WS_A)["status"] == "bound"
    assert h.svc.list_shared(OWNER, G.WS_A) == []
    refused(c, "share_put", body(IT2, 1), "not_bound")
    refused(c, "share_revoke", {"item_id": IT1}, "not_bound")
    h.svc._on_change = good
    h.svc.reconcile()
    assert h.share_store.pending() == set()
    assert h.svc.list_shared(OWNER, G.WS_A) == []
    refused(c, "share_put", old, "stale_version")  # the old binding's bytes never come back
    assert [e["change"] for _r, e in audit_records(h)] == ["put", "purge"]


def test_d9_ok_list_is_empty_while_pending_even_for_the_owner(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body())
    h.svc._on_change = _down
    c.request("binding_put", {"object": G.binding(seq=2, mode="full")})  # switch away from selective, purge fails
    assert h.share_store.usage(G.WS_A)[0] == 1 and h.share_store.pending() == {G.WS_A}
    assert h.svc.list_shared(OWNER, G.WS_A) == []


# --- D9-1 (P2): a pending purge heals only at a binding_put/join of that workspace or a restart ---------------------


def test_d9_1_rebound_workspace_recovers_once_the_sink_is_back(shub):
    """FAILS-ON-HEAD. Rebound selective with the sink down, then the sink comes back: the workspace stays refused
    (``not_bound`` on a bound workspace) until a restart or a re-sent binding_put, which the host has no reason to send.
    Wanted: put/revoke/list retry the pending purge under the lock before refusing."""
    h = shub()
    c = h.join()
    c.request("share_put", body())
    good = h.svc._on_change
    h.svc._on_change = _down
    c.request("binding_put", {"object": G.unbound(seq=1)})
    c.request("binding_put", {"object": G.binding(seq=2, mode="selective")})
    h.svc._on_change = good  # the audit sink is healthy again; no restart
    assert c.request("share_put", body(IT2, 1)) == {}


def test_d9_1_unbound_items_do_not_stay_on_disk_while_the_hub_runs(shub):
    """FAILS-ON-HEAD. An unbound workspace whose purge failed keeps its sealed items in shares.db until the hub
    restarts (or that workspace sends another binding_put); a healthy sink alone never purges them."""
    h = shub()
    c = h.join()
    c.request("share_put", body())
    good = h.svc._on_change
    h.svc._on_change = _down
    c.request("binding_put", {"object": G.unbound(seq=1)})
    h.svc._on_change = good  # healthy again; the hub keeps running, the unbound workspace sends nothing more
    refused(c, "share_put", body(IT2, 1), "not_bound")  # its link stays up; nothing retries the purge
    assert h.share_store.usage(G.WS_A)[0] == 0


# --- D9-2 (P2): ``sync`` says it never raises, but a failing mark_pending skips the other unbind steps ---------------


def test_d9_2_unbind_completes_even_when_mark_pending_fails(shub, monkeypatch):
    """FAILS-ON-HEAD. Sink down AND shares.db refusing writes (disk full): ``mark_pending`` raises out of ``sync``, so
    ``binding_put`` answers ``internal`` after the registry already committed the unbind, and ``downstream.binding``
    and ``spaces.on_unbind`` are skipped (the point of fix 2e1f562)."""
    h = shub()
    c = h.join()
    c.request("share_put", body())
    h.svc._on_change = _down

    def full(_ws):
        raise OSError("disk full")

    monkeypatch.setattr(h.share_store, "mark_pending", full)
    assert c.request("binding_put", {"object": G.unbound(seq=1)}) == {}

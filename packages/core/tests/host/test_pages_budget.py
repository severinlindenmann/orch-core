"""H6b: the byte budget and fairness of the page snapshots (pages.py)."""

from __future__ import annotations

import pytest

from orch.host import pages as pages_mod
from orch.host.errors import HostError


def test_a_principal_over_its_byte_share_loses_only_its_own_oldest(monkeypatch):
    monkeypatch.setattr(pages_mod, "PRINCIPAL_BYTES", 1000)
    monkeypatch.setattr(pages_mod, "MAX_BYTES", 10_000)
    pages = pages_mod.Pages("ws_x")
    victim = pages.open("ticket.query", "uid:victim", "e", None, {"x": "v" * 400})
    mine = [pages.open("ticket.query", "uid:flood", "e", None, {"x": "f" * 400}) for _ in range(6)]
    assert victim.id in pages._snaps
    assert [s.id in pages._snaps for s in mine] == [False] * 4 + [True] * 2  # its own oldest went first


def test_the_workspace_budget_evicts_the_principal_holding_the_most(monkeypatch):
    monkeypatch.setattr(pages_mod, "PRINCIPAL_BYTES", 5000)
    monkeypatch.setattr(pages_mod, "MAX_BYTES", 1700)
    pages = pages_mod.Pages("ws_x")
    small = pages.open("ticket.query", "uid:small", "e", None, {"x": "s" * 100})
    big = [pages.open("ticket.query", "uid:big", "e", None, {"x": "b" * 400}) for _ in range(4)]
    pages.open("ticket.query", "uid:small", "e", None, {"x": "s" * 100})
    assert small.id in pages._snaps
    assert big[0].id not in pages._snaps


def test_a_snapshot_records_its_size():
    pages = pages_mod.Pages("ws_x")
    snap = pages.open("ticket.query", "uid:1", "e", None, {"a": [1, 2, 3]})
    assert snap.size == len('{"a":[1,2,3]}')


def test_scan_finding_a_flood_of_principals_never_evicts_a_principal_within_its_share(monkeypatch):
    """Scan finding (cross-principal eviction): at the workspace count cap, many principals each opening snapshots
    evict from whoever holds the largest share, never from a principal holding one small snapshot."""
    monkeypatch.setattr(pages_mod, "MAX_SNAPSHOTS", 8)
    monkeypatch.setattr(pages_mod, "PER_PRINCIPAL", 4)
    pages = pages_mod.Pages("ws_x")
    victim = pages.open("ticket.query", "uid:victim", "e", None, {"x": 1})
    for n in range(40):
        for _ in range(4):
            pages.open("ticket.query", f"uid:flood{n % 3}", "e", None, {"x": n})
    assert victim.id in pages._snaps
    assert len(pages._snaps) <= 8
    counts: dict[str, int] = {}
    for v in pages._snaps.values():
        counts[v.principal] = counts.get(v.principal, 0) + 1
    assert all(c <= 4 for c in counts.values())


def test_scan_finding_one_oversized_snapshot_evicts_nobody_else(monkeypatch):
    monkeypatch.setattr(pages_mod, "PRINCIPAL_BYTES", 100)
    monkeypatch.setattr(pages_mod, "MAX_BYTES", 300)
    pages = pages_mod.Pages("ws_x")
    others = [pages.open("ticket.query", f"uid:{i}", "e", None, {"x": "o" * 50}) for i in range(3)]
    with pytest.raises(HostError) as exc:  # larger than one principal's share: refused, never stored
        pages.open("ticket.query", "uid:huge", "e", None, {"x": "h" * 1000})
    assert exc.value.code == "quota.host"
    assert all(s.id in pages._snaps for s in others)


# --- scan finding (resource-cap bypass): one choke point, every scope ---------------------------------------------


def test_scan_finding_every_paged_reader_stores_its_snapshot_through_the_one_store(tmp_path, monkeypatch):
    from tests.host.live_helpers import ok, world

    w = world(tmp_path)
    try:
        for n in range(3):
            w.ticket(f"t{n}")
        w.person_event("settings.changed", set={"repos": {"a": {"path": "/r/a"}, "b": {"path": "/r/b"}}})
        seen: list[str] = []
        admit = pages_mod.STORE.admit

        def spy(snap, now):
            seen.append(snap.op)
            return admit(snap, now)

        monkeypatch.setattr(pages_mod.STORE, "admit", spy)
        c = w.client()
        for op in ("ticket.query", "activity.list", "workspace.settings"):
            first = ok(c.req(op, {"limit": 1}))["data"]
            assert first["next_page"] is not None, op
            assert ok(c.req(op, {"limit": 1, "page": first["next_page"]}))["data"] is not None
        assert seen == ["ticket.query", "activity.list", "workspace.settings"]  # each first page, once
        held = {v.op for v in pages_mod.STORE.snaps.values()}
        assert held == {"ticket.query", "activity.list", "workspace.settings"}
    finally:
        w.close()
        w.lab.close()


def test_scan_finding_a_uids_grants_share_one_cap(monkeypatch):
    """Varying principals (several grants of one UID) does not widen the UID's share."""
    monkeypatch.setattr(pages_mod, "PER_UID", 4)
    store = pages_mod.STORE
    pages = pages_mod.Pages("ws_x")
    for g in range(10):
        pages.open("ticket.query", f"g:gr_{g}@501", "e", None, {"x": g})
    assert sum(1 for v in store.snaps.values() if pages_mod.uid_of(v.principal) == "501") <= 4


def test_scan_finding_caps_hold_across_workspaces_and_host_wide(monkeypatch):
    """One principal opening snapshots in many workspaces stays inside its principal cap (the store is host-wide);
    the host cap evicts the heaviest UID, never a UID holding one snapshot."""
    monkeypatch.setattr(pages_mod, "PER_PRINCIPAL", 3)
    monkeypatch.setattr(pages_mod, "HOST_SNAPSHOTS", 6)
    victim = pages_mod.Pages("ws_v").open("ticket.query", "uid:7", "e", None, {"x": 0})
    for n in range(20):
        pages_mod.Pages(f"ws_{n}").open("ticket.query", "uid:501", "e", None, {"x": n})
    for n in range(20):
        pages_mod.Pages(f"ws_{n}").open("ticket.query", "uid:502", "e", None, {"x": n})
    snaps = pages_mod.STORE.snaps.values()
    assert sum(v.principal == "uid:501" for v in snaps) <= 3
    assert len(pages_mod.STORE.snaps) <= 6
    assert victim.id in pages_mod.STORE.snaps


def test_scan_finding_another_workspaces_token_never_resumes_here():
    a, b = pages_mod.Pages("ws_a"), pages_mod.Pages("ws_b", key=None)
    snap = a.open("ticket.query", "uid:1", "e", None, {"x": 1})
    token = a.token(snap, 1, {})
    with pytest.raises(HostError) as exc:
        b.resume("ticket.query", token, "uid:1", "e", None, {})
    assert exc.value.code == "page.expired"


def test_scan_finding_ties_go_to_the_most_recent_opening_the_victim_keeps_its_share():
    """Review repro (finding 4): uid:501 holds 10; six grants on the same UID each open 10. Every holder ties at 10,
    and the tie-break evicts the holder that opened last, so the earlier victim keeps all of its snapshots."""
    store = pages_mod.SnapshotStore()
    pages = pages_mod.Pages("ws_x", store=store)
    victim = [pages.open("ticket.query", "uid:501", "e", "p_v", {"x": 1}) for _ in range(10)]
    for g in range(6):
        for _ in range(10):
            pages.open("ticket.query", f"g:gr{g}@501", "e", "p_a", {"x": 1})
    assert sum(s.id in store.snaps for s in victim) == 10
    assert sum(1 for v in store.snaps.values() if pages_mod.uid_of(v.principal) == "501") <= pages_mod.PER_UID


def test_the_snapshot_being_opened_is_not_its_own_victim_while_another_can_go(monkeypatch):
    monkeypatch.setattr(pages_mod, "PER_UID", 2)
    store = pages_mod.SnapshotStore()
    pages = pages_mod.Pages("ws_x", store=store)
    first = pages.open("ticket.query", "g:a@7", "e", None, {"x": 1})
    second = pages.open("ticket.query", "g:b@7", "e", None, {"x": 1})
    third = pages.open("ticket.query", "g:c@7", "e", None, {"x": 1})  # all tie at one: the latest earlier opener goes
    assert third.id in store.snaps and first.id in store.snaps and second.id not in store.snaps

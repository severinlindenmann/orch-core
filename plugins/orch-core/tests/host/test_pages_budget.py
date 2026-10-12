"""H6b: the byte budget and fairness of the page snapshots (pages.py)."""

from __future__ import annotations

from orch.host import pages as pages_mod


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

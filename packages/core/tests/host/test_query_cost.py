"""#1162 step 1: ``ticket.query`` costs O(tickets) filesystem checks, not O(rows x tickets). One request takes the store
lock once (one freshness check) and builds a full row only for the rows of the page it serves."""

from __future__ import annotations

import pytest

from orch.host import reads_query
from orch.store.rootfs import RootFS
from orch.store.store import Store
from tests.host.live_helpers import World, world
from tests.host.test_reads_query import agent, close_ticket, keys, mk, query

LIMIT = 5
BOTH = {"status": ["open", "closed"], "limit": LIMIT, "sort": "key", "order": "asc"}


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


@pytest.fixture
def counts(monkeypatch):
    calls = {"leaf_stats": 0, "events": 0}
    leaf, events = RootFS.leaf_stats, Store.events

    def count_leaf(self, *a, **kw):
        calls["leaf_stats"] += 1
        return leaf(self, *a, **kw)

    def count_events(self, *a, **kw):
        calls["events"] += 1
        return events(self, *a, **kw)

    monkeypatch.setattr(RootFS, "leaf_stats", count_leaf)
    monkeypatch.setattr(Store, "events", count_events)
    return calls


def grow(w: World, n: int, made: list[tuple[str, str]]) -> None:
    """``n`` more tickets: every 4th claimed (own session each), every 5th closed."""
    for _ in range(n):
        i = len(made)
        uid, key = mk(w, f"ticket {i}", priority=("high", "low", "medium")[i % 3])
        made.append((uid, key))
        if i % 4 == 0:
            agent(w, "claim", {"ref": key}, session=f"s_01J9ZP{i:020d}")
        elif i % 5 == 0:
            close_ticket(w, uid)


def client(w: World):
    if not hasattr(w, "_qc"):
        w._qc = w.client()
    return w._qc


def measure(w: World, counts: dict[str, int], args: dict) -> tuple[dict, dict[str, int]]:
    before = dict(counts)
    data = query(w, args, client=client(w))
    return data, {k: counts[k] - before[k] for k in counts}


def test_query_cost_does_not_grow_with_the_number_of_tickets(w, counts):
    made: list[tuple[str, str]] = []
    grow(w, 12, made)
    query(w, BOTH, client=client(w))  # warm: every log loaded
    first12, c12 = measure(w, counts, BOTH)
    grow(w, 12, made)
    query(w, BOTH, client=client(w))
    first24, c24 = measure(w, counts, BOTH)
    print(f"\nW10 first page: 12 tickets {c12}, 24 tickets {c24}")
    assert first12["total"] < first24["total"] and len(first24["rows"]) == LIMIT
    assert c24["leaf_stats"] == c12["leaf_stats"]  # a constant number of freshness checks per request
    assert c24["leaf_stats"] <= 10
    served = sum(1 for r in first24["rows"] if r["claim"] or r["status"] == "closed")
    assert served >= 1 and c24["events"] == served <= LIMIT  # events only for the rows of the page
    later, cl = measure(w, counts, {**BOTH, "page": first24["next_page"]})
    print(f"W10 later page: {cl}")
    assert len(later["rows"]) == LIMIT and cl["events"] <= LIMIT and cl["leaf_stats"] <= c24["leaf_stats"]


def test_the_pages_equal_the_full_list_in_every_order_and_filter(w):
    made: list[tuple[str, str]] = []
    grow(w, 12, made)
    _, epic = mk(w, "the epic", type="epic")
    mk(w, "zebracorn child", parent=epic)
    mk(w, "plain child", parent=epic)
    cases = [{"sort": s, "order": o} for s in ("updated", "priority", "key") for o in ("asc", "desc")]
    cases += [
        {"q": "zebracorn"},
        {"q": "ticket 1"},
        {"needs_me": True},
        {"epic": epic},
        {"parent": epic},
        {"sort": "priority", "order": "asc", "epic": epic},
    ]
    for extra in cases:
        base = {"status": ["open", "closed", "in_progress"], **extra}
        full = query(w, {**base, "limit": 200}, client=client(w))
        assert full["next_page"] is None and full["total"] == len(full["rows"])
        rank = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
        if extra.get("sort") == "priority":
            ranks = [rank.get(r["priority"], 4) for r in full["rows"]]
            assert ranks == sorted(ranks, reverse=extra.get("order") == "desc")
        if extra.get("sort") == "key":
            order = [reads_query._key_order(k) for k in keys(full)]
            assert order == sorted(order, reverse=extra.get("order") == "desc")
        walked, page, rows = None, None, []
        while True:
            data = query(w, {**base, "limit": 7, **({"page": page} if page else {})}, client=client(w))
            walked = walked or data
            assert data["total"] == full["total"], extra
            rows += data["rows"]
            page = data["next_page"]
            if not page:
                break
        assert rows == full["rows"], extra
        assert walked["rows"] == full["rows"][:7]


def test_a_foreign_append_between_two_queries_is_seen(w):
    _, key = mk(w, "watched")
    first = query(w, client=client(w))["rows"][0]
    store = w.lab.ws.other()
    w.lab.ws.store = store
    try:
        w.lab.ws.tick()
        store.ticket(store.uid_of(key))
        ev = w.lab.ws.log(store.uid_of(key), "from another store").event
    finally:
        store.close()
        w.lab.ws.store = None
    second = query(w, client=client(w))["rows"][0]
    assert second["head_seq"] == ev["seq"] == first["head_seq"] + 1

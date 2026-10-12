"""H6b security re-review (Opus) of 34c4fd13: the activity.list base cache after a journal rebuild."""

from __future__ import annotations

import pytest

from tests.host.live_helpers import ok as ok_
from tests.host.live_helpers import world


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


def feed(w):
    data = ok_(w.client().req("activity.list", {"order": "asc", "limit": 200}))["data"]
    return [(i["id"], i["log"], i["seq"], i["type"]) for i in data["items"]]


def test_the_base_cache_follows_a_rebuild_that_moves_events(w):
    """H6a allows a rebuild to find another event at a jpos (a foreign event sorting before, see
    test_live_updates::test_a_cursor_whose_entry_moved_after_a_rebuild_is_sub_reset). The base cache is keyed by the
    journal epoch and its length only, so after such a rebuild it must not keep serving the old order."""
    uid_a, _ = w.ticket("A")
    _, key_b = w.ticket("B")
    w.write(key_b, "b1")
    feed(w)  # fills the cache
    store = w.service.store
    orig = store.merged_events

    def moved():  # the rebuild finds a foreign event of A sorting before B's last one
        out = orig()
        out[-1], out[-2] = out[-2], out[-1]
        return out

    w.lab.ws.tick()
    w.append(lambda s: (s.ticket(uid_a), w.lab.ws.log(uid_a, "foreign")))
    store.merged_events = moved
    try:
        w.check_journals()  # the rebuild (same epoch: no restore)
        cached = feed(w)
        w.service.activity_base = None
        fresh = feed(w)
    finally:
        store.merged_events = orig
    assert cached == fresh, f"stale base cache after a rebuild:\n{cached[-3:]}\n{fresh[-3:]}"


def test_a_second_activity_list_over_a_warm_cache_answers(w):
    _, key = w.ticket("A")
    w.write(key, "one")
    first = feed(w)
    w.write(key, "two")
    second = feed(w)  # the cache is warm: its integrity check runs
    assert second[: len(first)] == first and len(second) == len(first) + 1


def test_the_workspace_byte_cap_holds_against_oversize_snapshots_of_several_principals(monkeypatch):
    from orch.host import pages as pages_mod

    monkeypatch.setattr(pages_mod, "MAX_BYTES", 10_000)
    monkeypatch.setattr(pages_mod, "PRINCIPAL_BYTES", 2_000)
    pages = pages_mod.Pages("ws_x")
    from orch.host.errors import HostError

    for n in range(10):  # each principal opens one snapshot larger than its share: refused, never stored
        try:
            pages.open("ticket.query", f"uid:{n}", "e", None, {"rows": "x" * 6_000})
        except HostError as exc:
            assert exc.code == "quota.host"
    held = sum(s.size for s in pages._snaps.values())
    assert held <= pages_mod.MAX_BYTES + 6_100, f"{held} bytes held against a {pages_mod.MAX_BYTES} byte cap"

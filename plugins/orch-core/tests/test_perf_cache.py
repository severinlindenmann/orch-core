"""Parsed-ticket cache and the per-request scope (one scan, one link index per page)."""
import os
import threading

import pytest

from orch.core import store
from orch.core.model import parse_ticket


@pytest.fixture
def parses(monkeypatch):
    """Count real YAML parses done through the store's cache."""
    calls = []
    real = store.parse_ticket

    def counting(text, source="<string>"):
        calls.append(source)
        return real(text, source)

    monkeypatch.setattr(store, "parse_ticket", counting)
    monkeypatch.setattr(store, "UNSTABLE_NS", 0)  # the files here were all written just now
    store.clear_parsed()
    return calls


def _path(ws, tid):
    return store.resolve(ws, tid).path


def _bump(path, text):
    """Rewrite in place (same inode) with a different size, so the cache key changes on every file system."""
    st = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_read_ticket_parses_once_and_matches_parse_ticket(ws, put, parses):
    tid = put("open", title="Cached")
    path = _path(ws, tid)
    want = parse_ticket(path.read_text(encoding="utf-8"))
    first = store.read_ticket(path)
    second = store.read_ticket(path)
    assert len(parses) == 1
    assert first.meta == want.meta and first.sections == want.sections and first.preamble == want.preamble
    assert second.meta == first.meta


def test_read_ticket_hands_out_independent_copies(ws, put, parses):
    tid = put("open", labels=["a"])
    path = _path(ws, tid)
    t = store.read_ticket(path)
    t.meta["labels"].append("mutated")
    t.meta["status"] = "done"
    t.set_section("Ask", "changed")
    again = store.read_ticket(path)
    assert again.meta["labels"] == ["a"] and again.status == "open" and again.section("Ask") == ""


def test_edit_reparses_only_that_ticket(ws, put, parses):
    a, b = put("open", title="A"), put("open", title="B")
    pa, pb = _path(ws, a), _path(ws, b)
    store.read_ticket(pa), store.read_ticket(pb)
    assert len(parses) == 2
    _bump(pa, pa.read_text(encoding="utf-8").replace("title: A", "title: A changed"))
    assert store.read_ticket(pa).title == "A changed"
    assert store.read_ticket(pb).title == "B"
    assert len(parses) == 3


def test_rename_and_delete_are_seen(ws, put, parses):
    tid = put("open", title="Moves")
    old = _path(ws, tid)
    t = store.read_ticket(old)
    t.meta["status"] = "backlog"
    path = store.save(ws, t, old)  # another folder: a new path, the old file is gone
    assert path != old and not old.exists()
    with pytest.raises(FileNotFoundError):
        store.read_ticket(old)
    assert store.read_ticket(path).status == "backlog"
    path.unlink()
    with pytest.raises(FileNotFoundError):
        store.read_ticket(path)


def test_save_is_seen_by_the_next_read(ws, put):
    tid = put("open", title="Before")
    path, t = store.load(ws, tid)
    t.meta["title"] = "After"
    store.save(ws, t, path)
    assert store.load(ws, tid)[1].title == "After"


def test_parse_errors_are_not_cached(ws, put, parses):
    from orch.errors import TicketParseError
    tid = put("open")
    path = _path(ws, tid)
    good = path.read_text(encoding="utf-8")
    _bump(path, "---\nid: [broken\n---\n")
    with pytest.raises(TicketParseError):
        store.read_ticket(path)
    _bump(path, good)
    assert store.read_ticket(path).id == tid


def test_read_ticket_is_thread_safe(ws, put):
    ids = [put("open", title=f"T{i}") for i in range(12)]
    paths = [_path(ws, i) for i in ids]
    store.clear_parsed()
    errors = []

    def worker():
        try:
            for _ in range(20):
                for tid, p in zip(ids, paths):
                    assert store.read_ticket(p).id == tid
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_cache_is_bounded(ws, put, monkeypatch):
    monkeypatch.setattr(store, "PARSED_MAX", 3)
    monkeypatch.setattr(store, "UNSTABLE_NS", 0)
    store.clear_parsed()
    for tid in [put("open") for _ in range(5)]:
        store.read_ticket(_path(ws, tid))
    assert len(store._PARSED) == 3


# -- request scope ------------------------------------------------------------------------------------------------

@pytest.fixture
def scans(monkeypatch):
    calls = []
    real = store._scan

    def counting(ws):
        calls.append(1)
        return real(ws)

    monkeypatch.setattr(store, "_scan", counting)
    return calls


def test_scan_without_a_scope_always_rescans(ws, put, scans):
    put("open")
    store.scan(ws), store.scan(ws)
    assert len(scans) == 2


def test_scope_shares_one_scan_and_save_drops_it(ws, put, scans):
    tid = put("open", title="One")
    with store.request_scope():
        first = store.scan(ws)
        second = store.scan(ws)
        assert len(scans) == 1 and [e.id for e in first] == [e.id for e in second]
        second.append("junk")  # a caller's list is its own
        assert len(store.scan(ws)) == len(first)
        path, t = store.load(ws, tid)
        t.meta["title"] = "Two"
        store.save(ws, t, path)  # a write inside the scope: the next scan sees it
        assert [e.meta["title"] for e in store.scan(ws)] == ["Two"]
        assert len(scans) == 2
    store.scan(ws)
    assert len(scans) == 3


def test_scope_memo_is_per_workspace_and_name(ws):
    with store.request_scope():
        assert store.memo(ws, "x", lambda: [1]) is store.memo(ws, "x", lambda: [2])
        assert store.memo(ws, "y", lambda: [3]) == [3]
    assert store.memo(ws, "x", lambda: [4]) == [4]


def test_shared_link_index_is_built_once_per_scope(ws, put, monkeypatch):
    from orch.core import links
    put("open")
    built = []
    real = links.LinkIndex.__init__

    def counting(self, *a, **k):
        built.append(1)
        real(self, *a, **k)

    monkeypatch.setattr(links.LinkIndex, "__init__", counting)
    with store.request_scope():
        assert links.shared_index(ws) is links.shared_index(ws)
    links.shared_index(ws)
    assert len(built) == 2


# -- per request (regression guard: counts, not timings) -----------------------------------------------------------

@pytest.fixture
def link_builds(monkeypatch):
    from orch.core import links
    built = []
    real = links.LinkIndex.__init__

    def counting(self, *a, **k):
        built.append(1)
        real(self, *a, **k)

    monkeypatch.setattr(links.LinkIndex, "__init__", counting)
    return built


@pytest.mark.parametrize("url", ["/", "/board", "/board?view=list", "/activity", "/reports", "/workspace", "/t/{tid}"])
def test_every_page_scans_the_tickets_once(dash, ws, put, scans, url):
    pytest.importorskip("fastapi")
    tid = put("in-progress", title="Busy", sections={"Plan": "1. do it"})
    put("open", title="Other", blocked_by=[tid])
    dash.get(url.format(tid=tid))  # warm-up (templates, caches)
    scans.clear()
    assert dash.get(url.format(tid=tid)).status_code == 200
    assert len(scans) == 1


def test_ticket_page_builds_at_most_one_link_index(dash, ws, put, link_builds):
    tid = put("in-progress", title="Busy")
    dash.get(f"/t/{tid}")
    link_builds.clear()
    assert dash.get(f"/t/{tid}").status_code == 200
    assert len(link_builds) <= 1


def test_today_reparses_only_the_changed_ticket(dash, ws, put, parses):
    ids = [put("in-progress", title=f"T{i}", sections={"Current state": "working"}) for i in range(4)]
    dash.get("/")
    parses.clear()
    dash.get("/")
    assert parses == []  # nothing changed: nothing parsed
    path = store.resolve(ws, ids[0]).path
    _bump(path, path.read_text(encoding="utf-8").replace("working", "working harder"))
    dash.get("/")
    assert len(parses) == 1


def test_warm_up_compiles_templates_and_parses_open_tickets(ws, put, parses):
    pytest.importorskip("fastapi")
    from orch.core import query
    from orch.dashboard.app import warm_up
    from orch.dashboard.views import TEMPLATES
    put("in-progress", title="Busy")
    put("open", title="Next")
    store.clear_parsed()
    TEMPLATES.env.cache.clear()
    warm_up(ws)
    assert len(parses) == 2
    assert any(key[1] == "today.html" for key in TEMPLATES.env.cache.keys())
    parses.clear()
    query._NEEDS_CACHE.clear()
    query.needs_you(ws)
    assert parses == []  # the first page finds every open ticket parsed already


def test_parse_stamp_is_memoized_and_unchanged():
    from datetime import datetime, timezone
    from orch import clock
    clock.parse_stamp.cache_clear()
    assert clock.parse_stamp("2026-10-03T08:12Z") == datetime(2026, 10, 3, 8, 12, tzinfo=timezone.utc)
    assert clock.parse_stamp("2026-10-03T08:12:05Z").second == 5
    clock.parse_stamp("2026-10-03T08:12Z")
    assert clock.parse_stamp.cache_info().hits == 1
    with pytest.raises(ValueError):
        clock.parse_stamp("yesterday")


def test_addon_tickets_are_copies_within_a_request(ws, put):
    from orch.addons.api import AddonContext
    put("open", labels=["a"])
    ctx = AddonContext(ws, "demo")
    with store.request_scope():
        mine = ctx.tickets()
        mine[0].meta["labels"].append("leaked")
        mine[0].meta["title"] = "changed"
        mine[0].status = "done"
        again = ctx.tickets()
        core = store.scan(ws)
        assert again[0].meta["labels"] == ["a"] and again[0].status == "open"
        assert core[0].meta["labels"] == ["a"] and core[0].meta["title"] != "changed" and core[0].status == "open"


def test_a_file_written_just_now_is_not_cached(ws, put, parses, monkeypatch):
    monkeypatch.setattr(store, "UNSTABLE_NS", 2 * 10**9)
    tid = put("open")
    path = _path(ws, tid)
    parses.clear()
    store.read_ticket(path), store.read_ticket(path)
    assert len(parses) == 2  # its stamp may still change within the same clock tick


def test_same_size_edit_with_restored_mtime_is_seen(ws, put, parses, monkeypatch):
    import time
    monkeypatch.setattr(store, "UNSTABLE_NS", 0)  # cache even this fresh file
    tid = put("open", title="AAAA")
    path = _path(ws, tid)
    old = time.time_ns() - 3_600 * 10**9
    os.utime(path, ns=(old, old))
    assert store.read_ticket(path).title == "AAAA"
    time.sleep(0.05)  # a later ctime even on a coarse clock
    path.write_text(path.read_text(encoding="utf-8").replace("title: AAAA", "title: BBBB"), encoding="utf-8")
    os.utime(path, ns=(old, old))  # same inode, same size, same mtime: only the ctime tells
    assert store.read_ticket(path).title == "BBBB"

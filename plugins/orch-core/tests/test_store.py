from concurrent.futures import ThreadPoolExecutor

import pytest

import orch.core.fsutil as fsutil
from orch.core import store
from orch.core.ids import next_id, normalize_ref
from orch.core.model import new_ticket
from orch.errors import NotFoundError, TicketParseError, UsageError


@pytest.mark.parametrize("title,slug", [
    ("Übersicht Größe", "uebersicht-groesse"),
    ('a:b/c*?"<>|d', "a-b-c-d"),
    ("🚀🚀", "ticket"),
    ("  Mixed CASE  title ", "mixed-case-title"),
])
def test_slugify(title, slug):
    assert store.slugify(title) == slug


def test_slugify_truncates_on_word_boundary():
    s = store.slugify("word " * 30)
    assert len(s) <= 40 and not s.endswith("-")


def test_next_id_sequence_and_stale_counter(ws):
    assert next_id(ws) == "L-0001"
    assert next_id(ws) == "L-0002"
    (ws.state_dir / "counter.json").unlink()
    (ws.status_dir("done") / "L-0007-old.md").write_text("---\nid: L-0007\ntitle: old\n---\n", encoding="utf-8")
    assert next_id(ws) == "L-0008"


def test_next_id_concurrent_unique(ws):
    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(lambda _: next_id(ws), range(40)))
    assert len(set(ids)) == 40


def test_normalize_ref(ws):
    assert normalize_ref(ws, "42") == "L-0042"
    assert normalize_ref(ws, "l-42") == "L-0042"
    assert normalize_ref(ws, "ABC-9") == "ABC-9"


def _save_new(ws, title="Hello", status="backlog", external=None):
    t = new_ticket(next_id(ws), title, type="feature", priority="normal", size="m", created="2026-09-30T08:00Z")
    t.meta["status"] = status
    if external:
        t.meta["external"] = [{"key": external, "url": None}]
    return t, store.save(ws, t)


def test_save_load_move(ws):
    _, p = _save_new(ws)
    assert p == ws.status_dir("backlog") / "L-0001-hello.md"
    path, loaded = store.load(ws, "1")
    loaded.meta["status"] = "open"
    new_path = store.save(ws, loaded, path)
    assert new_path == ws.status_dir("open") / "L-0001-hello.md"
    assert not path.exists()


def test_resolve_by_external_key_case_insensitive(ws):
    _save_new(ws, external="ABC-123")
    assert store.resolve(ws, "abc-123").id == "L-0001"
    with pytest.raises(NotFoundError):
        store.resolve(ws, "L-0099")


def test_duplicate_id_is_ambiguous(ws):
    _, p = _save_new(ws)
    (p.parent / "L-0001-hello 2.md").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")  # iCloud conflict copy
    with pytest.raises(UsageError, match="ambiguous"):
        store.resolve(ws, "L-0001")


def test_parse_error_load_raises_and_leaves_file(ws):
    p = ws.status_dir("open") / "L-0003-broken.md"
    p.write_text("---\nid: [unclosed\n---\n", encoding="utf-8")
    before = p.read_bytes()
    with pytest.raises(TicketParseError):
        store.load(ws, "L-0003")
    assert p.read_bytes() == before
    entry = store.resolve(ws, "L-0003")
    assert entry.meta is None and entry.error


def test_scan_uses_cache(ws, monkeypatch):
    _save_new(ws)
    store.scan(ws)
    calls = []
    real = store.parse_ticket
    monkeypatch.setattr(store, "parse_ticket", lambda *a, **k: calls.append(1) or real(*a, **k))
    store.scan(ws)
    assert calls == []


def test_atomic_write_failure_keeps_original(ws, monkeypatch):
    t, p = _save_new(ws)
    before = p.read_bytes()

    def boom(*args):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil.os, "replace", boom)
    t.meta["title"] = "changed"
    with pytest.raises(OSError):
        store.save(ws, t, p)
    assert p.read_bytes() == before
    assert [x.name for x in p.parent.iterdir()] == [p.name]

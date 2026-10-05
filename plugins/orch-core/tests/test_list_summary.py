"""The first line of a ticket's Summary in `orch list`, `next` and `search` (--summary; always in --json), read once
per file version through the scan cache, so an agent sees what is there before it opens anything."""
import json

from orch.core import store


def _row(capsys, *argv):
    from orch.cli import run
    capsys.readouterr()
    assert run(list(argv)) == 0
    return capsys.readouterr().out


def test_scan_keeps_the_first_summary_line(ws, put):
    tid = put(sections={"Summary": "\n- Export runs nightly\n- second bullet"})
    assert next(e for e in store.scan(ws) if e.id == tid).summary == "Export runs nightly"


def test_no_summary_is_none_and_a_long_one_is_cut(ws, put):
    bare = put()
    long = put(sections={"Summary": "* " + "x" * 400})
    entries = {e.id: e for e in store.scan(ws)}
    assert entries[bare].summary is None and len(entries[long].summary) == 160


def test_an_old_cache_without_summaries_is_read_again(ws, put):
    tid = put(sections={"Summary": "- First"})
    store.scan(ws)
    store.forget_scope()
    cache = ws.state_dir / "index.json"
    data = json.loads(cache.read_text())
    for v in data.values():
        v.pop("summary", None)
    cache.write_text(json.dumps(data))
    assert next(e for e in store.scan(ws) if e.id == tid).summary == "First"


def test_list_prints_it_only_when_asked(ws, put, capsys):
    put(status="open", sections={"Summary": "- Export runs nightly"})
    assert "    Export runs nightly" in _row(capsys, "list", "--summary")
    assert "Export runs nightly" not in _row(capsys, "list")
    assert "    Export runs nightly" in _row(capsys, "next", "--summary")
    assert "    Export runs nightly" in _row(capsys, "search", "nightly", "--summary")


def test_json_rows_always_carry_it(ws, put, capsys):
    tid = put(status="open", sections={"Summary": "- Export runs nightly"})
    rows = {r["id"]: r for r in json.loads(_row(capsys, "list", "--json"))}
    assert rows[tid]["summary"] == "Export runs nightly"

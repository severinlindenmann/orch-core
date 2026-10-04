"""A2 platform additions (Task 3): repos, trackers, links and snapshots on the addon context and views."""
from datetime import datetime, timezone

from addon_fixtures import loaded
from orch.addons import cache
from orch.addons.api import COMMIT_HOOK_HEADER, AddonContext, Snapshot

WALL = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


def test_repos_harness_first_and_deduped(configure):
    ws = configure(git={"repos": {"acme": {"path": "."}, "ingest": {"path": "ingest", "default_branch": "develop"},
                                  "again": {"path": "./ingest"}}})
    repos = AddonContext(ws, "x").repos()
    assert [(r.name, r.role, r.default_branch) for r in repos] == [("acme", "harness", None), ("ingest", "sub-repo", "develop")]
    assert repos[0].path == ws.root.resolve() and repos[1].path == (ws.root / "ingest").resolve()


def test_the_harness_is_named_harness_when_not_listed(ws):
    assert [(r.name, r.role) for r in AddonContext(ws, "x").repos()] == [("harness", "harness")]


def test_trackers_build_keys_and_urls(configure):
    ws = configure(external_trackers=[
        {"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": "https://github.com/a/b/issues/{id}"},
        {"prefix": "ABC", "pattern": "ABC-\\d+", "url": "https://jira.test/browse/{key}"}])
    gh, abc = AddonContext(ws, "x").provider_context().trackers()
    assert gh.key_for(12) == "GH-12" and abc.key_for(12) == "ABC-12"
    assert gh.url_for("gh-12") == "https://github.com/a/b/issues/12" and abc.url_for("abc-12") == "https://jira.test/browse/ABC-12"
    assert gh.matches("GH-3") and not gh.matches("3")


def test_links_and_snapshots(ws, put):
    tid = put("open", branches={"r": "feature/L-0001-x"})
    ctx = AddonContext(ws, "demo")
    assert [t.id for t in ctx.links().for_review(branch="feature/L-0001-x")] == [tid]
    cache.write_snapshot(ws, "demo", Snapshot("fake", "a", WALL, items=({"id": "x", "label": "X", "role": "ok", "text": "1"},)))
    assert [s.scope for s in ctx.snapshots()] == ["a"]
    assert ctx.provider_context().snapshots("fake")[0].items[0]["id"] == "x" and ctx.snapshots("other") == []


def test_commit_hook_header_is_the_installed_one():
    from orch.hooks.install import HEADER
    assert COMMIT_HOOK_HEADER == HEADER


def test_slot_view_state_dir_repos_trackers_and_links(ws):
    from orch.addons.runtime import SlotView
    la = loaded(ws, object())
    v = SlotView(ws, la, "page.demo", None, {"state": "failing"})
    assert v.params == {"state": "failing"} and v.state_dir == ws.state_dir / "addons" / "demo"
    assert v.repos()[0].role == "harness" and v.trackers() == [] and v.links() is v.links()


def test_fake_workspace_trackers_and_meta(tmp_path):
    from orch.core import store
    from orch.testing import fake_workspace
    tr = {"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": "https://github.com/a/b/issues/{id}"}
    fw = fake_workspace(tmp_path / "w", trackers=[tr], tickets=[{"title": "One", "meta": {"blocked_by": ["DEMO-0002"]}}])
    assert fw.ws.config["external_trackers"] == [tr]
    assert store.load(fw.ws, fw.tickets[0])[1].meta["blocked_by"] == ["DEMO-0002"]


def test_addons_md_documents_the_a2_context():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "ADDONS.md").read_text(encoding="utf-8")
    for needle in ("ctx.repos()", "ctx.trackers()", "ctx.links()", "ctx.snapshots(", "view.params", '"tickets": true',
                   "view.state_dir", "COMMIT_HOOK_HEADER", "addons/github-reviews"):
        assert needle in text, needle


def test_document_gives_the_schema_document_read_only(ws, aops):
    from orch.core.events import read_events
    from orch.core.schema import ticket_schema
    import jsonschema
    tid = aops.new("Doc me", ask="a").id
    aops.ask(tid, [{"text": "Which?", "options": ["A", "B"]}])
    before = len(read_events(ws, tid))
    ctx = AddonContext(ws, "x")
    doc = ctx.document(tid)
    jsonschema.Draft202012Validator(ticket_schema()).validate(doc)
    assert doc["questions"][0]["hash"].startswith("sha256:")
    assert ctx.provider_context().document(tid) == doc
    doc["title"] = "changed"
    assert ctx.document(tid)["title"] == "Doc me"
    assert len(read_events(ws, tid)) == before


def test_ticket_widgets_gives_text_and_documents_read_only(ws, aops):
    from orch.core.events import read_events
    tid = aops.new("Widgets", ask="a").id
    good = '```orch\n{"type": "stats", "title": "Runs", "source": "ci", "items": [{"label": "p50", "value": "6m"}]}\n```'
    bad = '```orch\n{"type": "nope"}\n```'
    aops.set_section(tid, "Findings", f"{good}\n\n{bad}")
    before = len(read_events(ws, tid))
    ctx = AddonContext(ws, "x")
    stats, nope = ctx.ticket_widgets(tid)
    assert (stats["section"], stats["index"], stats["key"], stats["layer"], stats["name"], stats["title"],
            stats["source"]) == ("Findings", 0, "0", "type", "stats", "Runs", "ci")
    assert "6m" in stats["text"] and stats["document"].startswith("<!doctype html>") and len(stats["raw_sha256"]) == 64
    body = stats["document"][stats["document"].index("<body"):]  # the companion draws the chrome around it
    assert "6m" in body and "<figcaption" not in body and "<details" not in body and "Source:" not in body
    assert nope["document"] is None and nope["problems"] and nope["text"].startswith("[")
    assert ctx.provider_context().ticket_widgets(tid)[0]["text"] == stats["text"]
    assert len(read_events(ws, tid)) == before

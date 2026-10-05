from datetime import datetime, timezone
from pathlib import Path

import pytest

from orch.addons.api import Intent, PendingDecision, Snapshot
from orch.addons.manifest import load_manifest
from orch.addons.runtime import SlotView
from orch.addons.widgets import Action, Callout, Card, Link, Search, Table, widget_problems
from orch.errors import ValidationError
from orch.testing import AddonContract, FakeRunner, fake_workspace

ADDON = Path(__file__).resolve().parents[1]
MANIFEST = load_manifest(ADDON)
FIXTURES = Path(__file__).with_name("fixtures")
SPACE = "acme/ticket-orch-demo"
REPO = "acme-energy-data"
NOW = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
FETCH = ["git", "-C", "*", "fetch", "--depth", "200", "--no-tags", "--quiet", "origin"]
RESET = ["git", "-C", "*", "reset", "--hard", "--quiet", "FETCH_HEAD"]
HINT = "Architecture overview documents files this ticket changed — may need an update"
WHY = "1 changed file(s) match: src/acme/ingest/loader.py"
KEY = f"DEMO-0003|{SPACE}|Architecture"
HIDDEN = Intent("none", reason="Hidden on DEMO-0003.")  # A1: resolve()/act() only ever return a none intent


def set_meta(fw, tid, **meta):
    from orch.core import store
    path, t = store.load(fw.ws, tid)
    t.meta.update(meta)
    store.save(fw.ws, t, old_path=path)


def ticket(fw, tid):
    from orch.core import store
    return store.load(fw.ws, tid)[1]


def diff_snapshot(*items):
    return Snapshot("branch-diffs", "tickets", NOW, items=items or ({
        "id": f"DEMO-0003:{REPO}", "label": "DEMO-0003", "role": "info", "text": "2 file(s) changed", "ticket": "DEMO-0003",
        "status": "testing", "repo": REPO, "branch": "feature/DEMO-0003-move-loader",
        "files": ["src/acme/ingest/loader.py", "README.md"]},))


@pytest.fixture
def wiki(tmp_path, clone_with_pages):
    fw = fake_workspace(tmp_path / "w", repos={REPO: {"path": "."}}, tickets=[
        {"title": "Open one", "status": "open"}, {"title": "Old one", "status": "done"},
        {"title": "Move loader", "status": "testing"}])
    set_meta(fw, "DEMO-0003", branches={REPO: "feature/DEMO-0003-move-loader"}, labels=["ingest"])
    fw.enable("wiki", {"provider": "github-wiki", "repos": SPACE})
    addon = fw.load(ADDON, runner=FakeRunner.from_dir(FIXTURES).add(FETCH).add(RESET))
    clone_with_pages(addon.ctx.state_dir)
    pages = next(p for p in addon.obj.providers if p.id == "github-wiki")
    fw.cache("wiki", pages.fetch(addon.ctx.provider_context(), SPACE, None))
    fw.cache("wiki", diff_snapshot())
    return fw, addon


def render(fw, addon, slot, t=None, params=None):
    widgets = addon.obj.widgets(slot, SlotView(fw.ws, addon, slot, t, params))
    for w in widgets:
        assert widget_problems(w, slot=slot, manifest=MANIFEST) == [], w
    return widgets


def card(widgets, title):
    return next(w for w in widgets if isinstance(w, Card) and w.title == title)


def today(fw, addon):
    return addon.obj.decisions(SlotView(fw.ws, addon, "today.from_addons"))


def test_ticket_panel_shows_the_hint_and_related_pages(wiki):
    fw, addon = wiki
    [panel] = render(fw, addon, "ticket.pages", ticket(fw, "DEMO-0003"))
    assert panel.title == "Wiki" and panel.body[0] == Callout("info", HINT, WHY)
    assert panel.body[1] == Action("dismiss", "Dismiss", KEY)
    related = panel.body[2]
    assert [r[0].text for r in related.rows] == ["Architecture overview", "Runbook: nightly load"]
    assert related.rows[0][0] == Link("Architecture overview", "https://github.com/acme/ticket-orch-demo/wiki/Architecture")
    assert related.rows[0][1] == "mentions DEMO-3, ingest ×2"


def test_ticket_panel_looks_mentions_up_without_scanning_page_text(wiki, monkeypatch):
    fw, addon = wiki

    def refuse(page):
        raise AssertionError("page text scanned during a render")
    monkeypatch.setattr(addon.obj, "text_of", refuse)
    [panel] = render(fw, addon, "ticket.pages", ticket(fw, "DEMO-0003"))
    assert [(r[0].text, r[1]) for r in panel.body[2].rows] == [
        ("Architecture overview", "mentions DEMO-3, ingest ×2"), ("Runbook: nightly load", "mentions ingest")]


def test_other_tickets_get_no_panel(wiki):
    fw, addon = wiki
    assert render(fw, addon, "ticket.pages", ticket(fw, "DEMO-0001")) == []


def test_today_decision_and_dismiss(wiki):
    fw, addon = wiki
    [d] = today(fw, addon)
    assert d == PendingDecision(id=f"docs|{KEY}", title="Architecture overview documents files DEMO-0003 changed — may need an update",
                                body=WHY, ticket="DEMO-0003", choices=(("dismiss", "Dismiss"),), role="info")
    assert addon.obj.resolve(d.id, "dismiss", object()) == HIDDEN
    assert today(fw, addon) == []
    [panel] = render(fw, addon, "ticket.pages", ticket(fw, "DEMO-0003"))
    assert isinstance(panel.body[0], Table)  # only the related pages remain


def test_dismiss_action_from_the_ticket(wiki):
    fw, addon = wiki
    assert addon.obj.act("dismiss", KEY, addon.ctx.provider_context()) == HIDDEN
    assert today(fw, addon) == []


@pytest.mark.parametrize("call", [
    lambda o, c: o.act("dismiss", "not|a key", c),
    lambda o, c: o.act("delete", KEY, c),
    lambda o, c: o.resolve(f"docs|{KEY}", "apply", None),
    lambda o, c: o.resolve(f"other|{KEY}", "dismiss", None),
])
def test_bad_dismissals_are_refused(wiki, call):
    fw, addon = wiki
    with pytest.raises(ValidationError):
        call(addon.obj, addon.ctx.provider_context())


def test_hint_disappears_when_the_ticket_leaves_testing(wiki):
    fw, addon = wiki
    set_meta(fw, "DEMO-0003", status="in-progress")
    assert today(fw, addon) == []


def test_wiki_page_sections(wiki):
    fw, addon = wiki
    widgets = render(fw, addon, "page.wiki")
    assert widgets[0] == Search("q", value="", placeholder="title or text")
    assert [w.title for w in widgets if isinstance(w, Card)] == [
        "Docs that may need an update", "Pages linked from open tickets", "Recently changed"]
    docs = card(widgets, "Docs that may need an update").body[0].rows
    assert docs == ((Link("DEMO-0003", "/t/DEMO-0003"), Link("Architecture overview", "https://github.com/acme/ticket-orch-demo/wiki/Architecture"),
                     WHY, Action("dismiss", "Dismiss", KEY)),)
    linked = card(widgets, "Pages linked from open tickets").body[0].rows
    assert [(r[0].text, r[1]) for r in linked] == [("Architecture overview", "DEMO-0003")]
    recent = card(widgets, "Recently changed").body[0].rows
    assert [r[0].text for r in recent] == ["Architecture overview", "Data model", "Runbook: nightly load", "Test", "Home"]
    assert recent[2][2] == "Anna Beispiel"


def test_search_on_the_wiki_page(wiki):
    fw, addon = wiki
    widgets = render(fw, addon, "page.wiki", params={"q": "nightly"})
    assert widgets[0].value == "nightly"
    results = card(widgets, "Results for nightly").body
    assert [w.text for w in results if isinstance(w, Link) and w.text != "Clear search"] == ["Runbook: nightly load"]
    empty = card(render(fw, addon, "page.wiki", params={"q": "zzz"}), "Results for zzz").body
    assert [w.text for w in empty if w.kind == "text"] == ["No page matches this search. Try other or fewer words."]
    assert Link("Clear search", "/addons/wiki/") in empty and Link("Clear search", "/addons/wiki/") in results


def test_no_repo_configured(tmp_path):
    fw = fake_workspace(tmp_path / "w")
    addon = fw.load(ADDON, runner=FakeRunner())
    widgets = render(fw, addon, "page.wiki")
    assert widgets[1] == Callout("info", "No wiki repo set",
                                 "Add owner/name in Workspace & addons, or give the harness repo a GitHub origin.")
    assert card(widgets, "Recently changed").body[0].empty == "No pages cached yet. Press Refresh."


def test_confluence_selected(tmp_path):
    fw = fake_workspace(tmp_path / "w")
    fw.enable("wiki", {"provider": "confluence"})
    widgets = render(fw, fw.load(ADDON, runner=FakeRunner()), "page.wiki")
    assert widgets[1].title == "Confluence is not available yet"


@pytest.mark.parametrize("slot", ["page.wiki", "ticket.pages", "decisions"])
def test_tickets_and_dismissed_are_read_once_per_view(wiki, monkeypatch, slot):
    from orch.core import store
    fw, addon = wiki
    Dismissed = type(addon.obj.dismissed)  # the loaded addon's own module, not the tests' import
    reads = {"scan": 0, "dismissed": 0}
    scan, read = store.scan, Dismissed._read

    def counting_scan(*a, **k):
        reads["scan"] += 1
        return scan(*a, **k)

    def counting_read(self):
        reads["dismissed"] += 1
        return read(self)
    t = ticket(fw, "DEMO-0003")
    monkeypatch.setattr(store, "scan", counting_scan)
    monkeypatch.setattr(Dismissed, "_read", counting_read)
    if slot == "decisions":
        assert today(fw, addon)
    else:
        assert render(fw, addon, slot, t)
    assert reads == {"scan": 1, "dismissed": 1}
    if slot == "page.wiki":  # a new view reads again, so a dismiss or a status change shows on the next render
        render(fw, addon, slot)
        assert reads == {"scan": 2, "dismissed": 2}


def test_wiki_render_survives_odd_cached_items(wiki):
    fw, addon = wiki
    odd = ({"provider": "github-wiki", "space": SPACE, "id": "x" * 600, "title": None, "url": "javascript:1", "path": "x",
            "updated_at": "2026-10-02T12:00:00+00:00", "documents": "src/**", "file_links": [1, None], "links": "DEMO-0003"},
           {"provider": "github-wiki", "space": SPACE, "id": "ok", "title": "T" * 900, "url": "https://example.com/" + "a" * 50,
            "path": "ok.md", "updated_at": "later", "documents": ["src/acme/**"]})
    fw.cache("wiki", Snapshot("github-wiki", "other/space", NOW, items=odd))
    fw.cache("wiki", diff_snapshot(
        {"id": "j", "label": "j", "role": "x", "text": "t", "ticket": "DEMO-0003", "repo": None, "files": "nope"},
        {"id": "k", "label": "k", "role": "info", "text": "t", "ticket": "DEMO-0003", "repo": REPO, "files": ["src/acme/ingest/loader.py", 7]}))
    render(fw, addon, "page.wiki", params={"q": "t"})
    render(fw, addon, "ticket.pages", ticket(fw, "DEMO-0003"))
    decisions = today(fw, addon)
    assert decisions and all(isinstance(d, PendingDecision) and len(d.id) <= 505 for d in decisions)


class TestAddon(AddonContract):
    addon_dir = ADDON
    runner = FakeRunner(strict=False)


def test_wk02_excerpt_is_the_matching_passage_with_the_match_in_bold(wiki):
    fw, addon = wiki
    out = card(render(fw, addon, "page.wiki", params={"q": "nightly"}), "Results for nightly").body
    md = [w.text for w in out if w.kind == "markdown"]
    assert len(md) == 1 and "**nightly**" in md[0] and md[0].startswith("_")


def test_wk02_quotes_and_spaces_around_a_query_are_ignored(wiki):
    fw, addon = wiki
    for q in ('"nightly"', "  nightly  ", "\u201cnightly\u201d"):
        out = card(render(fw, addon, "page.wiki", params={"q": q}), f"Results for {q.strip()}").body
        assert [w.text for w in out if isinstance(w, Link) and w.text != "Clear search"] == ["Runbook: nightly load"]


def test_wk02_snippet_is_centred_on_the_match_and_neutralises_markdown():
    from orch_wiki.render import snippet
    text = "intro " * 40 + "the [retry](http://evil) loop *waits* here " + "tail " * 40
    out = snippet(text, ["retry"])
    assert out.startswith("… ") and out.endswith(" …") and "**retry**" in out
    assert "[" not in out.replace("\\[", "") and "*waits*" not in out  # the page's own markup is escaped
    assert snippet("nothing here", ["zzz"], "First paragraph") == "First paragraph"


def test_wk04_confluence_stub_is_not_offered_in_the_provider_select():
    field = next(f for f in MANIFEST.settings_schema if f.key == "provider")
    assert "confluence" not in field.options and field.default in field.options

import subprocess
from pathlib import Path

import pytest

from orch.addons.manifest import load_manifest
from orch.addons.runner import SubprocessRunner
from orch.addons.runtime import SlotView
from orch.addons.widgets import Action, Callout, Card, Copy, Link, Markdown, Table, widget_problems
from orch.errors import ValidationError
from orch.testing import FakeRunner, ProviderContract, fake_workspace
from orch_wiki import local as local_module
from orch_wiki.local import LocalPages

ADDON = Path(__file__).resolve().parents[1]
MANIFEST = load_manifest(ADDON)
FOLDER = "orchestrator/wiki"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make(tmp_path, *, repo=True, settings=None):
    fw = fake_workspace(tmp_path / "w", tickets=[
        {"title": "Move loader", "status": "testing",
         "sections": {"Summary": "Moves the loader.", "Requirements": "- keep the schema"}},
        {"title": "Other", "status": "open"}])
    if repo:
        git(fw.root, "init", "-q")
        git(fw.root, "config", "user.email", "t@example.com")
        git(fw.root, "config", "user.name", "T")
        git(fw.root, "commit", "-q", "--allow-empty", "-m", "start")
    fw.enable("wiki", {"provider": "local", **(settings or {})})
    addon = fw.load(ADDON, runner=SubprocessRunner(env_names=(), cwd=fw.root))
    return fw, addon


def refresh(fw, addon):
    pctx = addon.ctx.provider_context()
    [scope] = LocalPages().scopes(pctx)
    snap = LocalPages().fetch(pctx, scope, None)
    fw.cache("wiki", snap)
    return snap


def render(fw, addon, slot, t=None, params=None):
    widgets = addon.obj.widgets(slot, SlotView(fw.ws, addon, slot, t, params))
    for w in widgets:
        assert widget_problems(w, slot=slot, manifest=MANIFEST) == [], w
    return widgets


def ticket(fw, tid):
    from orch.core import store
    return store.load(fw.ws, tid)[1]


@pytest.fixture
def wiki(tmp_path):
    fw, addon = make(tmp_path)
    write(fw.root, f"{FOLDER}/Home.md", "# Home\n\nWelcome. See DEMO-0001.\n")
    write(fw.root, f"{FOLDER}/decisions/DEMO-0009.md",
          "---\ntitle: Why we chose X\ndocuments:\n  - src/**\n---\nBody about nightly loads.\n")
    write(fw.root, f"{FOLDER}/notes.txt", "not markdown")
    write(fw.root, f"{FOLDER}/no title here.md", "just text")
    return fw, addon


def test_lists_nested_pages_with_github_wiki_shaped_items(wiki):
    fw, addon = wiki
    snap = refresh(fw, addon)
    assert snap.health == "ok" and snap.complete
    by_id = {i["id"]: i for i in snap.items}
    assert sorted(by_id) == ["Home", "decisions/DEMO-0009", "no title here"]
    home, dec = by_id["Home"], by_id["decisions/DEMO-0009"]
    assert home["title"] == "Home" and home["links"] == ["DEMO-0001"] and home["excerpt"].startswith("Welcome")
    assert home["url"] == "/addons/wiki/?page=Home" and home["space"] == FOLDER and home["provider"] == "local"
    assert dec["title"] == "Why we chose X" and dec["documents"] == ["src/**"]
    assert dec["url"] == "/addons/wiki/?page=decisions%2FDEMO-0009" and dec["path"] == f"{FOLDER}/decisions/DEMO-0009.md"
    assert by_id["no title here"]["title"] == "no title here"
    assert dec["updated_at"].endswith("+00:00")


def test_symlinks_are_skipped(wiki, tmp_path):
    fw, addon = wiki
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# Secret", encoding="utf-8")
    (fw.root / FOLDER / "link.md").symlink_to(outside / "secret.md")
    (fw.root / FOLDER / "linked").symlink_to(outside, target_is_directory=True)
    ids = {i["id"] for i in refresh(fw, addon).items}
    assert "link" not in ids and not any(i.startswith("linked") for i in ids)


@pytest.mark.parametrize("folder", ["/etc", "../outside", "orchestrator/../../x", "~/wiki", "C:/wiki"])
def test_folders_outside_the_workspace_are_refused(tmp_path, folder):
    fw, addon = make(tmp_path, repo=False, settings={"folder": folder})
    pctx = addon.ctx.provider_context()
    assert LocalPages().scopes(pctx) == []
    assert LocalPages().fetch(pctx, folder, None).health == "error"
    [_, callout] = render(fw, addon, "page.wiki")[:2]
    assert callout.title == "Wiki folder not usable"


def test_a_folder_that_is_a_symlink_is_refused(tmp_path):
    fw, addon = make(tmp_path, repo=False, settings={"folder": "linkdir"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (fw.root / "linkdir").symlink_to(outside, target_is_directory=True)
    assert LocalPages().scopes(addon.ctx.provider_context()) == []


def test_count_and_size_caps(tmp_path, monkeypatch):
    fw, addon = make(tmp_path, repo=False)
    for n in range(5):
        write(fw.root, f"{FOLDER}/p{n}.md", f"# P{n}")
    write(fw.root, f"{FOLDER}/big.md", "x" * 2000)
    monkeypatch.setattr(local_module, "MAX_PAGES", 3)
    monkeypatch.setattr(local_module, "MAX_PAGE_BYTES", 1000)
    snap = LocalPages().fetch(addon.ctx.provider_context(), FOLDER, None)
    assert len(snap.items) == 3 and snap.complete is False and "first 3 of 6" in snap.message
    monkeypatch.setattr(local_module, "MAX_PAGES", 500)
    snap = LocalPages().fetch(addon.ctx.provider_context(), FOLDER, None)
    assert len(snap.items) == 5 and "1 page(s)" in snap.message


def test_page_list_search_and_hints_use_the_local_pages(wiki):
    fw, addon = wiki
    refresh(fw, addon)
    widgets = render(fw, addon, "page.wiki")
    [pages] = [w for w in widgets if isinstance(w, Card) and w.title == "Pages"]
    rows = pages.body[0].rows
    assert [(r[0].text, r[1]) for r in rows] == [("Why we chose X", "decisions"), ("Home", None), ("no title here", None)]
    assert rows[0][0] == Link("Why we chose X", "/addons/wiki/?page=decisions%2FDEMO-0009")
    found = render(fw, addon, "page.wiki", params={"q": "nightly"})
    [res] = [w for w in found if isinstance(w, Card) and w.title == "Results for nightly"]
    assert [r[0].text for r in res.body[0].rows] == ["Why we chose X"]
    # related pages on a ticket: Home mentions DEMO-0001
    [panel] = render(fw, addon, "ticket.pages", ticket(fw, "DEMO-0001"))
    assert any(isinstance(w, Table) and w.rows[0][0].text == "Home" for w in panel.body)


def test_stale_hint_shows_on_the_page_view(wiki):
    from orch.addons.api import Snapshot
    from orch.clock import now
    fw, addon = wiki
    refresh(fw, addon)
    fw.cache("wiki", Snapshot("branch-diffs", "tickets", now(), items=({
        "id": "DEMO-0001:r", "label": "DEMO-0001", "role": "info", "text": "1", "ticket": "DEMO-0001", "status": "testing",
        "repo": None, "branch": "b", "files": ["src/a.py"]},)))
    from orch.core import store
    path, t = store.load(fw.ws, "DEMO-0001")
    t.meta["status"] = "testing"
    store.save(fw.ws, t, old_path=path)
    widgets = render(fw, addon, "page.wiki", params={"page": "decisions/DEMO-0009"})
    assert isinstance(widgets[0], Callout) and "may need an update" in widgets[0].title
    assert isinstance(widgets[1], Action) and widgets[1].action == "dismiss"


def test_page_view_shows_title_path_and_markdown_full_page(wiki):
    fw, addon = wiki
    refresh(fw, addon)
    widgets = render(fw, addon, "page.wiki", params={"page": "decisions/DEMO-0009"})
    assert widgets[0] == Link("All pages", "/addons/wiki/")
    card = widgets[1]
    assert card.title == "Why we chose X"
    assert card.body[0] == Copy("Copy path", f"{FOLDER}/decisions/DEMO-0009.md")
    assert card.body[1].text == "Body about nightly loads.\n"  # front matter is not part of the body
    assert card.body[1].here == "decisions/DEMO-0009" and "Home" in card.body[1].pages
    [missing] = [w for w in render(fw, addon, "page.wiki", params={"page": "nope"}) if isinstance(w, Callout)]
    assert missing.title == "Page not found"


def test_empty_or_missing_folder_shows_an_empty_state_and_creates_nothing(tmp_path):
    fw, addon = make(tmp_path, repo=False)
    assert not (fw.root / FOLDER).exists()
    snap = refresh(fw, addon)
    assert snap.items == () and snap.health == "ok"
    widgets = render(fw, addon, "page.wiki")
    assert widgets[1] == Callout("info", "No pages in orchestrator/wiki/ yet",
                                 "Add Markdown files to this folder and press Refresh, or create one from a ticket.")
    assert widgets[2] == Copy("Copy folder path", FOLDER)
    render(fw, addon, "page.wiki", params={"page": "x"})
    render(fw, addon, "ticket.pages", ticket(fw, "DEMO-0001"))
    assert not (fw.root / FOLDER).exists()


def test_ticket_panel_offers_create_only_without_a_page(wiki):
    fw, addon = wiki
    refresh(fw, addon)
    [panel] = render(fw, addon, "ticket.pages", ticket(fw, "DEMO-0002"))
    assert Action("create_page", "Create page from ticket", "DEMO-0002") in panel.body
    write(fw.root, f"{FOLDER}/decisions/DEMO-0002.md", "# DEMO-0002")
    refresh(fw, addon)
    assert all(not isinstance(w, Action) for p in render(fw, addon, "ticket.pages", ticket(fw, "DEMO-0002")) for w in p.body)


def test_github_provider_gets_no_create_action(tmp_path):
    fw = fake_workspace(tmp_path / "w", tickets=[{"title": "x", "status": "open"}])
    fw.enable("wiki", {"provider": "github-wiki"})
    addon = fw.load(ADDON, runner=FakeRunner())
    assert render(fw, addon, "ticket.pages", ticket(fw, "DEMO-0001")) == []


def test_create_page_writes_a_marked_copy_and_commits_nothing(wiki):
    fw, addon = wiki
    head = git(fw.root, "rev-parse", "HEAD")
    out = addon.obj.act("create_page", "DEMO-0001", addon.ctx.provider_context())
    rel = f"{FOLDER}/decisions/DEMO-0001.md"
    assert out.kind == "none" and rel in out.reason and "commit it" in out.reason
    text = (fw.root / rel).read_text(encoding="utf-8")
    assert text.startswith("---\ntitle: DEMO-0001 Move loader\nsource: DEMO-0001\n---\n\n# DEMO-0001 Move loader\n")
    doc = addon.ctx.document("DEMO-0001")
    assert "> Copied from ticket DEMO-0001 on " in text and doc["gates"]["requirements"]["hash"][:19] in text
    assert "not a record of approval" in text and "approved" not in text.replace("approved is shown", "")
    assert "## Summary\n\nMoves the loader." in text and "## Requirements\n\n- keep the schema" in text
    assert text.rstrip().endswith("## Decisions")
    assert git(fw.root, "rev-parse", "HEAD") == head  # orch never commits (#36)
    assert git(fw.root, "diff", "--cached", "--name-only") == ""
    with pytest.raises(ValidationError, match="exists already"):
        addon.obj.act("create_page", "DEMO-0001", addon.ctx.provider_context())


def test_create_page_needs_no_git_repo(tmp_path):
    fw, addon = make(tmp_path, repo=False)
    addon.obj.act("create_page", "DEMO-0001", addon.ctx.provider_context())
    assert (fw.root / FOLDER / "decisions" / "DEMO-0001.md").is_file()


def test_a_page_never_links_a_ticket_artifact(wiki):
    """Page text is bound by no gate: an agent-written page cannot make a live link or image to any artifact."""
    from orch.dashboard.views import TEMPLATES
    fw, addon = wiki
    write(fw.root, f"{FOLDER}/evil.md", "[a](/a/DEMO-0001/x.png) [b](artifacts/DEMO-0001/x.png) ![c](artifact:x.png) "
                                        "![d](/a/DEMO-0001/x.png) [e](https://example.com/)\n")
    refresh(fw, addon)
    widgets = render(fw, addon, "page.wiki", params={"page": "evil"})
    [md] = [w for c in widgets if isinstance(c, Card) for w in c.body if isinstance(w, Markdown)]
    html = TEMPLATES.env.from_string("{{ x|md_page|safe }}").render(x=md.text)
    assert "/a/" not in html and "artifacts/" not in html and "<img" not in html
    assert 'href="https://example.com/"' in html


@pytest.mark.parametrize("target", ["demo-0001", "DEMO-1; rm", "../x", "", "DEMO-0099"])
def test_create_page_refuses_bad_or_unknown_keys(wiki, target):
    fw, addon = wiki
    with pytest.raises(Exception):
        addon.obj.act("create_page", target, addon.ctx.provider_context())
    assert not (fw.root / FOLDER / "decisions" / "DEMO-0099.md").exists()


def test_create_page_refuses_a_decisions_folder_that_is_a_link(wiki, tmp_path):
    fw, addon = wiki
    outside = tmp_path / "outside"
    outside.mkdir()
    (fw.root / FOLDER / "decisions").rename(fw.root / FOLDER / "old")
    (fw.root / FOLDER / "decisions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValidationError, match="link"):
        addon.obj.act("create_page", "DEMO-0001", addon.ctx.provider_context())
    assert list(outside.iterdir()) == []


def test_create_page_needs_the_local_provider_and_an_inside_folder(tmp_path):
    fw = fake_workspace(tmp_path / "w", tickets=[{"title": "x", "status": "open"}])
    fw.enable("wiki", {"provider": "github-wiki"})
    addon = fw.load(ADDON, runner=FakeRunner())
    with pytest.raises(ValidationError, match="provider to local"):
        addon.obj.act("create_page", "DEMO-0001", addon.ctx.provider_context())
    fw.enable("wiki", {"provider": "local", "folder": "../out"})
    with pytest.raises(ValidationError, match="inside the workspace"):
        addon.obj.act("create_page", "DEMO-0001", addon.ctx.provider_context())


@pytest.mark.parametrize("folder", [".", "orchestrator", "orchestrator/tickets", "orchestrator/tickets/open",
                                    "orchestrator/.state", "orchestrator/artifacts", ".git", "docs/.hidden", "/etc",
                                    "~/x", "a/../b", "C:/x"])
def test_folder_refuses_hidden_outside_and_orch_record_folders(tmp_path, folder):
    assert local_module.resolve_folder(tmp_path, {"folder": folder})[0] is None


@pytest.mark.parametrize("folder", ["orchestrator/wiki", "docs/wiki", "wiki"])
def test_folder_accepts_plain_folders(tmp_path, folder):
    assert local_module.resolve_folder(tmp_path, {"folder": folder})[0] == tmp_path.joinpath(*folder.split("/"))


class TestLocalContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return LocalPages()

    @pytest.fixture
    def provider_ctx(self, orch_workspace):
        orch_workspace.enable("wiki", {"provider": "local"})
        return orch_workspace.provider_context(MANIFEST, runner=FakeRunner())


def test_relative_links_between_pages_open_the_page_and_escapes_stay_inert(wiki, tmp_path):
    from orch.dashboard.views import TEMPLATES
    fw, addon = wiki
    (tmp_path / "secret.md").write_text("# outside", encoding="utf-8")
    write(fw.root, f"{FOLDER}/decisions/linked.md",
          "[home](../Home.md) [sib](DEMO-0009.md#why) [out](../../../secret.md) [up](../../README.md) "
          "[none](missing.md) [txt](../notes.txt)\n")
    (fw.root / FOLDER / "decisions" / "alias.md").symlink_to(tmp_path / "secret.md")
    write(fw.root, f"{FOLDER}/decisions/linked2.md", "[alias](alias.md)\n")
    refresh(fw, addon)
    g = type("G", (), {"addon": "wiki", "confirms": {}, "uploads": {}, "key_prefix": "DEMO"})()
    tpl = TEMPLATES.env.from_string('{% import "_widgets.html" as w %}{{ w.widgets(ws, g) }}')
    html = tpl.render(ws=render(fw, addon, "page.wiki", params={"page": "decisions/linked"}), g=g)
    assert 'href="/addons/wiki/?page=Home"' in html and 'href="/addons/wiki/?page=decisions%2FDEMO-0009#why"' in html
    for bad in ("secret", "README", "missing", "notes.txt"):
        assert f'href="{bad}' not in html and f"page={bad}" not in html and f"%2F{bad}" not in html
    html2 = tpl.render(ws=render(fw, addon, "page.wiki", params={"page": "decisions/linked2"}), g=g)
    assert "page=decisions%2Falias" not in html2  # a symlinked file is never listed, so never a link target

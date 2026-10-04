import os
from pathlib import Path

import pytest

from orch.addons.manifest import load_manifest
from orch.testing import FakeRunner, ProviderContract
from orch_wiki import github_wiki
from orch_wiki.github_wiki import GitHubWikiPages, clone_dir, origin_repo, parse_log, wiki_repos
from orch_wiki.pages import IndexReader

MANIFEST = load_manifest(Path(__file__).resolve().parents[1])
FIXTURES = Path(__file__).with_name("fixtures")
SPACE = "acme/ticket-orch-demo"
URL = "https://github.com/acme/ticket-orch-demo.wiki.git"
FETCH = ["git", "-C", "*", "fetch", "--depth", "200", "--no-tags", "--quiet", "origin"]
RESET = ["git", "-C", "*", "reset", "--hard", "--quiet", "FETCH_HEAD"]
CLONE = ["git", "clone", "--depth", "200", "--no-tags", "--single-branch", "--quiet", "--", URL, "*"]


def runner(*extra):
    r = FakeRunner.from_dir(FIXTURES)
    for argv, kw in extra:
        r.recordings.insert(0, FakeRunner().add(argv, **kw).recordings[0])
    return r.add(FETCH).add(RESET).add(CLONE)


def ctx_for(fw, r, settings=None):
    fw.enable("wiki", {"provider": "github-wiki", "repos": SPACE, **(settings or {})})
    return fw.provider_context(MANIFEST, runner=r)


def test_scopes_from_settings_or_the_harness_origin(orch_workspace):
    p = GitHubWikiPages()
    ctx = ctx_for(orch_workspace, FakeRunner(), {"repos": f"{SPACE}, bad repo, -x/y, ../z, {SPACE}"})
    assert p.scopes(ctx) == [SPACE]
    git = orch_workspace.root / ".git"
    git.mkdir()
    (git / "config").write_text('[core]\n\tbare = false\n[remote "origin"]\n\turl = git@github.com:acme/ticket-orch-demo.git\n'
                                '\tfetch = +refs/heads/*:refs/remotes/origin/*\n', encoding="utf-8")
    assert p.scopes(ctx_for(orch_workspace, FakeRunner(), {"repos": ""})) == [SPACE]
    assert p.scopes(ctx_for(orch_workspace, FakeRunner(), {"provider": "confluence"})) == []
    assert wiki_repos({"repos": ",".join(f"o/r{i}" for i in range(9))}, orch_workspace.root) == [f"o/r{i}" for i in range(5)]


@pytest.mark.parametrize("url, repo", [
    ("https://github.com/acme/ticket-orch-demo.git", SPACE),
    ("https://github.com/acme/ticket-orch-demo", SPACE),
    ("ssh://git@github.com/acme/ticket-orch-demo.git", SPACE),
    ("https://gitlab.com/acme/ticket-orch-demo.git", None),
])
def test_origin_repo(tmp_path, url, repo):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")
    assert origin_repo(tmp_path) == repo


ORIGIN = '[remote "origin"]\n\turl = git@github.com:acme/ticket-orch-demo.git\n'


def test_origin_repo_of_a_worktree(tmp_path):
    """A worktree's .git is a file; its gitdir has a commondir file that points to the main .git with the config."""
    main = tmp_path / "main" / ".git"
    gitdir = main / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (main / "config").write_text(ORIGIN, encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert origin_repo(wt) == SPACE


def test_origin_repo_of_a_submodule(tmp_path):
    """A submodule's .git file names a relative gitdir that holds its own config."""
    gitdir = tmp_path / "super" / ".git" / "modules" / "sub"
    gitdir.mkdir(parents=True)
    (gitdir / "config").write_text(ORIGIN, encoding="utf-8")
    sub = tmp_path / "super" / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")
    assert origin_repo(sub) == SPACE


@pytest.mark.parametrize("content", ["", "not a gitdir line\n", "gitdir: /does/not/exist\n", "gitdir:\n"])
def test_origin_repo_of_a_broken_git_file(tmp_path, content):
    (tmp_path / ".git").write_text(content, encoding="utf-8")
    assert origin_repo(tmp_path) is None


def test_fetch_parses_the_existing_clone(orch_workspace, clone_with_pages):
    r = runner()
    ctx = ctx_for(orch_workspace, r)
    dest = clone_with_pages(ctx.addon.state_dir)
    snap = GitHubWikiPages().fetch(ctx, SPACE, None)
    assert snap.health == "ok" and snap.complete
    pages = {i["id"]: i for i in snap.items}
    assert sorted(pages) == ["Architecture", "Data-model", "Home", "Runbook", "Test"]
    arch = pages["Architecture"]
    assert arch["title"] == "Architecture overview" and arch["url"] == "https://github.com/acme/ticket-orch-demo/wiki/Architecture"
    assert arch["documents"] == ["src/acme/ingest/**", "acme-energy-data:sql/*.sql"] and arch["links"] == ["DEMO-0003", "DEMO-0007"]
    assert arch["updated_at"] == "2026-10-02T13:10:00+00:00" and arch["author"] == "Severin" and arch["path"] == "Architecture.md"
    assert pages["Runbook"]["author"] == "Anna Beispiel" and pages["Home"]["updated_at"] == "2026-10-02T12:17:45+00:00"
    assert [c[3] for c in r.calls] == ["fetch", "reset", "log"] and r.calls[0][2] == str(dest)
    texts = IndexReader(ctx.addon.state_dir).texts("github-wiki", SPACE)
    assert "meter readings" in texts["Architecture"] and texts["Home"].startswith("home\nwelcome to the ticket-orch-demo wiki!")


def test_first_fetch_clones_into_the_state_folder(orch_workspace):
    r = runner()
    ctx = ctx_for(orch_workspace, r)
    dest = clone_dir(ctx.addon.state_dir, "acme", "ticket-orch-demo")
    dest.mkdir(parents=True)
    (dest / "half-cloned.md").write_text("x", encoding="utf-8")  # a broken earlier clone is removed first
    snap = GitHubWikiPages().fetch(ctx, SPACE, None)
    assert r.calls[0] == ("git", "clone", "--depth", "200", "--no-tags", "--single-branch", "--quiet", "--", URL, str(dest))
    assert not (dest / "half-cloned.md").exists() and snap.health == "ok" and snap.items == ()


@pytest.mark.parametrize("rec, health, needle", [
    ({"returncode": 128, "stderr": "remote: Repository not found.\nfatal: repository 'https://github.com/acme/x.wiki.git/' not found\n"},
     "error", "no wiki at github.com/acme/ticket-orch-demo/wiki yet"),
    ({"returncode": 128, "stderr": "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"},
     "auth_required", "gh auth setup-git"),
    ({"returncode": 128, "stderr": "fatal: unable to access '...': Could not resolve host: github.com\n"}, "offline", "Could not resolve host"),
    ({"raises": "missing"}, "error", "not installed"),
    ({"raises": "timeout"}, "offline", "timed out"),
])
def test_clone_failures_become_health(orch_workspace, rec, health, needle):
    r = FakeRunner([{"argv": CLONE, **rec}])
    snap = GitHubWikiPages().fetch(ctx_for(orch_workspace, r), SPACE, None)
    assert snap.health == health and needle in snap.message and snap.items == ()


def test_page_caps(orch_workspace, clone_with_pages, monkeypatch):
    monkeypatch.setattr(github_wiki, "MAX_PAGES", 2)
    ctx = ctx_for(orch_workspace, runner())
    dest = clone_with_pages(ctx.addon.state_dir)
    (dest / "Big.md").write_text("x" * (github_wiki.MAX_PAGE_BYTES + 1), encoding="utf-8")
    snap = GitHubWikiPages().fetch(ctx, SPACE, None)
    assert len(snap.items) == 2 and not snap.complete
    assert "showing the first 2 of 6 pages" in snap.message


def test_big_pages_are_skipped(orch_workspace, clone_with_pages):
    ctx = ctx_for(orch_workspace, runner())
    dest = clone_with_pages(ctx.addon.state_dir)
    (dest / "Big.md").write_text("x" * (github_wiki.MAX_PAGE_BYTES + 1), encoding="utf-8")
    snap = GitHubWikiPages().fetch(ctx, SPACE, None)
    assert "Big" not in {i["id"] for i in snap.items} and "1 page(s) over 256 KiB skipped" in snap.message


def test_symlinked_pages_are_never_read(orch_workspace, clone_with_pages, tmp_path):
    ctx = ctx_for(orch_workspace, runner())
    dest = clone_with_pages(ctx.addon.state_dir)
    secret = tmp_path / "secret.txt"
    secret.write_text("PRIVATE KEY", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Leak.md").write_text("PRIVATE KEY", encoding="utf-8")
    try:
        os.symlink(secret, dest / "Secret.md")
        os.symlink(outside, dest / "linked-folder")
    except OSError:
        pytest.skip("no symlinks on this system")
    snap = GitHubWikiPages().fetch(ctx, SPACE, None)
    ids = {i["id"] for i in snap.items}
    assert "Secret" not in ids and "Leak" not in ids
    texts = IndexReader(ctx.addon.state_dir).texts("github-wiki", SPACE)
    assert not any("private key" in t for t in texts.values())


def test_parse_log_keeps_the_newest_commit_per_file():
    out = parse_log("\x1ea\x1f2026-10-02T15:00:00+02:00\x1fA\n\nx.md\n\x1eb\x1f2026-10-01T15:00:00+02:00\x1fB\n\nx.md\ny.md\n\x1ejunk\n")
    assert out == {"x.md": ("2026-10-02T13:00:00+00:00", "A"), "y.md": ("2026-10-01T13:00:00+00:00", "B")}


class TestGitHubWikiContract(ProviderContract):
    @pytest.fixture
    def provider(self):
        return GitHubWikiPages()

    @pytest.fixture
    def provider_ctx(self, orch_workspace, clone_with_pages):
        ctx = ctx_for(orch_workspace, runner())
        clone_with_pages(ctx.addon.state_dir)
        return ctx

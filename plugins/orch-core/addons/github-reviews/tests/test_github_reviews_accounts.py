"""#165: a repo gh's account cannot see, the workspace's own gh account, and repos on other git hosts."""
import pytest
from conftest import ADDON, demo_runner

from github_reviews.github import NO_ACCESS, GitHubProvider
from orch.addons.api import Snapshot
from orch.addons.manifest import load_manifest
from orch.addons.runtime import SlotView
from orch.addons.widgets import Badge, Card, Table
from orch.errors import OrchError
from orch.testing import fake_workspace

M = load_manifest(ADDON)
HARNESS = "acme-energy-data"
PAGE = "page.github-reviews"
NOT_FOUND = {"returncode": 1, "stderr": "GraphQL: Could not resolve to a Repository with the name 'acme/ticket-orch-demo-ingest'."}
INGEST_LIST = ["gh", "pr", "list", "--repo", "acme/ticket-orch-demo-ingest", "--state", "open", "--limit", "100", "--json", "*"]
TOKEN = {"argv": ["gh", "auth", "token", "-u", "work-acct"], "stdout": "gho_worktoken\n"}


def _flat(widgets):
    for w in widgets:
        yield w
        if isinstance(w, Card):
            yield from _flat(w.body)


def test_a_repo_the_account_cannot_see_is_that_repo_only(demo):
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=demo_runner(demo, {"argv": INGEST_LIST, **NOT_FOUND})),
                                  "ingest", None)
    assert snap.health == "ok" and not snap.complete and snap.items == ()
    assert snap.message.startswith(NO_ACCESS) and "acme/ticket-orch-demo-ingest" in snap.message
    assert "active gh account severinlindenmann" in snap.message and "gh auth switch -u <account>" in snap.message


def test_the_page_keeps_the_other_repos_and_shows_one_error_row(demo):
    runner = demo_runner(demo, {"argv": INGEST_LIST, **NOT_FOUND})
    addon = demo.load(ADDON, runner=runner)
    ctx = addon.ctx.provider_context()
    for p in addon.obj.providers:
        for scope in p.scopes(ctx):
            demo.cache(addon.name, p.fetch(ctx, scope, None))
    out = addon.obj.widgets(PAGE, SlotView(demo.ws, addon, PAGE, None, {"state": "all"}))
    flat = list(_flat(out))
    errors = [w for w in flat if isinstance(w, Table) and w.columns == ("State", "Problem")]
    assert len(errors) == 1 and errors[0].rows[0][0] == Badge("err", "no access")
    assert "gh auth switch" in errors[0].rows[0][1]
    assert any(isinstance(w, Table) and w.columns[0] == "PR" and w.rows for w in flat)  # the harness's PRs still show
    ingest = next(c for c in out[0].body if c.title == "ingest")
    assert ingest.role == "warn"


def test_the_workspace_account_sets_gh_token_for_every_gh_call(demo):
    demo.enable("github-reviews", {"gh_user": "work-acct"})
    runner = demo_runner(demo, TOKEN)
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=runner), HARNESS, None)
    assert snap.health == "ok" and len(snap.items) == 8
    gh = [(c, e) for c, e in zip(runner.calls, runner.envs) if c[0] == "gh"]
    assert gh[0] == (tuple(TOKEN["argv"]), {})
    assert [c[:2] for c, _ in gh[1:]] == [("gh", "api"), ("gh", "pr")]
    assert all(e == {"GH_TOKEN": "gho_worktoken"} for _, e in gh[1:])
    assert all(e == {} for c, e in zip(runner.calls, runner.envs) if c[0] == "git")


def test_no_access_with_a_workspace_account_names_that_account(demo):
    demo.enable("github-reviews", {"gh_user": "work-acct"})
    runner = demo_runner(demo, TOKEN, {"argv": INGEST_LIST, **NOT_FOUND})
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=runner), "ingest", None)
    assert "gh account severinlindenmann" in snap.message and "this addon's settings" in snap.message


def test_an_account_gh_has_no_login_for_asks_to_log_in(demo):
    demo.enable("github-reviews", {"gh_user": "work-acct"})
    runner = demo_runner(demo, {"argv": TOKEN["argv"], "returncode": 1,
                                "stderr": "no oauth token found for github.com account work-acct"})
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=runner), HARNESS, None)
    assert snap.health == "auth_required" and snap.message.endswith("run gh auth login")
    assert not any(c[:2] == ("gh", "pr") for c in runner.calls)


def test_a_bad_account_setting_runs_no_gh(demo):
    demo.enable("github-reviews", {"gh_user": "-u evil"})
    runner = demo_runner(demo)
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=runner), HARNESS, None)
    assert snap.health == "error" and "not a GitHub login" in snap.message
    assert not any(c[0] == "gh" for c in runner.calls)


def test_actions_use_the_workspace_account(demo):
    demo.enable("github-reviews", {"gh_user": "work-acct"})
    addon = demo.load(ADDON, runner=demo_runner(demo, TOKEN))
    ctx = addon.ctx.provider_context()
    for p in addon.obj.providers:
        for scope in p.scopes(ctx):
            demo.cache(addon.name, p.fetch(ctx, scope, None))
    runner = demo_runner(demo, TOKEN, {"argv": ["gh", "pr", "ready", "22", "--repo", "acme/ticket-orch-demo"]})
    addon.obj.act("mark_ready", "acme/ticket-orch-demo#22", addon.ctx.provider_context(runner=runner))
    assert runner.envs[runner.calls.index(("gh", "pr", "ready", "22", "--repo", "acme/ticket-orch-demo"))] == \
        {"GH_TOKEN": "gho_worktoken"}


def test_action_without_a_login_for_the_account_is_refused(demo):
    demo.enable("github-reviews", {"gh_user": "work-acct"})
    addon = demo.load(ADDON, runner=demo_runner(demo, TOKEN))
    ctx = addon.ctx.provider_context()
    for p in addon.obj.providers:
        for scope in p.scopes(ctx):
            demo.cache(addon.name, p.fetch(ctx, scope, None))
    runner = demo_runner(demo, {"argv": TOKEN["argv"], "returncode": 1, "stderr": "no oauth token"})
    with pytest.raises(OrchError, match="gh auth login"):
        addon.obj.act("mark_ready", "acme/ticket-orch-demo#22", addon.ctx.provider_context(runner=runner))
    assert not any(c[:3] == ("gh", "pr", "ready") for c in runner.calls)


def test_a_repo_on_another_git_host_type_makes_no_gh_call(tmp_path):
    fw = fake_workspace(tmp_path / "ws", repos={"meta": {"path": "meta", "type": "bitbucket-server",
                                                         "base_url": "https://bitbucket.example.com"}})
    meta = str((fw.ws.root / "meta").resolve())
    runner = demo_runner(fw, {"argv": ["git", "-C", meta, "remote", "get-url", "origin"],
                              "stdout": "ssh://git@bitbucket.example.com:7999/dis/meta.git\n"})
    snap = GitHubProvider().fetch(fw.provider_context(M, runner=runner), "meta", None)
    assert snap.health == "ok" and snap.message == "no provider for this host: bitbucket.example.com (bitbucket-server)"
    assert not any(c[0] == "gh" for c in runner.calls)


def test_a_cached_no_access_snapshot_is_not_counted_as_zero(demo):
    addon = demo.load(ADDON, runner=demo_runner(demo))
    now = addon.ctx.provider_context().now()
    demo.cache(addon.name, Snapshot("github", "acme-energy-data", now, complete=False,
                                    message=f"{NO_ACCESS} acme/x is not visible to the active gh account bob; run gh auth switch -u <account>"))
    card = addon.obj.widgets(PAGE, SlotView(demo.ws, addon, PAGE, None, {}))[0].body[0]
    kv = dict(next(w for w in card.body if w.kind == "kv").rows)
    assert kv["open PRs"] is None and card.role == "warn"

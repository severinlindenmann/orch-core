import pytest
from conftest import ADDON, demo_runner, recordings

from github_reviews.gh import classify
from github_reviews.github import GitHubProvider, checks_of
from github_reviews.localgit import LocalGitProvider, parse_remote
from orch.addons.api import COMMIT_HOOK_HEADER, item_problems
from orch.addons.manifest import load_manifest
from orch.testing import AddonContract, FakeRunner, ProviderContract

M = load_manifest(ADDON)
HARNESS = "acme-energy-data"


@pytest.mark.parametrize("url, want", [
    ("git@github.com:acme/ticket-orch-demo.git", ("github.com", "acme/ticket-orch-demo")),
    ("https://github.com/acme/ticket-orch-demo.git\n", ("github.com", "acme/ticket-orch-demo")),
    ("https://github.com/acme/ticket-orch-demo", ("github.com", "acme/ticket-orch-demo")),
    ("ssh://git@GitHub.com:22/acme/x.git", ("github.com", "acme/x")),
    ("https://gitlab.example.com/group/sub/app.git", ("gitlab.example.com", "group/sub/app")),
    ("file:///tmp/repo", None), ("/local/path", None), ("", None),
])
def test_parse_remote(url, want):
    assert parse_remote(url) == want


def test_checks_of_rollups():
    run = "https://github.com/a/b/actions/runs/{}/job/1"
    assert checks_of([])["state"] == "none"
    assert checks_of([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": run.format(1)}])["state"] == "passed"
    failed = checks_of([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": run.format(7)},
                        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "TIMED_OUT", "detailsUrl": run.format(7)},
                        {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": "", "detailsUrl": run.format(8)}])
    assert failed == {"state": "failed", "passed": 0, "failed": 2, "pending": 1, "cancelled": 0,
                      "url": run.format(7), "failed_runs": ["7"]}
    assert checks_of([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"}])["state"] == "cancelled"
    assert checks_of([{"__typename": "StatusContext", "state": "PENDING"}])["state"] == "pending"
    assert checks_of("nonsense")["state"] == "none"


def test_fetch_the_demo_harness(demo):
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=demo_runner(demo)), HARNESS, None)
    assert snap.health == "ok" and snap.me == "severinlindenmann" and snap.complete and len(snap.items) == 8
    by = {i["number"]: i for i in snap.items}
    assert by[21]["checks"]["state"] == "failed" and by[21]["checks"]["failed_runs"] == ["36979598989"]
    assert by[22]["draft"] is True and by[18]["mergeable"] == "conflict" and by[23]["mergeable"] == "yes"
    assert by[23]["source_branch"] == "feature/DEMO-0009-late-arriving-exports" and by[23]["my_role"] == "author"
    assert by[90]["my_role"] == "reviewer" and by[90]["review"] == "required" and by[90]["checks"]["state"] == "pending"
    assert by[90]["requested_reviewers"] == ["severinlindenmann"] and by[90]["mergeable"] == "unknown"
    assert by[91]["review"] == "changes_requested" and by[91]["checks"] == {
        "state": "failed", "passed": 0, "failed": 1, "pending": 0, "cancelled": 0,
        "url": "https://ci.example.com/lint/91", "failed_runs": []}
    assert by[20]["labels"] == ["review:changes-requested"] and by[19]["repo"] == "acme/ticket-orch-demo"
    assert all(item_problems("reviews", i) == [] for i in snap.items)


def test_fetch_a_sub_repo_uses_its_own_remote(demo):
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=demo_runner(demo)), "ingest", None)
    assert [i["number"] for i in snap.items] == [4, 5]
    assert {i["repo"] for i in snap.items} == {"acme/ticket-orch-demo-ingest"} and snap.items[1]["checks"]["state"] == "none"


def test_me_is_asked_once_a_day(demo):
    runner = demo_runner(demo)
    p = GitHubProvider()
    ctx = demo.provider_context(M, runner=runner)
    p.fetch(ctx, HARNESS, None)
    p.fetch(ctx, "ingest", None)
    assert runner.calls.count(("gh", "api", "user")) == 1


def test_non_github_remote_skips_gh(demo):
    runner = demo_runner(demo, {"argv": ["git", "-C", "*", "remote", "get-url", "origin"], "stdout": "git@gitlab.example.com:acme/app.git\n"})
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=runner), HARNESS, None)
    assert snap.health == "ok" and snap.items == () and snap.message == "no provider for this host: gitlab.example.com"
    assert not any(call[0] == "gh" for call in runner.calls)


def test_no_origin_remote(demo):
    runner = demo_runner(demo, {"argv": ["git", "-C", "*", "remote", "get-url", "origin"], "returncode": 2,
                                "stderr": "error: No such remote 'origin'\n"})
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=runner), HARNESS, None)
    assert snap.health == "ok" and snap.message == "no remote named origin" and not any(c[0] == "gh" for c in runner.calls)


def test_unknown_scope_is_an_error(demo):
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=demo_runner(demo)), "gone", None)
    assert snap.health == "error" and "no longer in git.repos" in snap.message


@pytest.mark.parametrize("recording, health, needle", [
    ({"returncode": 4, "stderr": "To get started with GitHub CLI, please run:  gh auth login"}, "auth_required", "gh auth login"),
    ({"returncode": 1, "stderr": "GraphQL: API rate limit exceeded for user ID 1."}, "rate_limited", "rate limit"),
    ({"returncode": 1, "stderr": "error connecting to api.github.com\ncheck your internet connection"}, "offline", "error connecting"),
    ({"raises": "timeout"}, "offline", "timed out"),
    ({"raises": "missing"}, "error", "gh is not installed"),
    ({"returncode": 1, "stderr": "GraphQL: Could not resolve to a Repository with the name 'x'."}, "error", "Could not resolve"),
    ({"stdout": "not json"}, "error", "did not return JSON"),
])
def test_gh_failures_become_health(demo, recording, health, needle):
    runner = demo_runner(demo, {"argv": ["gh", "api", "user"], **recording})
    snap = GitHubProvider().fetch(demo.provider_context(M, runner=runner), HARNESS, None)
    assert snap.health == health and needle in snap.message and snap.items == ()
    assert (snap.retry_after is not None) == (health == "rate_limited")


def test_classify_keeps_the_first_line():
    assert classify(1, "boom\nmore") == ("error", "boom")
    assert classify(2, "") == ("error", "gh exited with 2")


def test_local_git_state(demo):
    hooks = demo.root / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "commit-msg").write_text(f"#!/bin/sh\n{COMMIT_HOOK_HEADER}\n", encoding="utf-8")
    snap = LocalGitProvider().fetch(demo.provider_context(M, runner=demo_runner(demo)), HARNESS, None)
    by = {i["id"]: i for i in snap.items}
    assert snap.health == "ok" and by["branch"]["text"] == "feature/DEMO-0009-late-arriving-exports"
    assert by["vs-default"]["label"] == "vs main" and by["vs-default"]["text"] == "ahead 3, behind 1" and by["vs-default"]["role"] == "warn"
    assert by["changes"]["text"] == "2 files" and by["changes"]["role"] == "warn" and by["changes"]["count"] == 2
    assert by["remote"]["host"] == "github.com" and by["remote"]["full_name"] == "acme/ticket-orch-demo"
    assert by["commit-check"]["text"] == "installed" and by["commit-check"]["role"] == "ok"
    assert all(item_problems("status", i) == [] for i in snap.items)


def test_local_git_without_the_hook(demo):
    by = {i["id"]: i for i in LocalGitProvider().fetch(demo.provider_context(M, runner=demo_runner(demo)), "ingest", None).items}
    assert by["commit-check"]["text"] == "missing" and by["commit-check"]["role"] == "warn"
    assert by["remote"]["full_name"] == "acme/ticket-orch-demo-ingest"


def test_a_folder_that_is_not_a_repo(demo):
    runner = demo_runner(demo, {"argv": ["git", "-C", "*", "status", "--porcelain=v2", "--branch"], "returncode": 128,
                                "stderr": "fatal: not a git repository (or any of the parent directories): .git\n"})
    snap = LocalGitProvider().fetch(demo.provider_context(M, runner=runner), "ingest", None)
    assert snap.health == "ok" and [(i["id"], i["text"]) for i in snap.items] == [("state", "not a git repository")]


def test_the_configured_default_branch_wins(tmp_path):
    from orch.testing import fake_workspace
    fw = fake_workspace(tmp_path / "w", repos={"app": {"path": ".", "default_branch": "develop"}})
    runner = FakeRunner([{"argv": ["git", "-C", "*", "rev-list", "--left-right", "--count", "origin/develop...HEAD"], "stdout": "0\t0\n"},
                         *recordings()])
    by = {i["id"]: i for i in LocalGitProvider().fetch(fw.provider_context(M, runner=runner), "app", None).items}
    assert by["vs-default"]["text"] == "ahead 0, behind 0" and by["vs-default"]["role"] == "neu"
    assert not any("symbolic-ref" in call for call in runner.calls)


class TestGitHubProvider(ProviderContract):
    @pytest.fixture
    def provider(self):
        return GitHubProvider()

    @pytest.fixture
    def provider_ctx(self, demo):
        return demo.provider_context(M, runner=demo_runner(demo))


class TestLocalGitProvider(ProviderContract):
    @pytest.fixture
    def provider(self):
        return LocalGitProvider()

    @pytest.fixture
    def provider_ctx(self, demo):
        return demo.provider_context(M, runner=demo_runner(demo))


class TestAddon(AddonContract):
    addon_dir = ADDON
    runner = FakeRunner(recordings(), strict=False)


def test_a_failing_whoami_is_asked_once_an_hour(demo, monkeypatch):
    from datetime import datetime, timedelta, timezone

    import orch.clock
    clock = {"t": datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(orch.clock, "now", lambda: clock["t"])
    runner = demo_runner(demo, {"argv": ["gh", "api", "user"], "returncode": 1, "stderr": "boom"})
    p = GitHubProvider()
    ctx = demo.provider_context(M, runner=runner)
    first = p.fetch(ctx, HARNESS, None)
    assert first.health == "error" and first.message == "boom"
    for minutes in (5, 30, 59):
        clock["t"] = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)
        snap = p.fetch(ctx, HARNESS, None)
        assert snap.health == "ok" and snap.me is None and len(snap.items) == 8
    assert runner.calls.count(("gh", "api", "user")) == 1
    clock["t"] += timedelta(minutes=2)  # 61 minutes after the failure
    assert p.fetch(ctx, HARNESS, None).health == "error"
    assert runner.calls.count(("gh", "api", "user")) == 2

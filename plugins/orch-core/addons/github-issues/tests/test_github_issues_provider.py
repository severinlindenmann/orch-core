import pytest
from conftest import ADDON, GH, SCOPE, recordings, runner

from github_issues import provider
from github_issues.provider import IssuesProvider, github_trackers, sprints
from orch.addons.api import TrackerRef, item_problems
from orch.addons.manifest import load_manifest
from orch.testing import AddonContract, FakeRunner, ProviderContract, fake_workspace

M = load_manifest(ADDON)


def test_scopes_come_from_github_trackers(tmp_path):
    fw = fake_workspace(tmp_path / "w", trackers=[
        GH, {"prefix": "ABC", "pattern": "ABC-\\d+", "url": "https://jira.example/browse/{key}"},
        {"prefix": "ENT", "pattern": "ENT-(?P<id>\\d+)", "url": "https://github.acme.internal/a/b/issues/{id}"},
        {"prefix": "GH2", "pattern": "GH2-(?P<id>\\d+)", "url": "https://github.com/acme/ticket-orch-demo/issues/{id}"}])
    assert IssuesProvider().scopes(fw.provider_context(M)) == [SCOPE]
    assert github_trackers(fw.provider_context(M).trackers())[SCOPE].prefix == "GH"


def test_fetch_maps_the_demo_issues(issues_ws):
    snap = IssuesProvider().fetch(issues_ws.provider_context(M, runner=runner()), SCOPE, None)
    assert snap.health == "ok" and snap.me == "severinlindenmann" and snap.complete and len(snap.items) == 15
    by = {i["key"]: i for i in snap.items}
    assert by["GH-13"]["category"] == "done" and by["GH-13"]["resolution"] == "completed" and by["GH-13"]["raw_status"] == "closed"
    assert by["GH-5"]["priority"] == "urgent" and by["GH-5"]["rank"] == 0 and by["GH-5"]["type"] == "bug"
    assert by["GH-5"]["assigned_to_me"] is True and by["GH-5"]["assignee"] == "severinlindenmann"
    assert by["GH-12"]["assigned_to_me"] is False and by["GH-12"]["rank"] == 4 and by["GH-12"]["resolution"] is None
    assert by["GH-3"]["sprint"] == {"name": "Sprint 42", "number": 2, "state": "active", "start": None, "end": None}
    assert by["GH-12"]["sprint"]["state"] == "future" and by["GH-15"]["sprint"]["state"] == "closed"
    assert by["GH-1"]["url"] == "https://github.com/acme/ticket-orch-demo/issues/1" and by["GH-1"]["tracker"] == "GH"
    assert all(item_problems("issues", i) == [] for i in snap.items)


def test_sprints_by_due_date_with_start():
    issues = [
        {"state": "CLOSED", "milestone": {"number": 1, "title": "A", "dueOn": "2026-09-20T00:00:00Z"}},
        {"state": "OPEN", "milestone": {"number": 2, "title": "B", "dueOn": "2026-10-04T00:00:00Z"}},
        {"state": "OPEN", "milestone": {"number": 3, "title": "C", "dueOn": "2026-10-18T00:00:00Z"}},
        {"state": "OPEN", "milestone": {"number": 4, "title": "Old", "dueOn": "2026-09-01T00:00:00Z"}},
        {"state": "OPEN", "milestone": None},
    ]
    got = sprints(issues, "2026-10-02")
    assert {n: s["state"] for n, s in got.items()} == {1: "closed", 2: "active", 3: "future", 4: "open"}
    assert got[2]["start"] == "2026-09-20T00:00:00Z" and got[3]["start"] == "2026-10-04T00:00:00Z" and got[4]["start"] is None


def test_a_bare_number_tracker_is_an_error(tmp_path):
    bare = {"prefix": "GH", "pattern": "\\d+", "url": "https://github.com/acme/ticket-orch-demo/issues/{key}"}
    fw = fake_workspace(tmp_path / "w", trackers=[bare])
    r = runner()
    snap = IssuesProvider().fetch(fw.provider_context(M, runner=r), SCOPE, None)
    assert snap.health == "error" and "does not match GH-<number>" in snap.message and r.calls == []


def test_a_tracker_that_accepts_no_key_form(tmp_path):
    odd = {"prefix": "GH", "pattern": "GH-[A-Z]+", "url": "https://github.com/acme/ticket-orch-demo/issues/{key}"}
    fw = fake_workspace(tmp_path / "w", trackers=[odd])
    r = runner()
    snap = IssuesProvider().fetch(fw.provider_context(M, runner=r), SCOPE, None)
    assert snap.health == "error" and "does not match GH-<number>" in snap.message and r.calls == []


def test_the_limit_marks_the_snapshot_incomplete(issues_ws, monkeypatch):
    monkeypatch.setattr(provider, "LIMIT", 3)
    snap = IssuesProvider().fetch(issues_ws.provider_context(M, runner=runner()), SCOPE, None)
    assert snap.complete is False and snap.message == "showing the newest 3 issues"


@pytest.mark.parametrize("recording, health", [
    ({"returncode": 4, "stderr": "gh auth login"}, "auth_required"),
    ({"returncode": 1, "stderr": "API rate limit exceeded"}, "rate_limited"),
    ({"raises": "timeout"}, "offline"),
    ({"raises": "missing"}, "error"),
])
def test_gh_failures_become_health(issues_ws, recording, health):
    snap = IssuesProvider().fetch(issues_ws.provider_context(M, runner=runner({"argv": ["gh", "api", "user"], **recording})), SCOPE, None)
    assert snap.health == health and snap.items == ()


def test_tracker_key_for():
    tr = TrackerRef(GH["prefix"], GH["pattern"], GH["url"])
    assert tr.key_for(12) == "GH-12" and tr.url_for("GH-12").endswith("/issues/12")


class TestIssuesProvider(ProviderContract):
    @pytest.fixture
    def provider(self):
        return IssuesProvider()

    @pytest.fixture
    def provider_ctx(self, issues_ws):
        return issues_ws.provider_context(M, runner=runner())


class TestAddon(AddonContract):
    addon_dir = ADDON
    runner = FakeRunner(recordings(), strict=False)


def test_a_failing_whoami_is_asked_once_an_hour(issues_ws, monkeypatch):
    from datetime import datetime, timedelta, timezone

    import orch.clock
    start = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    clock = {"t": start}
    monkeypatch.setattr(orch.clock, "now", lambda: clock["t"])
    r = runner({"argv": ["gh", "api", "user"], "returncode": 1, "stderr": "boom"})
    p = IssuesProvider()
    ctx = issues_ws.provider_context(M, runner=r)
    assert p.fetch(ctx, SCOPE, None).health == "error"
    for minutes in (5, 59):
        clock["t"] = start + timedelta(minutes=minutes)
        snap = p.fetch(ctx, SCOPE, None)
        assert snap.health == "ok" and snap.me is None and len(snap.items) == 15
    assert r.calls.count(("gh", "api", "user")) == 1
    clock["t"] = start + timedelta(minutes=61)
    assert p.fetch(ctx, SCOPE, None).health == "error" and r.calls.count(("gh", "api", "user")) == 2


def test_whoami_stays_once_a_day_after_a_success(issues_ws, monkeypatch):
    from datetime import datetime, timedelta, timezone

    import orch.clock
    start = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    clock = {"t": start}
    monkeypatch.setattr(orch.clock, "now", lambda: clock["t"])
    r = runner()
    p = IssuesProvider()
    ctx = issues_ws.provider_context(M, runner=r)
    for hours in (0, 2, 23):
        clock["t"] = start + timedelta(hours=hours)
        assert p.fetch(ctx, SCOPE, None).me == "severinlindenmann"
    assert r.calls.count(("gh", "api", "user")) == 1
    clock["t"] = start + timedelta(hours=25)
    p.fetch(ctx, SCOPE, None)
    assert r.calls.count(("gh", "api", "user")) == 2

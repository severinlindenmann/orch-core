"""A2 platform additions (Task 3) on the dashboard: failing PRs from any reviews provider switch Start agent to
fix-checks; ticket actions still get no Ops (ruling R-A2-3)."""
from datetime import datetime, timezone

import pytest

from addon_fixtures import loaded
from orch.addons import cache
from orch.addons.api import Snapshot
from orch.addons.loader import AddonRegistry

ORIGIN = {"origin": "http://testserver"}
WALL = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
OVER = {"capabilities": ["provider", "page", "settings"], "menu": {"title": "Echo", "icon": "status"},
        "actions": [{"id": "plain", "label": "Plain"}, {"id": "close", "label": "Close", "tickets": True}]}


class Reviews:
    id = "prs"
    kind = "reviews"
    interval_s = 60

    def scopes(self, ctx):
        return ["harness"]

    def fetch(self, ctx, scope, previous):
        raise AssertionError("these tests write the cache themselves")


class Echo:
    def __init__(self):
        self.providers = [Reviews()]
        self.calls = []

    def widgets(self, slot, view):
        return []

    def act(self, action_id, target, ctx):  # three arguments only: a human_ops keyword would be a TypeError
        self.calls.append((action_id, target, ctx))
        return "done"


@pytest.fixture
def echo(ws):
    obj = Echo()
    ws._addons = AddonRegistry(ws, {"echo": loaded(ws, obj, name="echo", **OVER)})
    return obj


@pytest.fixture
def client(ws, echo, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _review(**over):
    item = {"provider": "github", "host": "github.com", "repo": "a/b", "number": 7, "url": "https://github.com/a/b/pull/7",
            "title": "Fix it", "state": "open", "draft": False, "review": "none", "checks": {"state": "failed"},
            "source_branch": "fix/L-0001-x"}
    return {**item, **over}


def test_ticket_actions_get_no_ops(client, echo):
    from orch.core.ops import Ops
    for action, target in (("plain", "a"), ("close", "b")):
        r = client.post(f"/addons/echo/actions/{action}", data={"target": target}, headers=ORIGIN, follow_redirects=False)
        assert r.status_code == 303 and "msg=done" in r.headers["location"]
    assert [c[:2] for c in echo.calls] == [("plain", "a"), ("close", "b")]
    assert not any(isinstance(c[2], Ops) for c in echo.calls)


def test_failing_review_pr_selects_fix_checks(client, ws, put):
    tid = put("in-progress", title="Fix it")
    assert tid == "L-0001"
    cache.write_snapshot(ws, "echo", Snapshot("prs", "harness", WALL, items=(_review(),)))
    assert client.app.state.addons.failing_prs(type("T", (), {"id": tid})()) == ["https://github.com/a/b/pull/7"]
    html = client.get(f"/t/{tid}").text
    assert '<option value="fix-checks" selected>' in html
    assert "Fix the failing checks on https://github.com/a/b/pull/7 for ticket L-0001." in html


def test_passing_or_unlinked_prs_do_not_switch_the_mode(client, ws, put):
    tid = put("in-progress", title="Fix it")
    cache.write_snapshot(ws, "echo", Snapshot("prs", "harness", WALL, items=(
        _review(checks={"state": "passed"}), _review(number=8, url="https://github.com/a/b/pull/8", source_branch="x"))))
    assert client.app.state.addons.failing_prs(type("T", (), {"id": tid})()) == []
    assert '<option value="fix-checks" selected>' not in client.get(f"/t/{tid}").text


def test_review_items_ignore_other_provider_kinds(client, ws, put):
    tid = put("in-progress", title="Fix it")
    cache.write_snapshot(ws, "echo", Snapshot("other", "harness", WALL, items=(_review(),)))
    assert client.app.state.addons.review_items(type("T", (), {"id": tid})()) == []


def test_a_body_only_mention_is_never_the_main_pr(client, ws, put):
    """#14 review: a PR whose description says "follow-up of L-1" belongs to its own ticket; on L-1 it is only
    "mentioned in", never the main PR, and it never switches Start agent to fix-checks."""
    first = put("in-progress", title="First")            # L-0001
    for _ in range(10):
        put("in-progress", title="Filler")
    twelfth = put("in-progress", title="Twelfth")        # L-0012
    cache.write_snapshot(ws, "echo", Snapshot("prs", "harness", WALL, items=(
        _review(number=9, url="https://github.com/a/b/pull/9", title="Tidy", source_branch="tidy",
                body_refs=["L-1"]),                       # unpadded, body only
        _review(number=10, url="https://github.com/a/b/pull/10", title="More", source_branch="more",
                body_refs=["L-12"], checks={"state": "passed"}),
    )))
    runtime = client.app.state.addons
    assert runtime.review_items(type("T", (), {"id": first})()) == []
    assert runtime.failing_prs(type("T", (), {"id": first})()) == []
    assert [i["number"] for i in runtime.mention_index().get(first, [])] == [9]  # L-1 is L-0001, not L-0012
    assert [i["number"] for i in runtime.mention_index().get(twelfth, [])] == [10]
    html = client.get(f"/t/{first}").text
    head = html.split('class="ticket-head', 1)[1].split("</header>", 1)[0]
    assert "mentioned in" in head and "PR #9" in head and 'class="code-token' not in head
    assert '<option value="fix-checks" selected>' not in html


def test_a_title_match_stays_the_main_pr(client, ws, put):
    tid = put("in-progress", title="Fix it")
    cache.write_snapshot(ws, "echo", Snapshot("prs", "harness", WALL, items=(
        _review(title=f"{tid} retry", source_branch="x", body_refs=[tid]),)))
    assert [i["number"] for i in client.app.state.addons.review_items(type("T", (), {"id": tid})())] == [7]
    assert client.app.state.addons.mention_index() == {}
    head = client.get(f"/t/{tid}").text.split('class="ticket-head', 1)[1].split("</header>", 1)[0]
    assert "PR #7" in head and "mentioned in" not in head

import inspect
from datetime import datetime, timezone

import pytest
from conftest import ADDON, SCOPE, runner

from github_issues.ignore import ignored, token
from github_issues.views import COLUMNS, SYNC_COLUMNS, progress
from orch.addons import intents
from orch.addons.api import Intent
from orch.addons.runtime import SlotView
from orch.addons.widgets import KV, Action, Badge, Callout, Card, Chips, Link, Table
from orch.core import store
from orch.core.events import Actor
from orch.errors import OrchError

HUMAN = Actor("human", "you", "dashboard")


def _cached(fw, login="severinlindenmann"):
    first = [{"argv": ["gh", "api", "user"], "stdout_json": {"login": login}}]
    addon = fw.load(ADDON, runner=runner(*first))
    ctx = addon.ctx.provider_context()
    fw.cache(addon.name, addon.obj.providers[0].fetch(ctx, SCOPE, None))
    return addon


def _view(fw, addon, slot, ticket=None, **params):
    return SlotView(fw.ws, addon, slot, ticket, params)


def _card(out, title):
    return next(w for w in out if isinstance(w, Card) and w.title.startswith(title))


def _keys(out):
    table = next(w for w in _card(out, "GitHub issues").body if isinstance(w, Table))
    return [row[0].text for row in table.rows]


def _click(fw, addon, action, target):
    """What the dashboard's POST does: act() with a ProviderContext (no Ops), then core runs the Intent as the human."""
    result = addon.obj.act(action, target, addon.ctx.provider_context())
    spec = addon.manifest.action(action)
    return intents.execute(fw.ws, intents.as_intent(result), allowed_ref=target, tickets=spec.tickets,
                           actor=HUMAN, source="act")


def test_mine_is_the_default(issues_ws):
    addon = _cached(issues_ws)
    out = addon.obj.widgets("board.external", _view(issues_ws, addon, "board.external"))
    assert _card(out, "GitHub issues").title == "GitHub issues · Mine" and _keys(out) == ["GH-5", "GH-3", "GH-11"]


def test_mine_falls_back_to_the_sprint_not_imported(issues_ws):
    addon = _cached(issues_ws, login="someone-else")
    out = addon.obj.widgets("board.external", _view(issues_ws, addon, "board.external"))
    note = next(w for w in out if isinstance(w, Callout))
    assert note.title == "Nothing in GitHub is assigned to you" and "Sprint 42 · not imported" in note.text
    assert _keys(out) == ["GH-3", "GH-11", "GH-2", "GH-4", "GH-8"]


def test_unknown_me_shows_unknown_mine_count(issues_ws, monkeypatch):
    """After a failed `gh api user`, Whoami returns None for an hour (gh.py WHOAMI_BACKOFF); the next `gh issue list`
    can still succeed, giving health ok with me=None. The "Mine" filter count must then read "unknown", never 0."""
    from datetime import datetime, timedelta, timezone

    import orch.clock
    from github_issues.provider import IssuesProvider

    start = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    clock = {"t": start}
    monkeypatch.setattr(orch.clock, "now", lambda: clock["t"])
    r = runner({"argv": ["gh", "api", "user"], "returncode": 1, "stderr": "boom"})
    addon = issues_ws.load(ADDON, runner=r)
    ctx = addon.ctx.provider_context()
    p = IssuesProvider()
    first = p.fetch(ctx, SCOPE, None)
    assert first.health == "error"
    clock["t"] = start + timedelta(minutes=5)
    snap = p.fetch(ctx, SCOPE, None)
    assert snap.health == "ok" and snap.me is None
    issues_ws.cache(addon.name, snap)

    out = addon.obj.widgets("board.external", _view(issues_ws, addon, "board.external"))
    show = next(w for w in out if isinstance(w, Card) and w.title == "Show")
    chips = next(x for x in show.body if isinstance(x, Chips)).items
    assert chips[0] == Link("Mine unknown", "/board?view=external&filter=mine", current=True)
    note = next(w for w in out if isinstance(w, Callout))
    assert "not known yet" in note.title.lower()


@pytest.mark.parametrize("flt, setting, keys", [
    ("all", None, ["GH-5", "GH-3", "GH-11", "GH-1", "GH-2", "GH-4", "GH-6", "GH-8", "GH-9", "GH-10", "GH-12"]),
    ("sprint", None, ["GH-5", "GH-3", "GH-7", "GH-11", "GH-1", "GH-2", "GH-4", "GH-8"]),
    (None, "all", ["GH-5", "GH-3", "GH-11", "GH-1", "GH-2", "GH-4", "GH-6", "GH-8", "GH-9", "GH-10", "GH-12"]),
    ("nonsense", None, ["GH-5", "GH-3", "GH-11"]),
])
def test_filters_and_the_saved_default(issues_ws, flt, setting, keys):
    if setting:
        issues_ws.enable("github-issues", {"default_filter": setting})
    addon = _cached(issues_ws)
    params = {"filter": flt} if flt else {}
    assert _keys(addon.obj.widgets("board.external", _view(issues_ws, addon, "board.external", **params))) == keys


def test_rows_link_local_tickets_or_offer_import(issues_ws):
    addon = _cached(issues_ws)
    out = addon.obj.widgets("board.external", _view(issues_ws, addon, "board.external", filter="all"))
    rows = {row[0].text: row for row in next(w for w in _card(out, "GitHub issues").body if isinstance(w, Table)).rows}
    local = COLUMNS.index("Local ticket")
    assert rows["GH-1"][local] == Link("DEMO-0001 · testing", "/t/DEMO-0001")
    assert rows["GH-9"][local] == Action("import", "Import", "GH-9")
    assert rows["GH-5"][COLUMNS.index("Status")] == Badge("neu", "to do") and rows["GH-5"][COLUMNS.index("Priority")] == "urgent"
    assert rows["GH-5"][COLUMNS.index("Sprint")] == "Sprint 42" and rows["GH-12"][COLUMNS.index("Assignee")] is None


def test_out_of_sync_card(issues_ws):
    addon = _cached(issues_ws)
    out = addon.obj.widgets("board.external", _view(issues_ws, addon, "board.external"))
    card = _card(out, "Out of sync")
    table = next(w for w in card.body if isinstance(w, Table))
    assert card.role == "warn" and table.columns == SYNC_COLUMNS
    fixes = {row[0].text: (row[3], row[4]) for row in table.rows}
    assert fixes["GH-13"] == (Action("close_local", "Close local", "DEMO-0002"),
                              Action("ignore", "Ignore", "DEMO-0002|GH-13|done"))
    assert fixes["GH-5"][0] == Action("reopen_local", "Reopen local", "DEMO-0003")


def test_sprint_progress(issues_ws):
    addon = _cached(issues_ws)
    card = _card(addon.obj.widgets("board.external", _view(issues_ws, addon, "board.external")), "Sprint · Sprint 42")
    assert dict(card.body[0].rows) == {"Done": "1 of 8 (12%)", "Expected by today": "unknown (no start or due date)",
                                       "Ends": "no due date"}


def test_progress_with_dates():
    sprint = {"number": 2, "start": "2026-09-20T00:00:00Z", "end": "2026-10-04T00:00:00Z"}
    items = [{"sprint": {"number": 2}, "category": "done"}] + [{"sprint": {"number": 2}, "category": "todo"}] * 3
    p = progress(items, sprint, datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert p == {"done": 1, "total": 4, "pct": 25, "expected": 79}


def test_no_github_tracker_says_how_to_add_one(tmp_path):
    from orch.testing import fake_workspace
    fw = fake_workspace(tmp_path / "w")
    addon = fw.load(ADDON)
    [callout] = addon.obj.widgets("board.external", _view(fw, addon, "board.external"))
    assert callout.title == "No GitHub tracker" and "/issues/{id}" in callout.text


def test_ticket_panel(issues_ws):
    addon = _cached(issues_ws)
    t = store.load(issues_ws.ws, "DEMO-0002")[1]
    [card] = addon.obj.widgets("ticket.external", _view(issues_ws, addon, "ticket.external", t))
    assert card.title == "GH-13 · GitHub issue"
    assert Callout("warn", "Out of sync", "Closed in GitHub, not done here.") in card.body
    assert dict(card.body[-1].rows)["Fix"] == Action("close_local", "Close local", "DEMO-0002")
    in_sync = store.load(issues_ws.ws, "DEMO-0004")[1]
    [card] = addon.obj.widgets("ticket.external", _view(issues_ws, addon, "ticket.external", in_sync))
    assert not any(isinstance(w, Callout) for w in card.body)


def test_import_is_idempotent(issues_ws):
    addon = _cached(issues_ws)  # fixtures/gh-issue-view.json holds GH-9's text
    ctx = addon.ctx.provider_context()
    assert addon.obj.act("import", "GH-9", ctx) == Intent("import", ref="GH-9", value="Handle late-arriving gateway exports",
                                                          reason="Imported from https://github.com/acme/ticket-orch-demo/issues/9",
                                                          data={"ask": "Late exports are dropped.\n## Steps\nRe-run the job."})
    assert _click(issues_ws, addon, "import", "GH-9") == "Imported GH-9 as DEMO-0005"
    t = store.load(issues_ws.ws, "DEMO-0005")[1]
    assert t.status == "backlog" and t.title == "Handle late-arriving gateway exports"
    assert t.section("Ask") == "Late exports are dropped.\n\\## Steps\nRe-run the job."  # the issue text, neutralised
    assert t.meta["external"] == [{"key": "GH-9", "url": "https://github.com/acme/ticket-orch-demo/issues/9"}]
    assert addon.obj.act("import", "gh-9", ctx) == Intent("none", reason="GH-9 is already DEMO-0005")


def test_import_without_the_issue_text_still_imports(issues_ws):
    from orch.testing.fakes import Recording
    addon = _cached(issues_ws)
    addon.ctx.runner.recordings.insert(0, Recording(("gh", "issue", "view", "9", "--repo", SCOPE, "--json", "body"), 1, "",
                                                    "could not resolve host: api.github.com", None))
    assert _click(issues_ws, addon, "import", "GH-9") == "Imported GH-9 as DEMO-0005"
    assert store.load(issues_ws.ws, "DEMO-0005")[1].section("Ask") == ""


def test_close_and_reopen_local(issues_ws):
    addon = _cached(issues_ws)
    ctx = addon.ctx.provider_context()
    assert addon.obj.act("close_local", "DEMO-0002", ctx) == Intent("close", ref="DEMO-0002", reason="GH-13 is closed in GitHub")
    assert _click(issues_ws, addon, "close_local", "DEMO-0002") == "Closed DEMO-0002"
    assert store.load(issues_ws.ws, "DEMO-0002")[1].status == "done"
    assert _click(issues_ws, addon, "reopen_local", "DEMO-0003") == "Reopened DEMO-0003 to backlog"


def test_close_local_refuses_when_no_longer_out_of_sync(issues_ws):
    addon = _cached(issues_ws)
    for action, target in (("close_local", "DEMO-0004"), ("reopen_local", "DEMO-0002"), ("close_local", "DEMO-0001"),
                           ("close_local", "DEMO-0003")):
        with pytest.raises(OrchError, match="no longer out of sync"):
            _click(issues_ws, addon, action, target)
    assert store.load(issues_ws.ws, "DEMO-0004")[1].status == "open"
    assert store.load(issues_ws.ws, "DEMO-0001")[1].status == "testing"


def test_act_gets_no_ops_and_returns_only_intents(issues_ws):
    addon = _cached(issues_ws)
    assert list(inspect.signature(addon.obj.act).parameters) == ["action_id", "target", "ctx"]
    ctx = addon.ctx.provider_context()
    for action, target in (("import", "GH-9"), ("close_local", "DEMO-0002"), ("reopen_local", "DEMO-0003"),
                           ("ignore", "DEMO-0002|GH-13|done")):
        assert isinstance(addon.obj.act(action, target, ctx), Intent)
    assert all(a.tickets for a in addon.manifest.actions if a.id in ("import", "close_local", "reopen_local"))
    assert not addon.manifest.action("ignore").tickets


def test_forged_targets_are_refused(issues_ws):
    addon = _cached(issues_ws)
    ctx = addon.ctx.provider_context()
    with pytest.raises(OrchError, match="not a local ticket id"):
        addon.obj.act("close_local", "DEMO-0002|GH-13", ctx)
    with pytest.raises(OrchError, match="not in the cached GitHub issues"):
        addon.obj.act("import", "GH-404", ctx)
    with pytest.raises(OrchError, match="not an out-of-sync target"):
        addon.obj.act("ignore", "DEMO-0002|GH-13", ctx)
    for forged in ("DEMO-0001|GH-1|todo", "DEMO-0002|GH-13|todo", "DEMO-0004|GH-13|done", "DEMO-0002|GH-404|done"):
        with pytest.raises(OrchError):
            addon.obj.act("ignore", forged, ctx)
    assert ignored(addon.ctx.state_dir) == set()
    with pytest.raises(OrchError, match="unknown action"):
        addon.obj.act("deploy", "x", ctx)
    # an intent for another ticket than the clicked target is refused by core, and nothing changes
    with pytest.raises(OrchError, match="nothing changed"):
        intents.execute(issues_ws.ws, Intent("close", ref="DEMO-0004", reason="forged"), allowed_ref="DEMO-0002",
                        tickets=True, actor=HUMAN, source="act")
    assert store.load(issues_ws.ws, "DEMO-0004")[1].status == "open"
    assert len(store.scan(issues_ws.ws)) == 4


def test_ignore_hides_the_difference_until_it_changes(issues_ws):
    addon = _cached(issues_ws)
    ctx = addon.ctx.provider_context()
    assert addon.obj.act("ignore", "DEMO-0002|GH-13|done", ctx) == \
        Intent("none", reason="Ignored the difference on DEMO-0002 until GH-13 changes again")
    assert token("DEMO-0002", "GH-13", "done") in ignored(addon.ctx.state_dir)
    out = addon.obj.widgets("board.external", _view(issues_ws, addon, "board.external"))
    table = next(w for w in _card(out, "Out of sync").body if isinstance(w, Table))
    assert [row[0].text for row in table.rows] == ["GH-5"]

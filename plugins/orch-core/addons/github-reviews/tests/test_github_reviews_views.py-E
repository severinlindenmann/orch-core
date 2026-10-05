import pytest
from conftest import ADDON, demo_runner

from github_reviews.views import COLUMNS, size_label
from orch.addons.api import Intent, Snapshot
from orch.addons.runtime import SlotView
from orch.addons.widgets import KV, Action, Badge, Callout, Card, Chips, Link, Table, Text, Tile
from orch.core import store
from orch.errors import OrchError

PAGE = "page.github-reviews"
SUB = "in 1 repo"  # both failing demo PRs are in the same repository


def _cached(demo, runner=None):
    addon = demo.load(ADDON, runner=runner or demo_runner(demo))
    ctx = addon.ctx.provider_context()
    for p in addon.obj.providers:
        for scope in p.scopes(ctx):
            demo.cache(addon.name, p.fetch(ctx, scope, None))
    return addon


def _view(demo, addon, slot, ticket=None, **params):
    return SlotView(demo.ws, addon, slot, ticket, params)


def _flat(widgets):
    for w in widgets:
        yield w
        if isinstance(w, Card):
            yield from _flat(w.body)


def _pr_rows(widgets):
    return [row for w in _flat(widgets) if isinstance(w, Table) and w.columns == COLUMNS for row in w.rows]


def _kv(card):
    return dict(next(w for w in card.body if isinstance(w, KV)).rows)


def _chips(card):
    return [item for w in card.body if isinstance(w, Chips) for item in w.items]


def test_page_defaults_to_needs_your_review(demo):
    addon = _cached(demo)
    out = addon.obj.widgets(PAGE, _view(demo, addon, PAGE))
    assert [w.title for w in out if isinstance(w, Card)][:2] == ["Repositories", "Showing: Needs your review"]
    assert [row[0].text for row in _pr_rows(out)] == ["#90"]


def test_repo_cards(demo):
    hooks = demo.root / ".git" / "hooks"
    hooks.mkdir(parents=True)
    from orch.addons.api import COMMIT_HOOK_HEADER
    (hooks / "commit-msg").write_text(f"#!/bin/sh\n{COMMIT_HOOK_HEADER}\n", encoding="utf-8")
    addon = _cached(demo)
    repos = addon.obj.widgets(PAGE, _view(demo, addon, PAGE))[0]
    assert repos.layout == "grid"  # compact cards side by side, not one long KV list per repo
    harness, ingest = repos.body
    rows = _kv(harness)
    assert harness.title == "acme-energy-data"
    assert next(w for w in harness.body if isinstance(w, KV)).layout == "stats"
    assert _chips(harness) == [Text("harness"), Badge("ok", "GitHub"), Text("branch feature/DEMO-0009-late-arriving-exports"),
                               Text("ahead 3, behind 1 vs main"), Badge("warn", "Uncommitted: 2 files"),
                               Badge("ok", "Commit check: installed")]
    assert rows == {"open PRs": 8, "need review": 1, "failing": Badge("err", "2")}
    assert ingest.title == "ingest" and _chips(ingest)[0] == Text("sub-repo") and _kv(ingest)["open PRs"] == 2


def test_loading_before_the_first_fetch_is_not_an_empty_result(demo):
    """GR-02: dashes, "0" counts and "Nothing in Needs your review" before the first fetch read as a real result."""
    addon = demo.load(ADDON, runner=demo_runner(demo))
    out = addon.obj.widgets(PAGE, _view(demo, addon, PAGE))
    assert [w.title for w in out] == ["Loading pull requests..."]
    assert not any(isinstance(w, Text) and w.text.startswith("Nothing in") for w in _flat(out))


def test_repo_cards_explain_a_missing_provider(demo):
    addon = demo.load(ADDON, runner=demo_runner(demo))
    demo.cache(addon.name, Snapshot("github", "acme-energy-data", addon.ctx.provider_context().now(),
                                    message="no provider for this host: gitlab.example.com"))
    card = addon.obj.widgets(PAGE, _view(demo, addon, PAGE))[0].body[0]
    assert Badge("neu", "no provider for this host: gitlab.example.com") in _chips(card) and _kv(card)["open PRs"] == 0


def test_unknown_me_shows_unknown_not_zero(demo, monkeypatch):
    """After a failed `gh api user`, Whoami returns None for an hour (gh.py WHOAMI_BACKOFF); the next `gh pr list`
    can still succeed, giving health ok with me=None. The review/mine counts and filters must then read "unknown",
    never 0 (unknown is never shown as 0)."""
    from datetime import datetime, timedelta, timezone

    import orch.clock
    from github_reviews.github import GitHubProvider

    clock = {"t": datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(orch.clock, "now", lambda: clock["t"])
    runner = demo_runner(demo, {"argv": ["gh", "api", "user"], "returncode": 1, "stderr": "boom"})
    addon = demo.load(ADDON, runner=runner)
    ctx = addon.ctx.provider_context()
    p = GitHubProvider()
    first = p.fetch(ctx, "acme-energy-data", None)
    assert first.health == "error"
    clock["t"] += timedelta(minutes=5)
    snap = p.fetch(ctx, "acme-energy-data", None)
    assert snap.health == "ok" and snap.me is None
    demo.cache(addon.name, snap)

    out = addon.obj.widgets(PAGE, _view(demo, addon, PAGE, state="all"))
    repos = next(w for w in out if isinstance(w, Card) and w.title == "Repositories")
    rows = _kv(repos.body[0])
    assert rows["need review"] == "unknown"
    note = next(w for w in out if isinstance(w, Callout))
    assert "unknown" in note.title.lower() and "review counts unavailable" in note.text
    filters = next(w for w in out if isinstance(w, Card) and w.title.startswith("Showing"))
    review = next(c for c in _chips(filters) if c.text.startswith("Needs your review"))
    assert review.text == "Needs your review unknown"


def test_today_summary_builds_no_link_index(demo, monkeypatch):
    """The Today summary never links PRs to tickets, so it must not pay for building the core's LinkIndex."""
    addon = _cached(demo)
    view = _view(demo, addon, "today.summary")

    def boom():
        raise AssertionError("today.summary must not build the LinkIndex")

    monkeypatch.setattr(view, "links", boom)
    assert addon.obj.widgets("today.summary", view) == [
        Tile("Checks failing", 2, "err", href="/addons/github-reviews/?state=failing", sub=SUB)]


@pytest.mark.parametrize("params, numbers", [
    ({"state": "failing"}, ["#21", "#91"]),
    ({"state": "draft"}, ["#22"]),
    ({"state": "mine"}, ["#23", "#22", "#21", "#20", "#19", "#18", "#4", "#5"]),
    ({"state": "all", "repo": "ingest"}, ["#4", "#5"]),
    ({"state": "nonsense", "repo": "nope"}, ["#90"]),
])
def test_filters(demo, params, numbers):
    addon = _cached(demo)
    assert [row[0].text for row in _pr_rows(addon.obj.widgets(PAGE, _view(demo, addon, PAGE, **params)))] == numbers


def test_rows_offer_the_right_action_and_ticket(demo):
    addon = _cached(demo)
    rows = {row[0].text: row for row in _pr_rows(addon.obj.widgets(PAGE, _view(demo, addon, PAGE, state="all")))}
    nxt, title = COLUMNS.index("Next"), COLUMNS.index("Title")
    rerun = rows["#21"][nxt]
    assert isinstance(rerun, Action) and (rerun.action, rerun.target) == ("rerun_failed", "acme/ticket-orch-demo#21")
    assert "acme/ticket-orch-demo#21" in rerun.confirm and "Guard suspect-rate" in rerun.confirm and rerun.detail
    ready = rows["#22"][nxt]
    assert (ready.action, ready.target) == ("mark_ready", "acme/ticket-orch-demo#22")
    assert "acme/ticket-orch-demo#22" in ready.confirm and "outside orch" not in ready.detail
    assert rows["#91"][nxt] is None  # failing, no run to rerun and no ticket: nothing to offer
    assert rows["#19"][nxt] == Link("DEMO-0001", "/t/DEMO-0001")
    assert rows["#18"][title].endswith("merge conflict")
    assert rows["#23"][0] == Link("#23", "https://github.com/acme/ticket-orch-demo/pull/23")


def test_table_has_no_author_column_and_fits(demo):
    """GR-01: every row is the same user, so the Author column only costs width (the table overflowed its card)."""
    assert "Author" not in COLUMNS and len(COLUMNS) <= 5


def test_landing_view_falls_back_when_nothing_needs_your_review(demo):
    """GR-03: opening all your PRs yourself must not leave the default view empty."""
    addon = _cached(demo)
    for snap in addon.ctx.snapshots("github"):
        demo.cache(addon.name, snap.replace(items=tuple({**i, "requested_reviewers": []} for i in snap.items)))
    out = addon.obj.widgets(PAGE, _view(demo, addon, PAGE))
    assert any(isinstance(w, Card) and w.title == "Showing: Yours" for w in out)
    assert any(isinstance(w, Text) and "Nothing needs your review" in w.text for w in out)
    assert _pr_rows(out)
    explicit = addon.obj.widgets(PAGE, _view(demo, addon, PAGE, state="review"))
    assert not _pr_rows(explicit)  # asked for explicitly: no silent switch


def test_invalid_filters_are_announced(demo):
    addon = _cached(demo)
    out = addon.obj.widgets(PAGE, _view(demo, addon, PAGE, state="nonsense", repo="nope"))
    texts = [w.text for w in out if isinstance(w, Text)]
    assert any("Unknown filter" in t for t in texts) and any("Unknown repository" in t for t in texts)


def test_repo_card_labels_are_screen_reader_only(demo):
    addon = _cached(demo)
    card = addon.obj.widgets(PAGE, _view(demo, addon, PAGE))[0].body[0]
    chips = [w for w in card.body if isinstance(w, Chips)]
    assert chips and all(c.show_label is False and c.label for c in chips)


def test_one_wording_for_no_review():
    from github_reviews.views import REVIEWS
    assert REVIEWS["none"][1] == REVIEWS["required"][1]


def test_acted_on_row_says_requested_until_a_newer_fetch(demo):
    """GR-05: after Rerun failed the same button must not be offered again before the next fetch."""
    from github_reviews.actions import act
    addon = _cached(demo)
    target = "acme/ticket-orch-demo#21"
    runner = demo_runner(demo, {"argv": ["gh", "run", "rerun", "36979598989", "--failed", "--repo", "acme/ticket-orch-demo"]})
    addon = demo.load(ADDON, runner=runner)
    act("rerun_failed", target, addon.ctx.provider_context())
    view = _view(demo, addon, PAGE, state="all")
    rows = {row[0].text: row for row in _pr_rows(addon.obj.widgets(PAGE, view))}
    assert rows["#21"][COLUMNS.index("Next")] == Text("Rerun requested")
    for p in addon.obj.providers:  # a fetch after the action lands: the row is real data again
        for scope in p.scopes(addon.ctx.provider_context()):
            demo.cache(addon.name, p.fetch(addon.ctx.provider_context(), scope, None))
    rows = {row[0].text: row for row in _pr_rows(addon.obj.widgets(PAGE, _view(demo, addon, PAGE, state="all")))}
    assert isinstance(rows["#21"][COLUMNS.index("Next")], Action)


def test_size_label():
    assert size_label({"additions": 56, "deletions": 0, "changed_files": 2}) == "M · +56 −0 · 2 files"
    assert size_label({"additions": 3, "deletions": 1}) == "XS · +3 −1"
    assert size_label({"additions": 3, "deletions": 1, "changed_files": 1}) == "XS · +3 −1 · 1 file"
    assert size_label({"additions": 900, "deletions": 200, "changed_files": 30}) == "XL · +900 −200 · 30 files"
    assert size_label({}) == "size unknown"


def test_one_ticket_several_repos(demo):
    addon = _cached(demo)
    card = next(w for w in addon.obj.widgets(PAGE, _view(demo, addon, PAGE)) if isinstance(w, Card) and w.title == "One ticket, several repos")
    table, text = card.body
    assert [(r[0].text, r[1]) for r in table.rows] == [("DEMO-0003", "acme-energy-data #21, ingest #5"),
                                                       ("DEMO-0009", "acme-energy-data #23, ingest #4")]
    assert text == Text("Suggested merge order: DEMO-0003 → DEMO-0009")


def test_login_needed_is_left_to_the_core_health_line(demo):
    addon = _cached(demo)
    old = next(s for s in addon.ctx.snapshots("github") if s.scope == "acme-energy-data")
    demo.cache(addon.name, old.replace(health="auth_required", message="login needed: run gh auth login"))
    out = addon.obj.widgets(PAGE, _view(demo, addon, PAGE, state="all"))
    # core's health callout says it (with the command to copy); the addon keeps its rows and draws no second one
    assert not any(isinstance(w, Card) and "login" in w.title.lower() for w in out) and _pr_rows(out)


def test_today_summary_and_items(demo):
    addon = _cached(demo)
    assert addon.obj.widgets("today.summary", _view(demo, addon, "today.summary")) == [
        Tile("Checks failing", 2, "err", href="/addons/github-reviews/?state=failing", sub=SUB)]
    [card] = addon.obj.widgets("today.from_addons", _view(demo, addon, "today.from_addons"))
    rows = [(r[0].text, r[1].url if r[1] else None) for r in card.body[0].rows]
    assert rows == [("Checks failing on acme-energy-data #21", "/t/DEMO-0003#start-agent-DEMO-0003"),
                    ("Review requested on acme-energy-data #90", None)]


def test_today_is_empty_without_data(demo):
    addon = demo.load(ADDON, runner=demo_runner(demo))
    for slot in ("today.summary", "today.from_addons"):
        assert addon.obj.widgets(slot, _view(demo, addon, slot)) == []


def test_ticket_code_lists_prs_across_repos(demo):
    addon = _cached(demo)
    ticket = store.load(demo.ws, "DEMO-0009")[1]
    [table] = addon.obj.widgets("ticket.code", _view(demo, addon, "ticket.code", ticket))
    assert [r[0].text for r in table.rows] == ["acme-energy-data #23", "ingest #4"]
    assert table.rows[1][1] == Badge("ok", "checks passed") and table.rows[1][2] == Badge("ok", "approved")
    other = store.load(demo.ws, "DEMO-0006")[1]
    assert addon.obj.widgets("ticket.code", _view(demo, addon, "ticket.code", other)) == []


def test_rerun_failed_runs_gh_for_each_failed_run(demo):
    addon = _cached(demo)
    runner = demo_runner(demo, {"argv": ["gh", "run", "rerun", "36979598989", "--failed", "--repo", "acme/ticket-orch-demo"]})
    msg = addon.obj.act("rerun_failed", "acme/ticket-orch-demo#21", addon.ctx.provider_context(runner=runner))
    assert msg == Intent("none", reason="Rerun started for 1 failed run(s) on acme/ticket-orch-demo#21")
    assert runner.calls == [("gh", "run", "rerun", "36979598989", "--failed", "--repo", "acme/ticket-orch-demo")]


def test_mark_ready(demo):
    addon = _cached(demo)
    runner = demo_runner(demo, {"argv": ["gh", "pr", "ready", "22", "--repo", "acme/ticket-orch-demo"]})
    assert addon.obj.act("mark_ready", "acme/ticket-orch-demo#22", addon.ctx.provider_context(runner=runner)) \
        == Intent("none", reason="acme/ticket-orch-demo#22 is ready for review")


def test_mark_ready_refuses_a_non_draft(demo):
    addon = _cached(demo)
    runner = demo_runner(demo)
    with pytest.raises(OrchError, match="is not a draft"):
        addon.obj.act("mark_ready", "acme/ticket-orch-demo#23", addon.ctx.provider_context(runner=runner))
    assert runner.calls == []


@pytest.mark.parametrize("action, target, needle", [
    ("rerun_failed", "acme/ticket-orch-demo#91", "no failed GitHub Actions run"),
    ("rerun_failed", "acme/ticket-orch-demo#999", "not in the cache"),
    ("mark_ready", "nonsense", "not a pull request"),
    ("deploy", "acme/ticket-orch-demo#21", "unknown action"),
])
def test_bad_targets_are_refused(demo, action, target, needle):
    addon = _cached(demo)
    runner = demo_runner(demo)
    with pytest.raises(OrchError, match=needle):
        addon.obj.act(action, target, addon.ctx.provider_context(runner=runner))
    assert runner.calls == []


def test_gh_failure_is_reported(demo):
    addon = _cached(demo)
    runner = demo_runner(demo, {"argv": ["gh", "pr", "ready", "22", "--repo", "acme/ticket-orch-demo"], "returncode": 1,
                                "stderr": "HTTP 403: Resource not accessible by integration\n"})
    with pytest.raises(OrchError, match="HTTP 403"):
        addon.obj.act("mark_ready", "acme/ticket-orch-demo#22", addon.ctx.provider_context(runner=runner))


def test_only_https_urls_become_links():
    from github_reviews.views import MERGE, _http
    assert _http("https://github.com/a/b/pull/1") and not _http("http://github.com/a/b/pull/1")
    assert not _http("javascript:alert(1)") and not _http(None)
    assert set(MERGE) == {"conflict"}  # the provider maps mergeable to yes|conflict|unknown only


def test_filters_are_chip_rows_with_the_shown_filter_current(demo):
    addon = _cached(demo)
    out = addon.obj.widgets(PAGE, _view(demo, addon, PAGE, state="failing", repo="ingest"))
    filters = next(w for w in out if isinstance(w, Card) and w.title.startswith("Showing"))
    states, repos = filters.body
    assert isinstance(states, Chips) and isinstance(repos, Chips)
    assert [c.text.rsplit(" ", 1)[0] for c in states.items] == ["Needs your review", "Yours", "Checks failing", "Drafts", "All open"]
    assert [c.current for c in states.items] == [False, False, True, False, False]
    assert [(c.text, c.current) for c in repos.items] == [("All repos", False), ("acme-energy-data", False), ("ingest", True)]
    assert repos.items[2].url == "/addons/github-reviews/?state=failing&repo=ingest"


def test_a_ticket_key_in_the_pr_body_links_the_pr(demo):
    """#14: a PR whose branch and title name no ticket still belongs to the one its description names."""
    from github_reviews.github import pr_item
    from github_reviews.views import Data
    item = pr_item({"number": 31, "url": "https://github.com/acme/ticket-orch-demo/pull/31", "title": "Tidy",
                    "headRefName": "tidy", "body": "Some words.\n\nCloses DEMO-0006 (and see ABC-12)."},
                   host="github.com", repo="acme/ticket-orch-demo", me=None)
    assert item["body_refs"] == ["DEMO-0006", "ABC-12"]
    addon = _cached(demo)
    assert [t.id for t in Data(_view(demo, addon, PAGE)).tickets(item)] == ["DEMO-0006"]


def test_body_keys_keep_their_exact_number(demo):
    """DEMO-1 is not DEMO-12, and DEMO-1 and DEMO-0001 are the same ticket."""
    from github_reviews.github import body_refs
    from github_reviews.views import Data
    assert body_refs("see DEMO-12, DEMO-1 and demo-0001.") == ["DEMO-12", "DEMO-1", "DEMO-0001"]
    addon = _cached(demo)
    data = Data(_view(demo, addon, PAGE))
    item = {"url": "https://github.com/x/y/pull/1", "source_branch": "b", "title": "t", "body_refs": ["DEMO-1"]}
    assert [t.id for t in data.tickets(item)] == ["DEMO-0001"]
    assert [t.id for t in data.tickets({**item, "body_refs": ["DEMO-0001", "DEMO-12"]})] == ["DEMO-0001"]  # 12 does not exist

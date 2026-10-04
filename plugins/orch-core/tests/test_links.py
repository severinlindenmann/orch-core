import pytest

from orch.core.links import LinkIndex, sync_action

GH = [{"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": "https://github.com/a/b/issues/{id}"}]


def test_for_review_by_pr_url_branch_and_keys(configure, ws_root):
    ws = configure(external_trackers=GH)
    from orch.core.ops import Ops
    from orch.core.events import Actor
    ops = Ops(ws, Actor("agent", "claude-code", "cli", "s1"))
    a = ops.new("Retry").id
    ops.link(a, repo="r", pr="https://github.com/a/b/pull/19")
    b = ops.new("Late").id
    ops.link(b, repo="r", branch="feature/late-exports")
    c = ops.new("From GitHub", external="GH-12").id
    idx = LinkIndex(ws)
    assert [t.id for t in idx.for_review(url="https://github.com/a/b/pull/19")] == [a]
    assert [t.id for t in idx.for_review(branch="feature/late-exports")] == [b]
    assert [t.id for t in idx.for_review(title=f"{b.lower().replace('-000', '-')} and gh-12 together")] == [b, c]
    assert idx.for_review(title="nothing to see") == []
    assert idx.keys_in("feature/L-0002-late", "GH-12 x") == ["L-0002", "GH-12"]


def test_bare_number_trackers_do_not_match_free_text(configure, put):
    ws = configure(external_trackers=[{"prefix": "GH", "pattern": "\\d+", "url": "https://github.com/a/b/issues/{key}"}])
    tid = put("open", external=[{"key": "3", "url": None}])
    idx = LinkIndex(ws)
    assert idx.for_review(title="Retry 3 times") == []
    assert [t.id for t in idx.for_external("3")] == [tid] and idx.for_external("3")[0].status == "open"


def test_ticket_lookup_and_broken_files(ws, put):
    tid = put("open", title="Real")
    broken = ws.status_dir("open") / "L-0099-broken.md"
    broken.write_text("not a ticket\n", encoding="utf-8")
    idx = LinkIndex(ws)
    assert idx.ticket("l-1").id == tid and idx.ticket("1").title == "Real"
    assert [t.id for t in idx.tickets] == [tid]


def test_merge_order(ws, put):
    a = put("in-progress")
    b = put("in-progress", blocked_by=[a])
    c = put("in-progress")
    idx = LinkIndex(ws)
    assert idx.merge_order([b, a]) == [a, b]
    assert idx.merge_order([a, c]) is None


def test_merge_order_with_a_cycle_is_none(ws, put):
    a = put("in-progress", blocked_by=["L-0002"])
    b = put("in-progress", blocked_by=[a])
    assert LinkIndex(ws).merge_order([a, b]) is None


@pytest.mark.parametrize("category, status, want", [
    ("done", "open", "close"), ("done", "testing", "close"), ("done", "done", None),
    ("todo", "done", "reopen"), ("in_progress", "done", "reopen"), ("todo", "open", None),
])
def test_sync_action(category, status, want):
    assert sync_action(category, status) == want


def test_sync_uses_the_linked_ticket(configure, put):
    ws = configure(external_trackers=GH)
    tid = put("in-progress", external=[{"key": "GH-13", "url": None}])
    idx = LinkIndex(ws)
    hits = idx.sync("gh-13", "done")
    assert len(hits) == 1 and hits[0][0].id == tid and hits[0][1] == "close"
    assert idx.sync("GH-13", "todo") == [] and idx.sync("GH-99", "done") == []


def test_sync_is_evaluated_per_ticket_when_one_key_links_several(configure, put):
    """A key linked from more than one ticket (e.g. imported twice, or linked by hand) must show and evaluate
    every one of them, not just the first (a bug: `for_external`/`sync` used to keep only the first linker)."""
    ws = configure(external_trackers=GH)
    done = put("done", external=[{"key": "GH-13", "url": None}])
    open_ = put("open", external=[{"key": "GH-13", "url": None}])
    idx = LinkIndex(ws)
    linked = idx.for_external("GH-13")
    assert {t.id for t in linked} == {done, open_}
    hits = {t.id: action for t, action in idx.sync("GH-13", "done")}
    assert hits == {open_: "close"}  # `done` already agrees; only `open_` is out of sync

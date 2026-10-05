import pytest

pytest.importorskip("fastapi")


def test_events_grouped_and_described(ws, hops):
    from orch.dashboard.data.timeline import timeline
    t = hops.new("Timeline ticket")
    hops.log(t.id, "hello")
    page = timeline(ws)
    assert page.groups[0]["label"] == "Today"
    whats = [i["what"] for i in page.groups[0]["items"]]
    assert whats[0].startswith("logged: hello") and "created the ticket" in whats


def test_category_filter(ws, hops):
    from orch.dashboard.data.timeline import timeline
    t = hops.new("Cat ticket")
    hops.log(t.id, "note")
    assert all(i["category"] == "agents" for g in timeline(ws, category="agents").groups for i in g["items"])


def test_paging_with_before(ws, hops):
    from orch.dashboard.data.timeline import timeline
    t = hops.new("Paging")
    for n in range(5):
        hops.log(t.id, f"n{n}")
    first = timeline(ws, limit=2)
    assert first.older is not None
    second = timeline(ws, limit=2, before=first.older)
    seqs1 = {i["seq"] for g in first.groups for i in g["items"]}
    seqs2 = {i["seq"] for g in second.groups for i in g["items"]}
    assert seqs1.isdisjoint(seqs2) and max(seqs2) < min(seqs1)


def test_code_category_from_link(ws, dash, hops):
    from orch.dashboard.data.timeline import timeline
    t = hops.new("Linked ticket")
    hops.link(t.id, repo="dbt-models", branch="feature/x", pr="https://git.example/dbt-models/pull/212")
    items = [i for g in timeline(ws, category="code").groups for i in g["items"]]
    assert any(i["ticket"] == t.id for i in items)
    html = dash.get("/activity?category=code").text
    assert t.id in html and "feature/x" in html and "PR #212" in html


def test_page_and_markdown_export(dash, hops):
    t = hops.new("Exported ticket")
    html = dash.get("/activity").text
    assert "created the ticket" in html and t.id in html
    md = dash.get("/activity.md")
    assert md.headers["content-type"].startswith("text/markdown") and "created the ticket" in md.text


@pytest.mark.parametrize("q", ["?category=nope", "?before=-1", "?before=abc"])
def test_garbage_params_fall_back(dash, q):
    assert dash.get("/activity" + q).status_code == 200


def test_empty_timeline(dash):
    assert "Nothing has happened yet" in dash.get("/activity").text


def test_markdown_export_escapes_link_syntax(dash, hops):
    t = hops.new("x")
    hops.log(t.id, "see [here](javascript:alert(1)) <b>")
    md = dash.get("/activity.md").text
    assert r"\[here\]\(javascript:alert\(1\)\) \<b\>" in md
    assert "[here](" not in md and "<b>" not in md


def test_export_link_keeps_before(dash, hops):
    hops.new("x")
    html = dash.get("/activity?category=all&before=5").text
    assert 'href="/activity.md?category=all&amp;before=5"' in html
    assert 'href="/activity.md?category=all"' in dash.get("/activity").text


def test_reports_and_timeline_share_one_escape_helper():
    from orch.dashboard.data import metrics, text, timeline
    assert metrics.md_escape is text.md_escape and timeline.md_escape is text.md_escape


def _addon_event(ws, kind, data):
    from orch.core.events import append_event
    from orch.dashboard.views import HUMAN
    append_event(ws, None, kind, HUMAN, data)


def test_addon_action_reads_as_a_sentence_under_code(ws, hops):
    from orch.dashboard.data.timeline import timeline
    _addon_event(ws, "addon.action", {"addon": "github-reviews", "action": "mark_ready", "target": "DEMO-0010|GH-14|done"})
    items = [i for g in timeline(ws, category="code").groups for i in g["items"]]
    assert [i["what"] for i in items] == ["mark ready on DEMO-0010, GH-14 (github reviews)"]
    assert items[0]["ticket"] == "DEMO-0010" and "|" not in items[0]["what"]


def test_addon_decision_files_under_decisions(ws):
    from orch.dashboard.data.timeline import timeline
    _addon_event(ws, "addon.decision", {"addon": "github-issues", "decision": "x", "choice": "ignore"})
    assert len([i for g in timeline(ws, category="decisions").groups for i in g["items"]]) == 1


def test_ledger_adopted_is_hidden_from_all_but_has_its_own_filter(ws, hops):
    from orch.dashboard.data.timeline import timeline
    t = hops.new("Adopt me")
    _addon_event(ws, "ledger.adopted", {})
    allitems = [i for g in timeline(ws).groups for i in g["items"]]
    assert all(i["kind"] != "ledger.adopted" for i in allitems) and allitems
    only = [i for g in timeline(ws, category="ledger").groups for i in g["items"]]
    assert [i["kind"] for i in only] == ["ledger.adopted"]

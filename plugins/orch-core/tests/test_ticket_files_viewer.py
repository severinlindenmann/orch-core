"""Ticket page redesign: the Files panel (type icons, grouping by criterion or kind, folding), the file viewer
(Markdown, text, JSON, CSV, images from the pinned bytes only; a drawer fragment for app.js), the Activity rounds,
and the compact task rows."""
import re
from datetime import datetime, timedelta, timezone

import pytest

from orch.core import store


def _file(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


def _sha(ws, tid, name):
    return next(e["sha256"] for e in store.load(ws, tid)[1].meta["artifacts"] if e.get("name") == name)[:16]


# ---------- file types and grouping ----------

def test_file_type_comes_from_the_extension():
    from orch.dashboard.data.artifact_view import file_type
    assert file_type("concept.md", "name") == {"icon": "md", "badge": "MD", "viewer": "md"}
    assert file_type("page.html", "name")["viewer"] == "tab"
    assert file_type("check.txt", "name") == {"icon": "txt", "badge": "TXT", "viewer": "text"}
    assert file_type("lighthouse.json", "name") == {"icon": "json", "badge": "JSON", "viewer": "text"}
    assert file_type("rows.csv", "name")["viewer"] == "table"
    assert file_type("build.py", "name") == {"icon": "code", "badge": "PY", "viewer": "text"}
    assert file_type("shot.png", "name")["viewer"] == "image"
    assert file_type("bundle.zip", "name") == {"icon": "file", "badge": "ZIP", "viewer": "tab"}
    assert file_type(None, "url") == {"icon": "link", "badge": "", "viewer": "link"}


def test_human_size():
    from orch.dashboard.data.artifact_view import human_size
    assert [human_size(n) for n in (None, 458, 7599, 1024, 3 * 1024 * 1024)] == ["", "458 B", "7.4 KB", "1 KB", "3.0 MB"]


def test_panel_groups_by_criterion_in_testing_and_by_kind_otherwise(ws, aops, working, tmp_path):
    from orch.dashboard.data import artifact_view
    aops.artifact_add(working, _file(tmp_path, "concept.md", "# hi"), ac=1, label="Concept")
    aops.artifact_add(working, _file(tmp_path, "check.txt", "OK"), ac=2, label="Check")
    aops.artifact_add(working, _file(tmp_path, "notes.txt", "n"), label="Notes")
    t = store.load(ws, working)[1]
    assert artifact_view.view(ws, t)["mode"] == "type"  # in progress
    t.meta["status"] = "testing"
    v = artifact_view.view(ws, t)
    assert v["mode"] == "ac"
    assert [(g["title"], g["count"]) for g in v["files"]] == [("AC1", 1), ("AC2", 1), ("Not tied to a criterion", 1)]
    assert [g["title"] for g in artifact_view.view(ws, t, "type")["files"]] == ["Reports", "Logs"]
    row = v["files"][0]["rows"][0]
    assert row["icon"] == "md" and row["size"] == "4 B"
    assert row["view"] == f"/t/{working}/view/concept.md?v={_sha(ws, working, 'concept.md')}"


def test_a_long_group_folds_after_four_rows(ws, aops, working, tmp_path):
    from orch.dashboard.data import artifact_view
    for n in range(7):
        aops.artifact_add(working, _file(tmp_path, f"log{n}.txt", str(n)))
    g = artifact_view.view(ws, store.load(ws, working)[1])["files"][0]
    assert len(g["rows"]) == 4 and len(g["more"]) == 3 and g["count"] == 7


def test_panel_rows_have_icons_and_open_in_the_viewer_or_a_new_tab(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _file(tmp_path, "concept.md", "# hi"), ac=1, label="Konzept")
    aops.artifact_add(working, _file(tmp_path, "page.html", "<p>x</p>"), ac=1, label="Page")
    html = dash.get(f"/t/{working}").text
    panel = html.split('id="artifacts"', 1)[1].split('<section class="card act-card"', 1)[0]
    md = panel.split('<span class="fi fi-md" aria-hidden="true">MD</span>', 1)[0].rsplit("<a ", 1)[1]
    assert f'href="/t/{working}/view/concept.md?v=' in md and "data-viewer" in md
    page = panel.split('<span class="fi fi-html" aria-hidden="true">HTML</span>', 1)[0].rsplit("<a ", 1)[1]
    assert f'href="/a/{working}/page.html?v=' in page and 'target="_blank"' in page and "data-viewer" not in page


def test_the_grouping_toggle_is_a_link_and_shown_only_when_a_file_proves_a_criterion(dash, aops, working, tmp_path):
    aops.artifact_add(working, _file(tmp_path, "a.txt", "a"))
    assert 'class="seg-toggle"' not in dash.get(f"/t/{working}").text
    aops.artifact_add(working, _file(tmp_path, "b.txt", "b"), ac=1)
    html = dash.get(f"/t/{working}?files=ac").text
    assert '<a href="?files=ac#artifacts" aria-current="true">By criterion</a>' in html
    assert '<h3 class="artifact-kind" id="art-g-ac1">AC1' in html


# ---------- the viewer ----------

def test_viewer_renders_markdown_escaped(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _file(tmp_path, "concept.md", "# Title\n\n<script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"))
    v = _sha(ws, working, "concept.md")
    r = dash.get(f"/t/{working}/view/concept.md?v={v}")
    assert r.status_code == 200
    body = r.text.split('class="viewer-body', 1)[1]
    assert "<h1" in body and "<table" in body and "<script>alert(1)" not in body


def test_viewer_shows_text_with_line_numbers_and_pretty_json(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _file(tmp_path, "check.txt", "one\n<two>\n"))
    aops.artifact_add(working, _file(tmp_path, "r.json", '{"a":{"b":1}}'))
    txt = dash.get(f"/t/{working}/view/check.txt?v={_sha(ws, working, 'check.txt')}").text
    assert '<li id="L2"><span class="ln" aria-hidden="true">2</span><code>&lt;two&gt;</code></li>' in txt
    assert "2 lines" in txt
    js = dash.get(f"/t/{working}/view/r.json?v={_sha(ws, working, 'r.json')}").text
    assert '<code>  &#34;a&#34;: {</code>' in js or '<code>  &quot;a&quot;: {</code>' in js


def test_viewer_shows_csv_as_a_table(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _file(tmp_path, "rows.csv", "name,count\nalpha,1\n<b>,2\n"))
    html = dash.get(f"/t/{working}/view/rows.csv?v={_sha(ws, working, 'rows.csv')}").text
    table = html.split('<table class="viewer-table">', 1)[1].split("</table>", 1)[0]
    assert '<th scope="col">name</th>' in table and "<td>alpha</td>" in table and "<td>&lt;b&gt;</td>" in table


def test_viewer_partial_is_a_fragment_with_steps(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _file(tmp_path, "a.txt", "a"))
    aops.artifact_add(working, _file(tmp_path, "b.md", "b"))
    r = dash.get(f"/t/{working}/view/a.txt?v={_sha(ws, working, 'a.txt')}&partial=1")
    assert r.status_code == 200 and "<html" not in r.text and r.headers["cache-control"] == "no-store"
    assert "data-viewer-close" in r.text and "1 of 2" in r.text
    assert f'href="/t/{working}/view/b.md?v={_sha(ws, working, "b.md")}" data-viewer data-viewer-next' in r.text


def test_viewer_image_mode_uses_the_pinned_route(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _file(tmp_path, "shot.png", b"\x89PNG-x"), label="Home page")
    v = _sha(ws, working, "shot.png")
    html = dash.get(f"/t/{working}/view/shot.png?v={v}").text
    assert f'<figure class="viewer-image"><img src="/a/{working}/shot.png?v={v}" alt="Home page"' in html


@pytest.mark.parametrize("path", ["concept.md?v=0000000000000000", "concept.md", "other.md?v={v}",
                                  "../concept.md?v={v}", "page.html?v={h}"])
def test_viewer_refuses_what_the_ticket_does_not_link_as_viewable(dash, ws, aops, working, tmp_path, path):
    aops.artifact_add(working, _file(tmp_path, "concept.md", "# hi"))
    aops.artifact_add(working, _file(tmp_path, "page.html", "<p>x</p>"))
    url = f"/t/{working}/view/" + path.format(v=_sha(ws, working, "concept.md"), h=_sha(ws, working, "page.html"))
    assert dash.get(url).status_code == 404


def test_viewer_refuses_a_file_changed_since_it_was_linked(dash, ws, aops, working, tmp_path):
    aops.artifact_add(working, _file(tmp_path, "check.txt", "before"))
    v = _sha(ws, working, "check.txt")
    (ws.artifacts_dir / working / "check.txt").write_text("after", encoding="utf-8")
    r = dash.get(f"/t/{working}/view/check.txt?v={v}")
    assert r.status_code in (404, 409) and "after" not in r.text


def test_the_viewer_route_is_a_read_for_remote_devices():
    from orch.dashboard.remote_gate import LOOK, TAGS
    assert TAGS[("GET", "/t/{ref}/view/{name:path}")] is LOOK


# ---------- activity ----------

class _E:
    def __init__(self, kind, at, actor="agent:claude-code:s1", **data):
        self.kind, self.at, self.actor, self.data = kind, at, actor, data


def _at(minutes):
    return (datetime(2026, 10, 7, 6, 30, tzinfo=timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_activity_splits_rounds_at_status_changes_and_folds_runs():
    from orch.dashboard.data.story import activity
    ev = [_E("ticket.created", _at(0), title="x"),
          _E("gate.approved", _at(3), actor="human:me", gate="requirements"),
          _E("ticket.moved", _at(50), **{"from": "open", "to": "in-progress"}),
          *[_E("artifact.added", _at(57), name=f"f{n}.txt") for n in range(3)],
          _E("task.moved", _at(58), task="T1", now="done"), _E("task.moved", _at(58), task="T2", now="done"),
          _E("ticket.moved", _at(60), **{"from": "in-progress", "to": "testing"}),
          _E("verdict.given", _at(62), actor="human:me", verdict="follow-up"),
          _E("ticket.moved", _at(62), **{"from": "testing", "to": "in-progress"}),
          _E("ticket.moved", _at(70), **{"from": "in-progress", "to": "testing"})]
    rounds = activity(ev, now=datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc))
    assert [(r["label"], r["round"]) for r in rounds] == [("Backlog", 1), ("In progress", 1), ("Testing", 1),
                                                         ("In progress", 2), ("Testing", 2)]
    first = rounds[0]["rows"]
    assert first[1]["human"] and first[1]["who"] == "you"
    work = rounds[1]
    assert work["elapsed"] == "10 min"
    folded = work["rows"][1]
    assert folded["what"] == "added 3 files" and len(folded["events"]) == 3
    assert work["rows"][2]["what"] == "finished 2 tasks (T1, T2)"
    # a row shows its time only when the minute changes (local time, as the page shows it)
    assert work["rows"][2]["time"] and rounds[2]["rows"][1]["time"]
    same = activity([_E("ticket.created", _at(0)), _E("log.added", _at(0), text="a")])[0]["rows"]
    assert same[0]["time"] and same[1]["time"] == ""
    assert rounds[-1].get("open") and rounds[-1]["elapsed"] == "1 h 20"


def test_activity_card_marks_your_actions(dash, aops, working):
    html = dash.get(f"/t/{working}").text
    act = html.split('<section class="card act-card"', 1)[1].split("</section>", 1)[0]
    assert '<h2 id="timeline-h">Activity</h2>' in act
    assert re.search(r'class="act-row act-you".*?<b>you</b> approved the requirements', act, re.S)


# ---------- tasks and the status card ----------

def test_task_rows_show_short_criterion_chips_and_one_after_approval_note(dash, aops, working, plan_approved):
    plan_approved(working)
    aops.task_add(working, [{"text": "the work", "refs": ["ac:1"]}])
    html = dash.get(f"/t/{working}?open=all").text
    tasks = html.split('id="tasks"', 1)[1].split("</section>", 1)[0]
    assert re.search(r'class="chip chip-neu task-ref" href="#proven" title="AC 1 · every job on serverless">.*?AC1', tasks, re.S)
    assert "All 1 tasks were added after the plan approval." in tasks and "added after approval</span>" not in tasks


def test_left_is_a_line_in_the_status_card(dash, aops, working):
    html = dash.get(f"/t/{working}").text
    card = html.split('class="status-card', 1)[1].split("</section>", 1)[0]
    assert '<p class="muted ym-left"><b>Left:</b>' in card and 'id="left"' not in html

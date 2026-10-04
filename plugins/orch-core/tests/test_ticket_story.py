"""The ticket page as a story (#16, ticket design review §3.4): chapters, full gated text before every Approve,
the re-approve diff, proof per criterion, attributed agent notes, a folded timeline and no browser popups."""
import re
from pathlib import Path

import pytest

from orch.core import store

pytest.importorskip("fastapi")

STATIC = Path(__file__).resolve().parents[1] / "src" / "orch" / "dashboard" / "static"
REQ = {"Requirements": "- r1\n- r2", "Acceptance criteria": "- [ ] first\n- [ ] second", "Out of scope": "- nope"}


def _page(dash, tid, query=""):
    return dash.get(f"/t/{tid}{query}").text


def _chapter(html, key):
    return html.split(f'id="{key}"', 1)[1].split("</details>\n", 1)[0]


def test_chapters_in_story_order_and_the_current_one_open(dash, put):
    tid = put("backlog", sections=REQ)
    html = _page(dash, tid)
    keys = re.findall(r'<details class="chapter chapter-(\w+)', html)
    assert keys == ["asked", "agreed", "doing", "proven", "left"]
    assert re.search(r'<details class="chapter chapter-agreed chapter-current" id="agreed" open', html)
    assert not re.search(r'id="doing" open', html)


def test_open_all_opens_every_chapter(dash, put):
    tid = put("open", sections=REQ)
    html = _page(dash, tid, "?open=all")
    assert len(re.findall(r'<details class="chapter[^>]*data-wide-open', html)) == 5
    assert all(re.search(rf'id="{k}" open', html) for k in ("asked", "agreed", "doing", "proven", "left"))


RICH = {
    "Requirements": "Intro prose about the job.\n\n- plain req bullet\n  - nested req detail\n- [ ] boxed req",
    "Acceptance criteria": "- [ ] first criterion\n  - nested ac detail\n- plain ac bullet\n\nAc closing prose.\n- [x] second criterion",
    "Out of scope": "Scope prose.\n- oos bullet\n  - nested oos detail",
}
PLAN = "Plan prose first.\n\n1. numbered step\n   - nested plan detail\n- [ ] boxed plan step\n  - nested boxed detail"


def _all_visible_before(block, approve, needles):
    for text in needles:
        assert text in block and block.index(text) < approve, text


def test_every_gated_section_is_shown_in_full_before_approve(dash, put):
    """Everything the hash binds is on the page before Approve: prose, plain and nested bullets, not only the
    top-level checkboxes (review blocker)."""
    tid = put("backlog", sections=RICH)
    gate = _page(dash, tid).split('id="gate-requirements"', 1)[1].split("</section>", 1)[0]
    approve = gate.index(f'action="/t/{tid}/approve"')
    _all_visible_before(gate, approve, [line.strip("- []x").strip() for text in RICH.values()
                                        for line in text.split("\n") if line.strip()])
    assert "Requirements · full text" in gate and "sha256 " in gate and "AC1–AC2" in gate


def test_the_whole_plan_is_shown_before_approve(dash, aops, working):
    aops.set_section(working, "Plan", PLAN)
    gate = _page(dash, working).split('id="gate-plan"', 1)[1].split("</section>", 1)[0]
    approve = gate.index(f'action="/t/{working}/approve"')
    _all_visible_before(gate, approve, ["Plan prose first.", "numbered step", "nested plan detail", "boxed plan step",
                                        "nested boxed detail"])


def test_reapprove_shows_a_diff_against_the_approved_snapshot(dash, hops, aops, working):
    aops.set_section(working, "Plan", "1. migrate the jobs")
    hops.approve(working, "plan")
    aops.set_section(working, "Plan", "1. migrate the jobs\n2. compare the cost")
    gate = _page(dash, working).split('id="gate-plan"', 1)[1].split("</section>", 1)[0]
    assert "Changed since you approved" in gate
    assert '<span class="diff-add">+ 2. compare the cost</span>' in gate
    assert "Re-approve plan" in gate and gate.index("diff-add") < gate.index('action="/t/')


def test_proven_lists_each_criterion_with_its_evidence_and_the_verdict(dash, put):
    tid = put("testing", sections={**REQ, "Verification": "- AC2: pytest 3 passed\n- ruff clean"})
    proven = _chapter(_page(dash, tid), "proven")
    assert re.search(r'<details class="chapter chapter-proven chapter-current" id="proven" open', _page(dash, tid))
    assert "AC 1/2" in proven and "pytest 3 passed" in proven and "No evidence yet" in proven
    assert "Other evidence" in proven and "ruff clean" in proven
    assert f'action="/t/{tid}/verdict"' in proven


def test_summary_is_rendered_first_and_attributed(dash, put):
    tid = put("backlog", sections={**REQ, "Summary": "- Retry downloads before alerting", "Ask": "please"})
    html = _page(dash, tid)
    assert html.index("Agent's summary") < html.index("Retry downloads before alerting") < html.index('id="asked"')


def test_agent_notes_are_folded_and_attributed(dash, aops, put, working):
    aops.set_state(working, "T1 done, T2 next")
    aops.set_section(working, "Findings", "a flaky test")
    html = _page(dash, working)
    notes = html.split('id="notes"', 1)[1].split("</details>", 1)[0]
    assert "Handoff" in notes and "claude-code wrote" in notes and "T1 done, T2 next" in notes and "Findings" in notes
    assert notes.index("Handoff") < notes.index("Findings")


def test_timeline_folds_runs_of_edits():
    from orch.core.events import Event
    from orch.dashboard.data.story import timeline
    ev = [Event(1, "2026-10-01T09:00Z", "L-1", "ticket.created", "agent:claude-code:s", "cli", {}),
          Event(2, "2026-10-01T09:01Z", "L-1", "ticket.edited", "agent:claude-code:s", "cli", {"section": "Requirements"}),
          Event(3, "2026-10-01T09:02Z", "L-1", "ticket.edited", "agent:claude-code:s", "cli", {"section": "Acceptance criteria"}),
          Event(4, "2026-10-01T09:03Z", "L-1", "ticket.edited", "agent:claude-code:s", "cli", {"section": "Requirements"}),
          Event(5, "2026-10-01T09:04Z", "L-1", "gate.approved", "human:you", "dashboard", {"gate": "requirements"}),
          Event(6, "2026-10-01T09:05Z", "L-1", "ticket.edited", "agent:claude-code:s", "cli", {"pr": "https://x/pull/2"})]
    rows = timeline(ev)
    assert [r["what"] for r in rows] == ["linked PR #2 / https://x/pull/2", "approved the requirements",
                                         "edited Requirements, Acceptance criteria · 3 edits", "created the ticket"]


def test_start_agent_panel_only_when_an_agent_can_start(dash, put):
    from orch.clock import stamp
    ready = put("open", sections=REQ)
    html = _page(dash, ready)
    assert html.count('class="start-agent"') == 1 and "Start another agent" not in html
    busy = put("in-progress", sections=REQ, claim={"harness": "claude-code", "session": "s1", "at": stamp()})
    html = _page(dash, busy)
    menu = html.split('id="more"', 1)[1].split("</details>\n  </div>", 1)[0]
    assert "Start another agent" in menu and 'class="start-agent"' in menu


def test_move_raw_upload_and_release_live_in_the_more_menu(dash, put):
    from orch.clock import stamp
    tid = put("in-progress", sections=REQ, claim={"harness": "claude-code", "session": "s1", "at": stamp()})
    menu = _page(dash, tid).split('id="more"', 1)[1]
    menu = menu[:menu.index('<header class="ticket-head')]
    for needle in (f'/t/{tid}/raw', f'action="/t/{tid}/move"', f'action="/t/{tid}/artifacts"',
                   f'action="/t/{tid}/release"', f'action="/t/{tid}/comment"'):
        assert needle in menu, needle


def test_the_ticket_page_never_asks_with_a_browser_popup(dash, put):
    for status in ("backlog", "in-progress", "testing"):
        tid = put(status, sections={**REQ, "Plan": "1. x", "Verification": "- AC1: y"},
                  claim={"harness": "x", "session": "s", "at": "2020-01-01T00:00Z"})
        html = _page(dash, tid)
        assert "data-confirm=" not in html, status
    tid = put("testing", sections=REQ)
    assert 'data-inline-confirm="Confirm · close' in _page(dash, tid)


def test_app_js_has_the_inline_confirm():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "form[data-inline-confirm]" in js and "\"Escape\"" in js and "ARM_MS = 6000" in js and "event.repeat" in js
    assert "focus()" in js and "window.confirm" not in js  # no browser popup anywhere (#17)


def test_story_reuses_the_request_scan(dash, ws, put, monkeypatch):
    from orch.core import store
    tid = put("in-progress", sections={**REQ, "Plan": "1. a", "Tasks": "- [ ] T1 a"})
    dash.get(f"/t/{tid}")
    real, calls = store._scan, []
    monkeypatch.setattr(store, "_scan", lambda ws_: calls.append(1) or real(ws_))
    assert tid in dash.get(f"/t/{tid}").text and len(calls) == 1


def test_design_gallery_shows_the_ticket_card_at_three_sizes(dash):
    html = dash.get("/design").text
    assert "Ticket card · S row" in html and 'class="tc-row"' in html
    assert 'class="card tcard"' in html and 'class="tc-header"' in html


def test_evidence_is_labelled_with_its_author(dash, aops, working):
    aops.set_section(working, "Verification", "- AC1: databricks jobs list shows 14 serverless jobs")
    proven = _chapter(_page(dash, working), "proven")
    assert "evidence by claude-code" in proven and "14 serverless jobs" in proven


def test_heading_outline_is_h1_h2_h3(dash, put):
    """Each chapter's summary holds an h2, gates are h3: h1 → h2 → h3 without skipping a level (review a11y)."""
    tid = put("backlog", sections={**REQ, "Context": "found it"})
    html = _page(dash, tid)
    story = html.split('class="story"', 1)[1].split("<aside", 1)[0]
    assert re.findall(r'<h2 class="chapter-title">([^<]+)</h2>', story) == ["Asked", "What we agreed", "Doing", "Proof so far", "Left"]
    levels = [int(n) for n in re.findall(r"<h([1-6])[ >]", html.split("<main", 1)[1])]
    assert levels[0] == 1 and all(b - a <= 1 for a, b in zip(levels, levels[1:]))


def test_board_search_results_are_compact_rows(dash, put):
    put("open", title="Alpha", sections={"Ask": "needle"})
    put("open", title="Beta")
    html = dash.get("/board?q=needle").text
    results = html.split('class="card search-results"', 1)[1].split("</section>", 1)[0]
    assert 'class="tc-row"' in results and "Alpha" in results and "Beta" not in html and 'class="board"' not in html
    assert "1 ticket matches" in results


def test_reapprove_diff_has_no_blank_context_lines():
    from orch.dashboard.data.story import diff
    rows = diff("## Plan\n\n1. a\n\n2. b", "## Plan\n\n1. a\n\n2. c")
    assert all(r["text"].strip() for r in rows if r["kind"] == "ctx")
    assert {"kind": "add", "text": "2. c"} in rows and {"kind": "del", "text": "2. b"} in rows


# -- merge with security (hash v2, ledger, hidden characters, open-question override) -----------------------------

def _gate_parts_shown(html, gate):
    block = html.split(f'id="gate-{gate}"', 1)[1].split("</section>", 1)[0]
    return block, re.findall(r'data-gate-part="([^"]+)"', block)


def test_the_approve_block_shows_exactly_what_the_v2_hash_covers(dash, ws, put):
    from orch.core.gates import gate_covers, gate_meta, gate_parts
    tid = put("backlog", type="bug", sections={**REQ, "Summary": "- retry before alerting"})
    t = store.load(ws, tid)[1]
    block, shown = _gate_parts_shown(_page(dash, tid), "requirements")
    assert shown == gate_covers("requirements", t) == ["Summary", "Requirements", "Acceptance criteria",
                                                        "Out of scope", "size", "type"]
    approve = block.index(f'action="/t/{tid}/approve"')
    for name, _ in gate_parts(t, "requirements"):
        for line in t.section(name).split("\n"):
            text = line.strip("- []x").strip()
            assert block.index(text) < approve, (name, text)
    for key, value in gate_meta(t, "requirements"):
        assert f"{key}: <b>{value}</b>" in block


def test_a_v1_approval_shows_what_the_v1_hash_covered(dash, ws, put):
    from orch.core.gates import gate_covers, gate_hash
    tid = put("open", sections={**REQ, "Summary": "- written after the approval"})
    path, t = store.load(ws, tid)
    t.meta["gates"]["requirements"] = {"approved": "2026-10-01T09:00Z", "via": "dashboard",
                                       "hash": gate_hash(t, "requirements", 1), "hash_v": 1}
    store.save(ws, t, path)
    _, shown = _gate_parts_shown(_page(dash, tid), "requirements")
    assert shown == gate_covers("requirements", t, 1) == ["Requirements", "Acceptance criteria", "Out of scope"]


def test_an_open_question_needs_the_override_checkbox(dash, aops, working):
    aops.set_section(working, "Plan", "1. migrate\n2. Open question for the human: which region?")
    block = _page(dash, working).split('id="gate-plan"', 1)[1].split("</section>", 1)[0]
    form = block.split(f'action="/t/{working}/approve"', 1)[1].split("</form>", 1)[0]
    assert 'name="despite_open_question" value="1" required' in form and "which region?" in form


def test_an_approval_missing_from_the_ledger_is_marked_unsigned(dash, ws, put):
    from orch.core.gates import gate_hash
    tid = put("open", sections=REQ)
    path, t = store.load(ws, tid)
    t.meta["gates"]["requirements"] = {"approved": "2026-10-01T09:00Z", "via": "dashboard",
                                       "hash": gate_hash(t, "requirements"), "hash_v": 2}
    store.save(ws, t, path)
    head = _page(dash, tid).split('id="gate-requirements"', 1)[1].split('class="gate-head"', 1)[1].split("</div>", 1)[0]
    assert "unsigned" in head and "orch ledger adopt" in head


def test_signed_approvals_carry_no_badge(dash, hops, aops, working):
    head = _page(dash, working).split('id="gate-requirements"', 1)[1].split('class="gate-head"', 1)[1].split("</div>", 1)[0]
    assert "approved" in head and "unsigned" not in head


def test_hidden_characters_in_gated_text_show_as_badges(dash, put):
    tid = put("backlog", sections={**REQ, "Requirements": "- pay ‮exe.txt\n- r2"})
    block = _page(dash, tid).split('id="gate-requirements"', 1)[1].split("</section>", 1)[0]
    assert "hidden-char" in block and "‮" not in block

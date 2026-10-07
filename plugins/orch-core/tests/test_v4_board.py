"""M (v4 Board): the Your move strip acts in place only where the full text is on the card (answers, a short plan, a
verdict after its whole proof is expanded) and links to the full-text approve view otherwise; the agent flow draws
empty lanes as rails and Done as this week's count; cards say their progress in words and bars; the Backlog is a
drawer with its priorities; Group by keeps the strip on top."""
import re

import pytest

pytest.importorskip("fastapi")

from orch.core import store  # noqa: E402
from orch.core.gates import gate_hash  # noqa: E402


def _ym(html: str, tid: str) -> str:
    m = re.search(rf'<article class="card ym-card" data-ym="{tid}".*?</article>', html, re.S)
    assert m, f"no strip card for {tid}"
    return m.group(0)


def _card(html: str, tid: str) -> str:
    return re.search(rf'<a class="card tcard" href="/t/{tid}".*?</a>', html, re.S).group(0)


def test_an_answer_is_numbered_buttons_bound_to_the_question(dash, ws, working, aops):
    aops.ask(working, [{"text": "Rotate every 90 or 180 days?", "options": ["90 days", "180 days"], "recommended": "A"}])
    card = _ym(dash.get("/board").text, working)
    q = store.load(ws, working)[1].meta["questions"][0]
    from orch.core.questions import question_hash
    assert f'action="/t/{working}/answer"' in card and f'name="qhash" value="{question_hash(q)}"' in card
    assert 'data-option="1"' in card and 'data-option="2"' in card and "data-delayed-send=" in card
    assert 'name="next" value="/board"' in card


def test_a_short_plan_is_shown_in_full_and_approved_with_its_hash(dash, ws, working, aops):
    aops.set_section(working, "Plan", "1. query system tables\n2. build the dashboard")
    card = _ym(dash.get("/board").text, working)
    assert "query system tables" in card and "build the dashboard" in card and 'data-gate-part="Plan"' in card
    seen = re.search(r'name="seen" value="([^"]+)"', card).group(1)
    assert seen == gate_hash(store.load(ws, working)[1], "plan")
    assert 'action="/t/' + working + '/approve"' in card and "data-inline-confirm=" in card
    r = dash.post(f"/t/{working}/approve", data={"gate": "plan", "seen": seen, "next": "/board"}, follow_redirects=False)
    assert r.status_code == 303
    assert store.load(ws, working)[1].meta["gates"]["plan"]["approved"]


@pytest.mark.parametrize("plan", ["\n".join(f"{i}. step number {i} with some words to read" for i in range(1, 30)),
                                  "1. do it\n2. Open question for the human: keep the old view?"])
def test_a_long_plan_or_one_with_an_open_question_opens_its_full_text(dash, working, aops, plan):
    aops.set_section(working, "Plan", plan)
    card = _ym(dash.get("/board").text, working)
    assert "/approve" not in card and 'name="seen"' not in card
    assert f'href="/t/{working}#gate-plan"' in card and "Review &amp; approve" in card


def test_requirements_open_the_full_text_approve_view(dash, put):
    tid = put("backlog", sections={"Requirements": "- one\n- two", "Acceptance criteria": "- [ ] a"})
    card = _ym(dash.get("/board").text, tid)
    assert f'href="/t/{tid}#gate-requirements"' in card and "/approve" not in card and 'name="seen"' not in card
    assert "2 req" in card


def test_requirements_and_plan_together_link_to_the_combined_view(dash, aops):
    t = aops.new("Weekly report as CSV export")
    aops.set_section(t.id, "Requirements", "- export as CSV")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] one row per job")
    aops.set_section(t.id, "Plan", "1. add the writer\n2. add the button")
    card = _ym(dash.get("/board").text, t.id)
    assert f'href="/t/{t.id}#approve-together"' in card and "Approve requirements + plan" in card
    assert "1 req · 2 steps" in card and "/approve" not in card


def test_a_pinned_image_is_the_thumbnail(dash, ws, aops, tmp_path):
    t = aops.new("Export dialog")
    shot = tmp_path / "dialog.png"
    shot.write_bytes(b"\x89PNG-dialog")
    aops.artifact_add(t.id, shot)
    aops.set_section(t.id, "Requirements", "- export\n\n![Export dialog](artifact:dialog.png)")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    sha = store.load(ws, t.id)[1].meta["artifacts"][0]["sha256"]
    card = _ym(dash.get("/board").text, t.id)
    assert f'<img src="/a/{t.id}/dialog.png?v={sha[:16]}" alt="Export dialog"' in card


def test_accept_sits_inside_the_proof_drawer(dash, ws, put):
    from orch.core.epics import verdict_hash
    tid = put("testing", sections={"Acceptance criteria": "- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d",
                                   "Verification": "- AC1: query log clean\n- AC2: dropped in int"})
    card = _ym(dash.get("/board").text, tid)
    marker = f'<dialog class="proof-drawer" id="proof-{tid}"'
    assert marker in card
    proof = card.split(marker, 1)[1]
    before = card.split(marker, 1)[0]
    assert 'value="done"' not in before and "/verdict" not in before  # nothing to accept outside the drawer
    assert f'href="/t/{tid}#proven" data-proof-open="{tid}"' in before  # the button; a plain link without JS
    assert proof.index("AC4") < proof.index('value="done"')  # every criterion, then Accept
    assert "query log clean" in proof and "Verification" in proof
    assert f'name="seen" value="{verdict_hash([store.load(ws, tid)[1]], ws)}"' in proof
    assert "data-inline-confirm=" in proof and "+1" in before  # three tiles, then "+1"
    for tab in ("criteria", "deliverables", "verification", "handoff"):
        assert f'data-pd-tab="{tab}"' in proof and f'data-pd-panel="{tab}"' in proof
    assert "<details" not in before  # the card itself no longer grows with the proof


def test_the_drawer_pins_what_to_check_from_the_summary_widget(dash, put):
    summary = ('```orch\n{"type": "summary", "delivered": "ten drafts", "check_first": "Open index.html first",'
               ' "open": ["placeholder URL"]}\n```')
    tid = put("testing", sections={"Acceptance criteria": "- [ ] a", "Verification": "- AC1: ok",
                                   "Current state": summary})
    card = _ym(dash.get("/board").text, tid)
    assert 'aria-label="What to check"' in card and "Open index.html first" in card and "Open: placeholder URL" in card


def test_the_drawer_pins_the_first_handoff_lines_without_a_summary(dash, put):
    tid = put("testing", sections={"Acceptance criteria": "- [ ] a", "Verification": "- AC1: ok",
                                   "Current state": "- Landing page URL is a placeholder\n- Sign-off needed first"})
    card = _ym(dash.get("/board").text, tid)
    assert "Landing page URL is a placeholder" in card and "Sign-off needed first" in card


def test_a_card_tile_falls_back_to_the_criterions_linked_artifact(dash, ws, aops, put, tmp_path):
    tid = put("testing", sections={"Acceptance criteria": "- [ ] a\n- [ ] b", "Verification": "no ac cited here"})
    shot = tmp_path / "ac1.png"
    shot.write_bytes(b"\x89PNG-ac1")
    page = tmp_path / "landing.html"
    page.write_text("<p>hi</p>")
    aops.artifact_add(tid, shot, ac=1)
    aops.artifact_add(tid, page, ac=2)
    sha = store.load(ws, tid)[1].meta["artifacts"][0]["sha256"]
    card = _ym(dash.get("/board").text, tid).split('<dialog', 1)[0]
    assert f'<img src="/a/{tid}/ac1.png?v={sha[:16]}"' in card  # the linked image is the tile
    assert 'class="ev-img ev-file"' in card and ">HTML<" in card  # an HTML artifact: a typed tile that links it


def test_more_than_eight_moves_link_to_the_list(dash, put):
    for i in range(10):
        put("testing", title=f"Verdict {i}", sections={"Verification": "ok"})
    html = dash.get("/board").text
    assert html.count('<article class="card ym-card"') == 8
    assert "+2 more in the List" in html


def test_empty_lanes_are_rails_and_done_counts_this_week(dash, ws, put):
    from datetime import timedelta

    from orch.clock import now, stamp
    put("in-progress", title="Busy")
    put("done", title="Fresh", updated=stamp())
    old = put("done", title="Old")
    path = store.resolve(ws, old).path  # store.save stamps `updated`: age the file by hand
    text = path.read_text(encoding="utf-8")
    fresh = store.load(ws, old)[1].meta["updated"]
    path.write_text(text.replace(f"updated: {fresh}", f"updated: {stamp(now() - timedelta(days=30))}", 1)
                    .replace(f"updated: '{fresh}'", f"updated: '{stamp(now() - timedelta(days=30))}'", 1), encoding="utf-8")
    html = dash.get("/board").text
    for name in ("Ready", "Waiting", "Testing"):
        assert re.search(rf'<section class="col lane lane-rail" id="col-[a-z-]+" data-status="[a-z-]+" aria-label="{name}: no tickets">', html)
    assert 'aria-label="1 done in the last 7 days: open them in the List"' in html


def test_cards_say_progress_in_words_and_bars(dash, ws, working, aops, plan_approved, tmp_path):
    plan_approved(working)
    aops.task_add(working, [{"text": "one"}, {"text": "two"}])
    shot = tmp_path / "s.png"
    shot.write_bytes(b"x")
    aops.artifact_add(working, shot)
    card = _card(dash.get("/board").text, working)
    assert "Tasks 0/2" in card and 'class="task-bar" role="img"' in card
    assert "AC 0/2" in card and "1 artifact" in card
    assert "R<" not in card and "strip-gate" not in card  # no cryptic gate codes on the Board card
    listing = dash.get("/board?view=list").text
    assert "strip-gate" in listing  # the compact rows keep the strip, with words for screen readers


def test_backlog_drawer_shows_priority_counts(dash, put):
    put("backlog", title="A", priority="high")
    put("backlog", title="B", priority="normal")
    put("backlog", title="C", priority="normal")
    html = dash.get("/board").text
    drawer = re.search(r'<section class="col lane col-backlog backlog-drawer".*?</summary>', html, re.S).group(0)
    assert "high 1" in drawer and "normal 2" in drawer and 'class="chip chip-warn"' in drawer


def test_group_by_keeps_the_strip_on_top(dash, put):
    tid = put("testing", title="Check", labels=["ops"], sections={"Verification": "ok"})
    html = dash.get("/board?group=label").text
    assert html.count('id="your-move"') == 1 and html.index('id="your-move"') < html.index('class="swimlane"')
    assert f'data-ym="{tid}"' in html and 'id="col-testing-1"' in html
    dash.get("/board?group=none")


def test_initials():
    from orch.dashboard.views import initials
    assert initials("claude-code") == "CC" and initials("copilot") == "CO" and initials("") == "?"


# -- fix round 3 -----------------------------------------------------------------------------------------------------

def test_an_unsigned_ticket_is_never_acted_on_in_the_strip(dash, ws, put):
    """R25: an approval the ledger on this machine does not hold (written into the file) shows the unsigned chip, and
    the strip offers only the ticket page: no inline plan approval, no inline Accept."""
    from orch.clock import stamp
    for status, sections in (("in-progress", {"Plan": "1. do it"}), ("testing", {"Verification": "- AC1: ok"})):
        tid = put(status, sections={"Requirements": "- r", "Acceptance criteria": "- [ ] a", **sections},
                  claim={"harness": "claude-code", "session": f"s-{status}", "at": stamp()})
        path, t = store.load(ws, tid)
        t.meta["gates"] = {"requirements": {"approved": stamp(), "hash": gate_hash(t, "requirements"), "by": "human:you"}}
        store.save(ws, t, path)
        card = _ym(dash.get("/board").text, tid)
        assert "unsigned requirements" in card and 'class="chip chip-warn"' in card
        assert "<form" not in card and 'name="seen"' not in card and "ym-proof" not in card
        assert f'<a class="btn btn-primary" href="/t/{tid}">Open the ticket</a>' in card


def test_the_strip_acts_on_the_move_its_chip_names(dash, ws, working, aops):
    """The chip and the form name the same move: the ticket's move is the question (answer before plan)."""
    aops.set_section(working, "Plan", "1. do it")
    aops.ask(working, [{"text": "Which schema?", "options": ["ops", "tmp"]}])
    card = _ym(dash.get("/board").text, working)
    chip = re.search(r'<span class="chip chip-you">.*?</svg> ([^<]+)</span>', card).group(1)
    assert chip.startswith("Answer") and 'data-move="answer"' in card
    assert f'action="/t/{working}/answer"' in card and f'action="/t/{working}/approve"' not in card


def test_more_questions_are_named_before_the_options(dash, working, aops):
    aops.ask(working, [{"text": "First?", "options": ["a", "b"]}, {"text": "Second?", "options": ["c", "d"]}])
    card = _ym(dash.get("/board").text, working)
    assert "+1 more question on the ticket" in card
    assert card.index("+1 more question") < card.index('data-option="1"')


def test_swimlane_rails_name_their_group_and_done_takes_drops(dash, put):
    put("in-progress", title="Busy", labels=["ops"])
    html = dash.get("/board?group=label").text
    assert re.search(r'<div class="flow" role="group" aria-label="ops: agent flow">', html)
    assert 'aria-label="ops: Ready: no tickets"' in html
    assert re.search(r'<a class="lane lane-rail rail-done" href="[^"]+" data-status="done" aria-label="ops: 0 done', html)
    dash.get("/board?group=none")

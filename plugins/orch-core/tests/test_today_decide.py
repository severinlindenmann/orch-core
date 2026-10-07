"""Today as the decide page (#11/#17): blocking decisions first as DecisionCards, the backlog folded into one line,
low-priority confirm items, no browser popups (inline confirm, in-page dialog with a no-JS confirm page, delayed send),
keyboard and palette hooks, one shared Start agent panel, inbox zero."""
import re
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")

from orch.clock import now as clock_now
from orch.clock import stamp


def _q(qid="Q1", blocking=True):
    return {"id": qid, "text": "Which env?", "type": "single",
            "options": [{"key": "A", "label": "dev"}, {"key": "B", "label": "prod"}], "recommended": "A",
            "blocking": blocking, "answer": None, "note": None, "answered": None, "via": None}


def _claim(minutes_ago=0, session="s1"):
    return {"harness": "claude-code", "session": session, "at": stamp(clock_now() - timedelta(minutes=minutes_ago))}


def _cards(html):
    return re.findall(r'<article class="decision-card decision dc-(\w+)" id="d-([^"]+)"', html)


def test_blocking_first_backlog_folded_into_one_line(dash, put):
    groom = [put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"}) for _ in range(4)]
    plan = put("in-progress", claim=_claim(), sections={"Plan": "1. step"})
    testing = put("testing", sections={"Verification": "ok"})
    html = dash.get("/").text
    assert [c for _, c in _cards(html)] == [f"{plan}-approve-plan", f"{testing}-verdict"]  # no backlog card on Today
    assert "<h1>2 decisions, then the agents run on their own</h1>" in html and "4 ideas in the backlog" in html
    assert re.search(r"Backlog <b>4</b> to groom", html) and "none of them blocks an agent" in html
    assert 'href="/groom">Groom one by one</a>' in html and "Show list</summary>" in html
    for g in groom:
        assert f'href="/groom?at={g}">Review</a>' in html
    assert "<title>(2) Today" in html


def test_no_open_textarea_no_popup_no_chart(dash, put, aops):
    put("in-progress", claim=_claim(), sections={"Plan": "1. step"})
    put("testing", sections={"Verification": "ok"})
    asking = put("open")
    aops.ask(asking, [{"text": "Which schema?", "type": "text"}])
    html = dash.get("/").text
    # the only text areas are the folded Send back / Request changes composers (issue #202): multi-line, never open
    import re
    bare = re.sub(r"<details class=\"dc-more\">.*?</details>", "", html, flags=re.S)
    assert "<textarea" not in bare and "data-confirm=" not in html
    assert "Done per day" not in html and 'class="bars"' not in html
    # the folded disclosures
    assert "Request changes…</summary>" in html and "Send back…</summary>" in html


def test_answer_card_names_options_by_key_and_folds_another_answer(dash, put):
    tid = put("waiting", questions=[_q()])
    html = dash.get("/").text
    assert '<span class="opt-n">A</span> · dev</button> <span class="chip chip-ok"><svg class="i" aria-hidden="true"><use href="#i-ok"/></svg> recommended</span>' in html
    assert 'data-option="2"' in html and "Write another answer…</summary>" in html
    assert f'data-delayed-send="Sending your answer to {tid}"' in html


def test_approve_card_shows_hash_feedforward_and_inline_confirm(dash, ws, put):
    from orch.core import store
    from orch.core.gates import gate_hash
    tid = put("in-progress", claim=_claim(), sections={"Plan": "1. step one\n2. step two"})
    h = gate_hash(store.load(ws, tid)[1], "plan").removeprefix("sha256:")
    html = dash.get("/").text
    assert f"sha256 {h[:4]}…{h[-2:]}" in html
    assert f'data-inline-confirm="Confirm · plan {h[:4]}…{h[-2:]}"' in html
    assert "Lets claude-code work the tasks. Does not accept the work." in html
    # the full text sits before the button
    assert html.index("step two") < html.index(f'action="/t/{tid}/approve"')
    # phone: a link to the ticket's read-and-approve view
    assert f'href="/t/{tid}#gate-plan">Read plan and approve ›</a>' in html


def test_open_question_override_sits_inside_the_confirm_form(dash, put):
    tid = put("in-progress", claim=_claim(), sections={"Plan": "1. step\nOpen question: which bucket?"})
    html = dash.get("/").text
    form = html.split(f'action="/t/{tid}/approve"', 1)[1].split("</form>", 1)[0]
    assert 'name="despite_open_question"' in form and "required" in form


def test_reapprove_card_shows_the_diff_and_the_full_text(dash, ws, working, aops, hops, plan_approved):
    plan_approved(working, "1. first step")
    aops.set_section(working, "Plan", "1. first step\n2. a new step")
    html = dash.get("/").text
    card = html.split(f'id="d-{working}-re-approve"', 1)[1].split("</article>", 1)[0]
    assert "Changed since you approved" in card and '<span class="diff-add">+ 2. a new step</span>' in card
    assert 'data-gate-part="Plan"' in card and card.index("Changed since") < card.index('data-gate-part="Plan"')


def test_verdict_card_lists_acceptance_criteria_with_proof(dash, put):
    tid = put("testing", sections={"Acceptance criteria": "- [x] retries 3 times\n- [ ] gives up\n- [ ] logs\n- [ ] metrics",
                                   "Verification": "- AC1: test_retry.py passed"})
    html = dash.get("/").text
    card = html.split(f'id="d-{tid}-verdict"', 1)[1].split("</article>", 1)[0]
    assert "<b class=\"ac-n\">AC1</b> retries 3 times" in card and "test_retry.py passed" in card
    assert "Show 1 more criteria</summary>" in card
    assert f'data-inline-confirm="Confirm · close {tid} as done"' in card
    assert "Verification notes</summary>" in card


def test_stale_claims_are_one_compact_list_with_dialog_releases(dash, configure, put):
    configure(dashboard={"stale_minutes": 60})
    tid = put("in-progress", claim=_claim(minutes_ago=200))
    html = dash.get("/").text
    assert f"{tid}-stale-claim" not in [c for _, c in _cards(html)]  # no full card per silent claim
    section = html.split('class="card stale-claims"', 1)[1].split("</section>", 1)[0]
    assert "1 claim gone silent" in section and "claude-code silent 3 h" in section
    form = section.split(f'action="/t/{tid}/release"', 1)[1].split("</form>", 1)[0]
    assert f'data-dialog="Release claude-code\'s claim on {tid}?"' in form and 'name="ask" value="1"' in form
    assert "<title>(1) Today" in html


def test_many_decisions_stay_light(dash, configure, put):
    """#17 page weight: past the first five, decisions are one-line rows that open the one-by-one view; silent claims
    list ten; In flight shows eight cards."""
    ws = configure(dashboard={"stale_minutes": 60})
    for i in range(30):
        put("testing", title=f"Verdict {i}", sections={"Acceptance criteria": "- [ ] a", "Verification": "- AC1: ok"})
    for i in range(20):
        put("in-progress", claim=_claim(minutes_ago=300, session=f"s{i}"), title=f"Silent {i}")
    html = dash.get("/").text
    assert len(_cards(html)) == 5
    assert html.count('<li class="dc-row"') == 25 + 10
    assert "All 20 on Activity" in html
    assert 'href="/groom?scope=blocking&amp;at=' in html
    assert len(html.encode()) < 150_000


def test_focus_view_walks_the_blocking_decisions(dash, put):
    ids = [put("testing", title=f"V{i}", sections={"Verification": "ok"}) for i in range(7)]
    html = dash.get(f"/groom?scope=blocking&at={ids[6]}-verdict").text
    assert "<h1>Decide one by one</h1>" in html and "7 of 7" in html and f'id="d-{ids[6]}-verdict"' in html
    assert f'href="/groom?scope=blocking&amp;at={ids[5]}-verdict" rel="prev"' in html


def test_confirm_item_is_low_priority_and_not_counted(dash, put):
    tid = put("in-progress", claim=_claim(), questions=[_q("Q2", blocking=False)])
    html = dash.get("/").text
    assert ("neu", f"{tid}-confirm") in _cards(html)  # no pink
    assert "When you have a moment" in html and "claude-code went with A." in html
    assert "<title>Today" in html and "Nothing is waiting on you" in html


def test_inbox_zero_is_honest_about_the_backlog(dash, put):
    put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    put("in-progress", claim=_claim(), sections={"Plan": "1. x"}, size="xs")
    html = dash.get("/").text
    assert "Nothing needs you right now" in html and "1 agent is working." in html
    assert '<a class="lnk" href="/groom">1 requirement in the backlog · groom a few</a>' in html


def test_decision_list_carries_the_grouping_hook(ws, put):
    """Epic grouping plugs in through Decision.group and data.today.groups (one heading per key, list order kept)."""
    from orch.core import query
    from orch.dashboard.data import decisions, today
    a = put("testing")
    b = put("testing")
    c = put("testing")
    ds = decisions.decisions(ws, needs=query.waiting(ws))
    assert [g["key"] for g in today.groups(ds)] == [None] and len(today.groups(ds)[0]["decisions"]) == 3
    ds[0].group, ds[2].group = "Epic one", "Epic one"
    got = today.groups(ds)
    assert [(g["label"], [d.ticket for d in g["decisions"]]) for g in got] == [("Epic one", [a, c]), (None, [b])]


def test_grouped_decisions_render_under_a_heading(dash, put, monkeypatch):
    from orch.dashboard.data import decisions
    put("testing")
    real = decisions.decisions

    def grouped(*a, **k):
        out = real(*a, **k)
        for d in out:
            d.group = "Billing epic"
        return out
    monkeypatch.setattr(decisions, "decisions", grouped)
    html = dash.get("/").text
    assert 'class="dc-group" data-group="Billing epic"' in html and "Billing epic <span" in html


# -- no browser popups: the careful tier is a dialog, and a confirm page without JS --------------------------------

def test_release_without_js_asks_on_a_confirm_page(dash, ws, put):
    from orch.core import store
    tid = put("in-progress", claim=_claim())
    r = dash.post(f"/t/{tid}/release", data={"ask": "1", "next": "/"})
    assert r.status_code == 200 and f"Release claude-code&#39;s claim on {tid}?" in r.text
    assert f'action="/t/{tid}/release"' in r.text and 'name="ask"' not in r.text and "Keep claim</a>" in r.text
    assert store.load(ws, tid)[1].meta["claim"].get("session") == "s1"  # nothing happened yet
    r = dash.post(f"/t/{tid}/release", data={"next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and not store.load(ws, tid)[1].meta["claim"].get("session")


def test_tidy_without_js_asks_first(dash):
    r = dash.post("/workspace/tidy", data={"ask": "1"})
    assert r.status_code == 200 and "Delete old files from temporary/?" in r.text and "Keep files</a>" in r.text


def test_revoke_and_trust_forms_use_the_dialog_tier():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "orch" / "dashboard"
    workspace = "".join((root / "templates" / n).read_text(encoding="utf-8") for n in ("workspace.html", "_addons_tab.html"))
    assert "data-confirm" not in workspace and workspace.count("data-dialog=") >= 3
    for path in (root / "templates").glob("*.html"):
        assert "data-confirm=" not in path.read_text(encoding="utf-8"), path
    assert "window.confirm" not in (root / "static" / "app.js").read_text(encoding="utf-8")


def test_addon_action_without_js_confirms_and_returns_to_its_page(dash, monkeypatch):
    """The confirm page carries the page it came from, so the action's redirect lands there and not on the POST URL."""
    import types

    from orch.addons.manifest import ActionSpec
    spec = ActionSpec("rerun", "Rerun checks", confirm="Rerun the failed checks?")
    called = []
    fake = types.SimpleNamespace(manifest=types.SimpleNamespace(action=lambda a: spec if a == "rerun" else None,
                                                                 title="Demo"),
                                 obj=types.SimpleNamespace(act=lambda *a, **k: called.append(a)), ctx=None)
    registry = dash.app.state.addons.registry
    real_get = registry.get
    monkeypatch.setattr(registry, "get", lambda name: fake if name == "demo" else real_get(name))
    origin = {"origin": "http://testserver"}
    r = dash.post("/addons/demo/actions/rerun", data={"target": "pr-1", "ask": "1"}, headers=origin)
    assert r.status_code == 200 and "Rerun the failed checks?" in r.text and called == []
    assert 'name="return_to" value="/addons/demo/"' in r.text


def test_keyboard_shortcuts_setting_per_workspace(dash):
    html = dash.get("/").text
    assert 'data-shortcuts="on"' in html
    r = dash.post("/workspace/shortcuts", data={"on": "0"}, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    assert 'data-shortcuts="off"' in dash.get("/").text
    assert "Turn shortcuts on" in dash.get("/workspace").text


def test_kbd_hint_only_with_shortcuts_on(dash, put):
    put("testing")
    assert "data-kbd-hint" in dash.get("/").text
    dash.post("/workspace/shortcuts", data={"on": "0"}, headers={"origin": "http://testserver"})
    assert "data-kbd-hint" not in dash.get("/").text


def test_palette_json_lists_decisions_and_tickets(dash, put):
    plan = put("in-progress", claim=_claim(), title="Coverage in CI", sections={"Plan": "1. x"})
    put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    data = dash.get("/palette.json").json()
    assert data["decisions"] == [{"label": f"Approve plan · {plan}", "card": f"d-{plan}-approve-plan",
                                  "href": f"/#d-{plan}-approve-plan", "words": "approve plan Coverage in CI"}]
    assert {t["id"] for t in data["tickets"]} >= {plan}


# -- the shared Start agent panel ---------------------------------------------------------------------------------

def _two_startable(put, hops):
    ids = []
    for title in ("First to start", "Second to start"):
        tid = put("backlog", title=title, sections={"Requirements": "r", "Acceptance criteria": "a"})
        hops.approve(tid, "requirements")
        ids.append(tid)
    return ids


def test_today_has_one_start_agent_panel(dash, put, hops):
    a, b = _two_startable(put, hops)
    html = dash.get("/").text
    assert html.count('class="start-agent"') == 1 and html.count("sa-options") == 1
    assert f'id="start-agent-{a}"' in html
    assert f'href="/?start={b}#start-agent" data-start-panel="{b}"' in html
    html = dash.get(f"/?start={b}").text
    assert html.count('class="start-agent"') == 1 and f'id="start-agent-{b}"' in html


def test_start_panel_fragment(dash, put, hops):
    a, _ = _two_startable(put, hops)
    r = dash.get(f"/t/{a}/agent/panel?next=/")
    assert r.status_code == 200 and f'id="start-agent-{a}"' in r.text and "<html" not in r.text
    assert 'name="next" value="/"' in r.text


def test_page_size_stays_small_with_many_in_flight(dash, put, hops):
    for i in range(12):
        tid = put("backlog", title=f"Ticket {i}", sections={"Requirements": "r", "Acceptance criteria": "a"})
        hops.approve(tid, "requirements")
    html = dash.get("/").text
    assert html.count("sa-options") == 1


# -- the one-by-one groom view -------------------------------------------------------------------------------------

def test_groom_without_backlog(dash):
    html = dash.get("/groom").text
    assert "The backlog is groomed" in html


def test_groom_card_full_text_and_back_to_next(dash, put):
    a = put("backlog", sections={"Requirements": "- only line", "Acceptance criteria": "- a"})
    b = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    html = dash.get(f"/groom?at={a}").text
    assert "only line" in html and f'action="/t/{a}/approve"' in html and 'data-key-next' in html
    assert f'name="next" value="/groom?scope=backlog&amp;at={b}-approve-requirements"' in html


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is not installed")
def test_keyboard_palette_and_delayed_send_js():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(["node", str(root / "tests" / "js" / "today_keys.js"),
                        str(root / "src" / "orch" / "dashboard" / "static" / "app.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "today keys ok" in r.stdout


# -- answers and change requests are bound to what the human saw (fix round 1) --

def test_answer_posts_the_question_hash_and_a_reasked_question_is_refused(dash, ws, put, aops):
    from orch.core import store
    from orch.core.questions import question_hash
    tid = put("waiting", questions=[_q()])
    html = dash.get("/").text
    qh = question_hash(store.load(ws, tid)[1].meta["questions"][0])
    assert html.count(f'name="qhash" value="{qh}"') == 2  # the options form and "Write another answer"
    # the agent re-asks Q1 with other options while the answer waits in its Undo window
    path, t = store.load(ws, tid)
    t.meta["questions"][0]["options"] = [{"key": "A", "label": "prod"}, {"key": "B", "label": "dev"}]
    store.save(ws, t, path)
    r = dash.post(f"/t/{tid}/answer", data={"qid": "Q1", "value": "A", "qhash": qh}, follow_redirects=False)
    assert "question+changed" in r.headers["location"]
    assert store.load(ws, tid)[1].meta["questions"][0]["answer"] in (None, "")
    r = dash.post(f"/t/{tid}/answer", data={"qid": "Q1", "value": "A"}, follow_redirects=False)
    assert "reload+the+page" in r.headers["location"]  # no hash, no answer


def test_request_changes_posts_seen_and_an_edited_plan_is_refused(dash, ws, put):
    from orch.core import gates, store
    tid = put("in-progress", claim=_claim(), sections={"Plan": "1. step"})
    seen = gates.gate_hash(store.load(ws, tid)[1], "plan")
    card = dash.get("/").text.split(f'id="d-{tid}-approve-plan"', 1)[1].split("</article>", 1)[0]
    form = card.split(f'action="/t/{tid}/request-changes"', 1)[1].split("</form>", 1)[0]
    assert f'name="seen" value="{seen}"' in form
    path, t = store.load(ws, tid)
    t.set_section("Plan", "1. step\n2. another")
    store.save(ws, t, path)
    r = dash.post(f"/t/{tid}/request-changes", data={"gate": "plan", "message": "smaller", "seen": seen},
                  follow_redirects=False)
    assert "changed+since" in r.headers["location"]
    assert not gates.changes_pending(store.load(ws, tid)[1], "plan")
    r = dash.post(f"/t/{tid}/request-changes", data={"gate": "plan", "message": "smaller"}, follow_redirects=False)
    assert "reload+the+page" in r.headers["location"]


def test_ticket_page_forms_carry_the_hashes(dash, ws, put):
    tid = put("waiting", claim=_claim(), questions=[_q()], sections={"Plan": "1. step"})
    html = dash.get(f"/t/{tid}").text
    assert 'name="qhash" value="sha256:' in html

"""#11 / #12: every needs-you item carries a scope (blocking, backlog, later); blocking items come first, unanswered
non-blocking questions are low-priority "confirm" items, a silent claim is a blocking "stale-claim" item, and every
count (headline, menu badge, tab title, SessionStart hook) is the blocking count from query.counts()."""
from datetime import timedelta

from orch.clock import now as clock_now
from orch.clock import stamp
from orch.core import query


def _q(qid="Q1", blocking=True, **extra):
    return {"id": qid, "text": "Which env?", "type": "single",
            "options": [{"key": "A", "label": "dev"}, {"key": "B", "label": "prod"}], "recommended": "A",
            "blocking": blocking, "answer": None, "note": None, "answered": None, "via": None, **extra}


def _claim(session="s1", minutes_ago=0):
    return {"harness": "claude-code", "session": session, "at": stamp(clock_now() - timedelta(minutes=minutes_ago))}


def scoped(ws, **kw):
    return [(i["ticket"], i["kind"], i["scope"]) for i in query.waiting(ws, **kw)]


def test_plan_on_a_claimed_ticket_sorts_before_backlog_requirements(ws, put):
    groom = [put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"}) for _ in range(3)]
    plan = put("in-progress", claim=_claim(), sections={"Plan": "1. step"})
    got = scoped(ws)
    assert got[0] == (plan, "approve-plan", "blocking")
    assert [(t, s) for t, _, s in got[1:]] == [(g, "backlog") for g in groom]


def test_verdict_and_answer_are_blocking(ws, put):
    asking = put("waiting", questions=[_q()])
    testing = put("testing")
    got = scoped(ws)
    assert (asking, "answer", "blocking") in got and (testing, "verdict", "blocking") in got


def test_requirements_on_a_claimed_backlog_ticket_block_the_refine_agent(ws, put):
    tid = put("backlog", claim=_claim(), sections={"Requirements": "r", "Acceptance criteria": "a"})
    assert scoped(ws) == [(tid, "approve-requirements", "blocking")]


def test_reapprove_on_an_unclaimed_backlog_ticket_is_backlog(ws, aops, hops, put):
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "a")
    hops.approve(t.id, "requirements")
    aops.set_section(t.id, "Requirements", "changed")
    assert scoped(ws) == [(t.id, "re-approve", "blocking")]  # open: an agent may pick it up any time
    hops.move(t.id, "backlog")
    assert all(s == "backlog" for _, k, s in scoped(ws) if k == "re-approve")


def test_human_task_while_the_agent_still_has_work_is_later(ws, aops, working, plan_approved):
    aops.task_add(working, [{"text": "a"}, {"text": "sign off", "owner": "human"}])
    plan_approved(working)
    assert scoped(ws) == [(working, "task", "later")]


def test_unanswered_non_blocking_question_is_a_confirm_item(ws, put):
    """#12 alternative: the agent went with the recommendation; the human may confirm or change it."""
    tid = put("in-progress", claim=_claim(), questions=[_q("Q2", blocking=False)])
    items = query.waiting(ws)
    assert [(i["ticket"], i["kind"], i["scope"], i["detail"]) for i in items] == [(tid, "confirm", "later", "Q2")]
    assert query.counts(items) == {"blocking": 0, "backlog": 0, "later": 1, "total": 1}


def test_confirm_item_sorts_after_blocking_items(ws, put):
    confirm = put("in-progress", claim=_claim(), questions=[_q("Q2", blocking=False)])
    testing = put("testing")
    assert [t for t, _, _ in scoped(ws)] == [testing, confirm]


def test_answering_a_confirm_item_keeps_the_status(ws, hops, put):
    tid = put("in-progress", claim=_claim(), questions=[_q("Q2", blocking=False)])
    t = hops.answer(tid, "Q2", "B")
    assert t.status == "in-progress" and scoped(ws) == []


def test_confirm_items_never_mark_a_ticket_as_waiting_on_the_human(ws, put):
    from orch.core import store
    from orch.dashboard.data.tasks import waiting_on_human
    put("in-progress", claim=_claim(), questions=[_q("Q2", blocking=False)])
    entries = store.scan(ws)
    assert waiting_on_human(query.needs_you(ws, entries=entries), entries) == set()


def test_silent_claim_is_a_blocking_stale_claim_item(ws, configure, put):
    ws = configure(dashboard={"stale_minutes": 60})
    stale = put("in-progress", claim=_claim(minutes_ago=180))
    put("in-progress", claim=_claim("s2", minutes_ago=5))  # working
    items = query.waiting(ws)
    assert [(i["ticket"], i["kind"], i["scope"]) for i in items] == [(stale, "stale-claim", "blocking")]
    assert items[0]["harness"] == "claude-code" and "3 h" in items[0]["detail"]


def test_claim_waiting_on_the_human_is_not_stale(ws, configure, put):
    ws = configure(dashboard={"stale_minutes": 60})
    tid = put("waiting", claim=_claim(minutes_ago=600), questions=[_q()])
    assert scoped(ws) == [(tid, "answer", "blocking")]


def test_stale_claim_follows_the_clock_not_the_needs_cache(ws, configure, put):
    ws = configure(dashboard={"stale_minutes": 60})
    tid = put("in-progress", claim=_claim(minutes_ago=30))
    assert scoped(ws) == []
    later = clock_now() + timedelta(minutes=45)
    assert scoped(ws, now=later) == [(tid, "stale-claim", "blocking")]


def test_counts_split_blocking_backlog_later(ws, put):
    put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    put("testing")
    assert query.counts(query.waiting(ws)) == {"blocking": 1, "backlog": 2, "later": 0, "total": 3}


def test_session_start_shows_the_blocking_count_first(ws, put):
    from orch.hooks.session_start import session_start_text
    for _ in range(3):
        put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    put("testing")
    text = session_start_text(ws, "nobody")
    assert "Waiting on the human: 1 blocking · 3 in the backlog" in text
    listed = [line for line in text.splitlines() if line.startswith("- ")]
    assert "give a verdict" in listed[0]


def test_menu_badge_and_title_show_the_blocking_count(dash, put):
    for _ in range(2):
        put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    put("testing")
    html = dash.get("/board").text
    assert "<title>(1) Board" in html


def test_title_has_no_count_with_only_backlog(dash, put):
    put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    html = dash.get("/board").text
    assert "<title>Board" in html

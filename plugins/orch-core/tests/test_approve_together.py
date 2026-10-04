"""F2: when an agent drafted Requirements, Acceptance criteria and Plan before handing over, the human approves both
gates in one confirm: both texts shown in full, each bound to its own hash, two events and two signed ledger entries.
No gate is removed or weakened: the plan stays a gate of its own (an xs ticket still has none)."""
import pytest

from orch.core import ledger, query, store
from orch.core.events import read_events
from orch.core.gates import gate_hash, gate_state
from orch.errors import HumanOnlyError, TransitionError, UsageError, ValidationError


@pytest.fixture
def drafted(aops):
    t = aops.new("Export as CSV")
    aops.set_section(t.id, "Requirements", "- the weekly report downloads as CSV")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a CSV file with one row per job")
    aops.set_section(t.id, "Plan", "1. add the CSV writer\n2. add the download button")
    return t.id


def _hashes(ws, tid):
    t = store.load(ws, tid)[1]
    return gate_hash(t, "requirements"), gate_hash(t, "plan")


def test_both_gates_are_approved_in_one_step(ws, hops, drafted):
    req, plan = _hashes(ws, drafted)
    t = hops.approve_together(drafted, requirements_hash=req, plan_hash=plan)
    assert t.status == "open"
    assert gate_state(t, "requirements") == "approved" and gate_state(t, "plan") == "approved"
    approved = [e for e in read_events(ws, drafted) if e.kind == "gate.approved"]
    assert [e.data["gate"] for e in approved] == ["requirements", "plan"]
    assert approved[0].data["hash"] == req and approved[1].data["hash"] == plan
    assert approved[0].data["to"] == "open" and all(e.data.get("together") for e in approved)
    signed = [e for e in ledger.entries(ws) if e["kind"] == "gate" and e["ticket"] == drafted]
    assert {(e["gate"], e["hash"]) for e in signed} == {("requirements", req), ("plan", plan)}
    assert ledger.gate_verification(ws, t, "plan") == "verified"


def test_the_agent_starts_work_without_a_second_plan_approval(ws, hops, aops, drafted):
    req, plan = _hashes(ws, drafted)
    hops.approve_together(drafted, requirements_hash=req, plan_hash=plan)
    aops.claim(drafted)
    assert not [i for i in query.needs_you(ws) if i["ticket"] == drafted]
    _, ids = aops.task_add(drafted, [{"text": "add the CSV writer"}])
    aops.task_start(drafted, ids[0])  # refused until the plan is approved (#9): the one confirm covered it


@pytest.mark.parametrize("which", ["requirements", "plan"])
def test_a_changed_text_refuses_both(ws, hops, aops, drafted, which):
    req, plan = _hashes(ws, drafted)
    aops.set_section(drafted, "Plan" if which == "plan" else "Requirements", "- something else")
    with pytest.raises(ValidationError, match="changed since"):
        hops.approve_together(drafted, requirements_hash=req, plan_hash=plan)
    t = store.load(ws, drafted)[1]
    assert t.status == "backlog" and gate_state(t, "requirements") == "pending" and gate_state(t, "plan") == "pending"
    assert not [e for e in ledger.entries(ws) if e.get("ticket") == drafted]


def test_both_hashes_are_required(ws, hops, drafted):
    req, plan = _hashes(ws, drafted)
    for kw in ({"requirements_hash": req, "plan_hash": ""}, {"requirements_hash": "", "plan_hash": plan}):
        with pytest.raises((ValidationError, UsageError)):
            hops.approve_together(drafted, **kw)
    assert store.load(ws, drafted)[1].status == "backlog"


def test_an_agent_cannot_approve_together(ws, aops, drafted):
    req, plan = _hashes(ws, drafted)
    with pytest.raises(HumanOnlyError):
        aops.approve_together(drafted, requirements_hash=req, plan_hash=plan)


def test_an_empty_plan_is_refused(ws, hops, aops):
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    req, plan = _hashes(ws, t.id)
    with pytest.raises(ValidationError, match="Plan"):
        hops.approve_together(t.id, requirements_hash=req, plan_hash=plan)


def test_a_size_without_a_plan_gate_is_refused(ws, hops, aops):
    t = aops.new("x", size="xs")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    aops.set_section(t.id, "Plan", "1. x")
    req, plan = _hashes(ws, t.id)
    with pytest.raises(ValidationError, match="no plan gate"):
        hops.approve_together(t.id, requirements_hash=req, plan_hash=plan)


def test_outside_backlog_it_is_refused(ws, hops, drafted):
    hops.approve(drafted, "requirements")
    req, plan = _hashes(ws, drafted)
    with pytest.raises(TransitionError):
        hops.approve_together(drafted, requirements_hash=req, plan_hash=plan)


def test_an_open_question_line_in_the_plan_needs_the_override(ws, hops, aops, drafted):
    aops.set_section(drafted, "Plan", "1. write it\nOpen question: which delimiter?")
    req, plan = _hashes(ws, drafted)
    with pytest.raises(ValidationError, match="open question"):
        hops.approve_together(drafted, requirements_hash=req, plan_hash=plan)
    t = hops.approve_together(drafted, requirements_hash=req, plan_hash=plan, despite_open_question=True)
    assert gate_state(t, "plan") == "approved"


def test_hidden_characters_in_the_plan_refuse_both(ws, hops, aops, drafted):
    aops.set_section(drafted, "Plan", "1. write it‮")
    req, plan = _hashes(ws, drafted)
    with pytest.raises(ValidationError, match="hidden"):
        hops.approve_together(drafted, requirements_hash=req, plan_hash=plan)
    assert store.load(ws, drafted)[1].status == "backlog"


def test_a_blocking_question_refuses_both(ws, hops, aops, drafted):
    aops.ask(drafted, [{"text": "Which delimiter?", "options": ["comma", "semicolon"]}])
    req, plan = _hashes(ws, drafted)
    with pytest.raises(ValidationError, match="blocking"):
        hops.approve_together(drafted, requirements_hash=req, plan_hash=plan)


def test_an_epic_is_refused(ws, hops, aops):
    e = aops.new("Epic", type="epic")
    with pytest.raises(UsageError):
        hops.approve_together(e.id, requirements_hash="sha256:a", plan_hash="sha256:b")


# -- needs_you offers it --------------------------------------------------------------------------------------------

def test_needs_you_marks_the_requirements_item_as_approvable_together(ws, drafted):
    item = next(i for i in query.needs_you(ws) if i["ticket"] == drafted)
    req, plan = _hashes(ws, drafted)
    assert item["kind"] == "approve-requirements" and item["together"] is True
    assert item["gate_hash"] == req and item["plan_hash"] == plan


def test_needs_you_does_not_offer_it_without_a_plan_or_for_xs(ws, aops):
    a = aops.new("no plan")
    aops.set_section(a.id, "Requirements", "r")
    aops.set_section(a.id, "Acceptance criteria", "- [ ] a")
    b = aops.new("xs", size="xs")
    aops.set_section(b.id, "Requirements", "r")
    aops.set_section(b.id, "Acceptance criteria", "- [ ] a")
    aops.set_section(b.id, "Plan", "1. x")
    for i in query.needs_you(ws):
        assert not i.get("together"), i


# -- the dashboard offers it as the primary action -------------------------------------------------------------------

def _card(page: str, tid: str) -> str:
    for chunk in page.split('<article class="decision-card decision')[1:]:
        if f'href="/t/{tid}"' in chunk:
            return chunk.split("</article>", 1)[0]
    raise AssertionError(f"no card for {tid}")


def test_the_card_shows_both_texts_and_one_primary_approve(dash, ws, drafted):
    import re
    card = _card(dash.get("/groom").text, drafted)
    req, plan = _hashes(ws, drafted)
    assert 'action="/t/%s/approve-together"' % drafted in card
    assert "Approve requirements and plan" in card
    assert "the weekly report downloads as CSV" in card and "add the download button" in card
    assert re.search(r'name="seen" value="%s"' % re.escape(req), card)
    assert re.search(r'name="seen_plan" value="%s"' % re.escape(plan), card)
    assert card.index("approve-together") < card.index('action="/t/%s/approve"' % drafted)  # the single one is secondary


def test_a_card_without_a_plan_keeps_the_single_approval(dash, ws, aops):
    t = aops.new("no plan")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    card = _card(dash.get("/groom").text, t.id)
    assert "approve-together" not in card and "Approve requirements" in card


def test_posting_approves_both(dash, ws, drafted):
    req, plan = _hashes(ws, drafted)
    r = dash.post(f"/t/{drafted}/approve-together", data={"seen": req, "seen_plan": plan}, follow_redirects=False)
    assert r.status_code == 303
    t = store.load(ws, drafted)[1]
    assert t.status == "open" and gate_state(t, "plan") == "approved"


def test_posting_without_the_plan_hash_approves_nothing(dash, ws, drafted):
    req, _ = _hashes(ws, drafted)
    dash.post(f"/t/{drafted}/approve-together", data={"seen": req}, follow_redirects=False)
    t = store.load(ws, drafted)[1]
    assert t.status == "backlog" and gate_state(t, "requirements") == "pending"


def test_posting_a_stale_plan_hash_approves_nothing(dash, ws, aops, drafted):
    req, plan = _hashes(ws, drafted)
    aops.set_section(drafted, "Plan", "1. something else")
    r = dash.post(f"/t/{drafted}/approve-together", data={"seen": req, "seen_plan": plan})
    assert "changed since" in r.text
    assert store.load(ws, drafted)[1].status == "backlog"


def test_the_ticket_page_offers_both_together(dash, ws, drafted):
    html = dash.get(f"/t/{drafted}").text
    assert 'action="/t/%s/approve-together"' % drafted in html and "Approve requirements and plan" in html
    assert "add the download button" in html


def test_the_combined_form_lists_every_open_question_line_of_both_gates(dash, ws, aops, drafted):
    aops.set_section(drafted, "Requirements", "- CSV\nOpen question: which encoding?")
    aops.set_section(drafted, "Plan", "1. writer\nTBD: the delimiter")
    card = _card(dash.get("/groom").text, drafted)
    form = card.split('approve-together"', 1)[1].split("</form>", 1)[0]
    assert "which encoding?" in form and "TBD: the delimiter" in form and "despite_open_question" in form
    page = dash.get(f"/t/{drafted}").text
    form = page.split('action="/t/%s/approve-together"' % drafted, 1)[1].split("</form>", 1)[0]
    assert "which encoding?" in form and "TBD: the delimiter" in form

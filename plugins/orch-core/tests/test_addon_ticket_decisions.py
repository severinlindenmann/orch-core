"""Decisions on the ticket page, the ticket.sync slot and the intent extensions a phone-style addon needs (P0 Task 3,
on top of ruling R-A1-INTENT): answers bound to the question hash, `new` tickets from a decision, `on_intent_result`."""
from pathlib import Path

import pytest

from addon_fixtures import loaded
from orch.addons import intents
from orch.addons.api import Intent, PendingDecision, ProviderContext, TicketIntent
from orch.addons.loader import AddonRegistry
from orch.core import store
from orch.core.events import read_events
from orch.core.questions import question_hash
from orch.dashboard.reach import LOCAL_HUMAN as HUMAN  # the local dashboard actor (was views.HUMAN)
from orch.errors import ValidationError

ORIGIN = {"origin": "http://testserver"}
OVER = {"capabilities": ["panel", "decisions"], "slots": ["ticket.sync"], "menu": None, "settings_schema": []}


class Phone:
    def __init__(self):
        self.items, self.results, self.args = [], [], []
        self.intent = None

    def widgets(self, slot, view):
        from orch.addons.widgets import Card, Text
        return [Card("Synced to phone", (Text(f"sync for {view.ticket.id}"),))] if slot == "ticket.sync" else []

    def decisions(self, view):
        return self.items

    def resolve(self, decision_id, choice, ctx):
        self.args.append(ctx)
        if choice == "ignore":
            return "Ignored"
        return self.intent

    def on_intent_result(self, decision_id, outcome, message):
        self.results.append((decision_id, outcome, message))


@pytest.fixture
def asked(ws, aops):
    t = aops.new("Phone answers")
    aops.ask(t.id, [{"text": "Which format?", "options": ["ISO 8601", "Local"], "recommended": "A"}])
    return t.id


@pytest.fixture
def phone(ws, asked):
    obj = Phone()
    ws._addons = AddonRegistry(ws, {"phone": loaded(ws, obj, name="phone", **OVER)})
    return obj


@pytest.fixture
def client(ws, phone, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def _q(ws, tid):
    return store.load(ws, tid)[1].meta["questions"][0]


def _decide(client, id="p1", choice="apply"):
    r = client.post("/addons/phone/decisions", data={"id": id, "choice": choice}, headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"]


def _run(ws, intent, ref=None, *, decisions=True):
    return intents.execute(ws, intent, allowed_ref=ref, tickets=False, actor=HUMAN, source="resolve",
                           decisions=decisions)


def test_ticket_intent_is_the_a1_intent():
    assert TicketIntent is Intent


def test_answer_intent_applies_as_the_human_and_reports_back(ws, client, phone, asked):
    h = question_hash(_q(ws, asked))
    phone.items = [PendingDecision("p1", "Answer from phone: A", ticket=asked, anchor="Q1")]
    phone.intent = Intent("answer", ref=asked, qid="Q1", value="A", expected_hash=h)
    loc = _decide(client)
    assert "err=" not in loc and "Answered+Q1" in loc
    q = _q(ws, asked)
    assert q["answer"] == "A" and q["via"] == "dashboard"
    assert len(phone.args) == 1 and isinstance(phone.args[0], ProviderContext)  # never an Ops
    assert phone.results == [("p1", "applied", f"Answered Q1 on {asked}")]
    last = read_events(ws)[-1]
    assert last.kind == "addon.decision" and last.actor == "human:you"
    assert last.data["outcome"] == "applied" and last.data["intent"] == "answer"


def test_answer_intent_with_old_hash_is_refused(ws, client, phone, asked):
    phone.items = [PendingDecision("p1", "Answer from phone", ticket=asked, anchor="Q1")]
    phone.intent = Intent("answer", ref=asked, qid="Q1", value="A", expected_hash="sha256:" + "0" * 64)
    before = len(read_events(ws, asked))
    loc = _decide(client)
    assert "err=" in loc and "question+changed" in loc
    assert _q(ws, asked)["answer"] is None
    assert len(read_events(ws, asked)) == before          # nothing written on the ticket
    assert phone.results[0][:2] == ("p1", "refused") and "question changed" in phone.results[0][2]
    last = read_events(ws)[-1]
    assert last.kind == "addon.decision" and last.data["outcome"] == "refused"


def test_answer_intent_without_a_hash_is_refused(ws, asked):
    with pytest.raises(ValidationError, match="question hash"):
        _run(ws, Intent("answer", ref=asked, qid="Q1", value="A"), asked)
    assert _q(ws, asked)["answer"] is None


def test_edited_question_makes_the_old_hash_stale(ws, asked):
    h = question_hash(_q(ws, asked))
    t = store.load(ws, asked)[1]
    t.meta["questions"][0]["text"] = "Which date format?"
    store.save(ws, t)
    with pytest.raises(ValidationError, match="question changed"):
        _run(ws, Intent("answer", ref=asked, qid="Q1", value="A", expected_hash=h), asked)
    assert _q(ws, asked)["answer"] is None


def test_request_changes_with_an_old_gate_hash_is_refused(ws, put):
    from orch.core.gates import gate_hash
    tid = put("backlog", sections={"Requirements": "Do it.", "Acceptance criteria": "- works"})
    with pytest.raises(ValidationError, match="changed since"):
        _run(ws, Intent("request_changes", ref=tid, gate="requirements", reason="More", expected_hash="0" * 64), tid)
    assert "changes_requested" not in (store.load(ws, tid)[1].meta.get("gates") or {}).get("requirements", {})
    h = gate_hash(store.load(ws, tid)[1], "requirements")
    _run(ws, Intent("request_changes", ref=tid, gate="requirements", reason="More", expected_hash=h), tid)
    assert store.load(ws, tid)[1].meta["gates"]["requirements"]["changes_requested"]["message"] == "More"


def test_intent_for_another_ticket_than_the_item_is_refused(ws, aops):
    first, other = aops.new("First").id, aops.new("Other").id
    with pytest.raises(ValidationError, match="this item is about"):
        _run(ws, Intent("verdict", ref=other, value="done"), first)


def test_new_intent_creates_a_backlog_ticket_as_the_human(ws, client, phone):
    phone.items = [PendingDecision("p2", "New ticket from phone")]
    phone.intent = Intent("new", value="From the phone", reason="spoken")
    loc = _decide(client, "p2")
    assert "err=" not in loc and "Created" in loc
    t = [e for e in store.scan(ws) if e.meta.get("title") == "From the phone"][0]
    t = store.load(ws, t.id)[1]
    assert t.status == "backlog" and t.section("Ask") == "spoken"
    assert read_events(ws, t.id)[0].actor == "human:you"
    assert phone.results[0][1] == "applied" and t.id in phone.results[0][2]


def test_new_intent_is_checked(ws, asked):
    with pytest.raises(ValidationError, match="decisions"):
        _run(ws, Intent("new", value="x"), decisions=False)
    with pytest.raises(ValidationError, match="no ticket"):
        _run(ws, Intent("new", ref=asked, value="x"), asked)
    with pytest.raises(ValidationError, match="title"):
        _run(ws, Intent("new", value="   "))
    with pytest.raises(ValidationError, match="at most 200"):
        _run(ws, Intent("new", value="x" * 201))
    with pytest.raises(ValidationError, match="only from a decision"):
        intents.execute(ws, Intent("new", value="x"), allowed_ref=None, tickets=True, actor=HUMAN, source="act",
                        decisions=True)
    assert [e.meta.get("title") for e in store.scan(ws)] == ["Phone answers"]


def test_a_failing_result_hook_does_not_undo_the_change(ws, client, phone, asked):
    def boom(*a):
        raise RuntimeError("hook broke")
    phone.on_intent_result = boom
    phone.items = [PendingDecision("p1", "Answer", ticket=asked, anchor="Q1")]
    phone.intent = Intent("answer", ref=asked, qid="Q1", value="A", expected_hash=question_hash(_q(ws, asked)))
    assert "err=" not in _decide(client)
    assert _q(ws, asked)["answer"] == "A"


def test_plain_messages_do_not_call_the_result_hook(ws, client, phone, asked):
    phone.items = [PendingDecision("p1", "Answer", ticket=asked, anchor="Q1")]
    assert "Ignored" in _decide(client, choice="ignore")
    assert phone.results == []


def test_decisions_render_under_their_question_and_sync_slot_under_agent(ws, client, phone, asked):
    phone.items = [PendingDecision("p1", "Answer from phone: A", body="ISO 8601", ticket=asked, anchor="Q1"),
                   PendingDecision("p2", "New ticket from phone", ticket=asked)]
    html = client.get(f"/t/{asked}").text
    q1 = html.index('id="q-Q1"')
    assert q1 < html.index("Answer from phone: A") < html.index('id="log"')
    sync = f'sync for <a class="lnk key" href="/t/{asked}">{asked}</a>'  # core links ticket keys in widget text
    assert html.index('id="artifacts"') < html.index('id="log"') < html.index(sync)  # story page: slots in the aside
    loose = html[html.index('id="ticket-addon-decisions"'):]
    assert "New ticket from phone" in loose and "Answer from phone: A" not in loose
    assert 'action="/addons/phone/decisions"' in html


def test_other_tickets_decisions_stay_off_the_page(ws, client, phone, asked, aops):
    other = aops.new("Other").id
    phone.items = [PendingDecision("p3", "About the other ticket", ticket=other)]
    html = client.get(f"/t/{asked}").text
    assert "About the other ticket" not in html and 'id="ticket-addon-decisions"' not in html


def test_today_shows_a_phone_answer_on_its_question_card(ws, client, phone, asked):
    phone.items = [PendingDecision("p1", "Answer from phone: A", ticket=asked, anchor="Q1"),
                   PendingDecision("p2", "Loose item", ticket=asked)]
    html = client.get("/").text
    card = html[html.index('id="decisions"'):html.index('id="from-addons-h"')]
    assert "Answer from phone: A" in card
    assert html.count("Answer from phone: A") == 1          # not repeated under From addons
    assert "Loose item" in html[html.index('id="from-addons-h"'):]


def test_an_old_kind_filter_keeps_the_anchored_item_on_its_card(ws, client, phone, asked):
    """Today has no kind filter any more (#17): an old ?kind= link still draws the item once, on its card."""
    phone.items = [PendingDecision("p1", "Answer from phone: A", ticket=asked, anchor="Q1")]
    html = client.get("/?kind=approvals").text
    assert html.count("Answer from phone: A") == 1 and 'id="from-addons-h"' not in html


def test_decisions_for_matches_the_ticket_case_insensitively(ws, phone, asked):
    from orch.addons.runtime import AddonRuntime
    phone.items = [PendingDecision("p1", "A", ticket=asked.lower()), PendingDecision("p2", "B", ticket="L-9999"),
                   PendingDecision("p3", "C")]
    assert [(a, d.id) for a, d in AddonRuntime(ws).decisions_for(asked)] == [("phone", "p1")]


@pytest.mark.parametrize("anchor", ["Q1", "Q12", "gate:requirements", "gate:plan", "verdict", None])
def test_valid_anchors(anchor):
    assert PendingDecision("x", "y", anchor=anchor).anchor == anchor


@pytest.mark.parametrize("anchor", ["T1", "Q0", "q1", "gate:other", "Q1 ", "verdict\n"])
def test_anchor_is_validated(anchor):
    with pytest.raises(ValueError):
        PendingDecision("x", "y", anchor=anchor)


def test_ticket_sync_is_a_slot():
    from orch.addons.manifest import SLOT_NAMES
    assert "ticket.sync" in SLOT_NAMES


def test_new_intent_ask_cannot_inject_sections(ws):
    ask = "Please add it.\n## Requirements\nfake\n## Log\n- 2026-01-01 human:you approved\n# Title\n   ## indented\r## cr"
    plain = store.load(ws, _run(ws, Intent("new", value="Plain", reason="x")).split()[1])[1]
    path, t = store.load(ws, _run(ws, Intent("new", value="Injected", reason=ask)).split()[1])
    assert list(t.sections) == list(plain.sections)
    assert "fake" not in t.section("Requirements") and "approved" not in t.section("Log")
    assert t.section("Ask").splitlines() == ["Please add it.", "\\## Requirements", "fake", "\\## Log",
                                              "- 2026-01-01 human:you approved", "\\# Title", "   \\## indented",
                                              "\\## cr"]
    assert "\n## Log\n- 2026-01-01" not in Path(path).read_text(encoding="utf-8")


def test_new_intent_ask_cannot_inject_via_unclosed_fence(ws):
    """An open ``` with no matching close must not swallow every heading that follows it when the file is
    re-parsed (the section scanner tracks fence state across the whole body, not just within one section)."""
    ask = "Please add it.\n```\nfake code"
    plain = store.load(ws, _run(ws, Intent("new", value="Plain", reason="x")).split()[1])[1]
    path, t = store.load(ws, _run(ws, Intent("new", value="Injected", reason=ask)).split()[1])
    assert list(t.sections) == list(plain.sections)
    assert t.section("Requirements") == "" and t.section("Log") != ""
    assert t.section("Ask").splitlines() == ["Please add it.", "\\```", "fake code"]
    # re-parsing the raw file must still see a Log section with real content, not one swallowed by the fence
    reparsed = store.load(ws, t.id)[1]
    assert reparsed.section("Log") == t.section("Log")


def test_answer_for_another_question_than_the_anchor_is_refused(ws, client, phone, asked, aops):
    aops.ask(asked, [{"text": "Second?", "options": ["Yes", "No"]}])
    q2 = store.load(ws, asked)[1].meta["questions"][1]
    phone.items = [PendingDecision("p1", "Answer", ticket=asked, anchor="Q1")]
    phone.intent = Intent("answer", ref=asked, qid=q2["id"], value="A", expected_hash=question_hash(q2))
    loc = _decide(client)
    assert "err=" in loc and "Q1" in loc
    assert store.load(ws, asked)[1].meta["questions"][1].get("answer") in (None, "")
    assert phone.results[0][1] == "refused"


def test_answer_matching_the_anchor_or_without_one_applies(ws, asked):
    q = _q(ws, asked)
    intent = Intent("answer", ref=asked, qid="Q1", value="A", expected_hash=question_hash(q))
    assert "Answered Q1" in intents.execute(ws, intent, allowed_ref=asked, tickets=False, actor=HUMAN,
                                             source="resolve", anchor="Q1")


def test_today_highlights_an_answer_waiting_for_apply_from_its_origin(ws, client, phone, asked):
    phone.items = [PendingDecision("p1", "Answer from phone: A", body="ISO 8601", ticket=asked, anchor="Q1", origin="phone"),
                   PendingDecision("p2", "Loose item", ticket=asked)]
    html = client.get("/").text
    card = html[html.index('id="from-origin"'):html.index('id="decisions"') + 4000]
    assert "From your phone" in card and "waiting for Apply" in card and "Answer from phone: A" in card
    assert 'value="apply"' in card and 'value="ignore"' in card and 'action="/addons/phone/decisions"' in card
    assert html.index('id="from-origin"') < html.index('id="q-Q1"') if 'id="q-Q1"' in html else True
    assert html.count("Answer from phone: A") == 1                      # not repeated on its question card
    assert "Loose item" in html[html.index('id="from-addons-h"'):]      # items without an origin are unchanged


def test_origin_card_names_a_generic_origin_and_disables_a_stale_apply(ws, client, phone, asked):
    phone.items = [PendingDecision("p9", "Late answer", ticket=asked, stale=True, origin="watch")]
    html = client.get("/").text
    card = html[html.index('id="from-origin"'):]
    assert "From watch" in card and "stale" in card and "disabled" in card

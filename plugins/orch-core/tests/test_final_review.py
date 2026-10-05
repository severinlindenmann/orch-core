"""Final review of the night build: a changed approval is never accepted with a verdict, the human reads what each
decision binds, and the dashboard binds what it showed."""
import re

import pytest

pytest.importorskip("fastapi")

from orch.core import epics, store  # noqa: E402
from orch.core.query import needs_you  # noqa: E402


def _ready(ops, title="x", size="m", epic=None):
    t = ops.new(title, size=size, **({"epic": epic} if epic else {}))
    ops.set_section(t.id, "Requirements", "r")
    ops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    return t.id


def _to_testing(aops, hops, tid, close_tasks, *, approve=True):
    if approve:
        hops.approve(tid, "requirements")
    aops.claim(tid)
    aops.set_section(tid, "Plan", "1. do it")
    if approve:
        hops.approve(tid, "plan")
    close_tasks(aops, tid)
    aops.set_section(tid, "Verification", "- AC1: ran it")
    aops.move(tid, "testing")


# -- C1: no verdict offered on a ticket whose approved text changed -----------------------------------------------

def test_today_offers_no_verdict_for_a_testing_ticket_with_changed_requirements(dash, ws, aops, hops, close_tasks):
    tid = _ready(aops)
    _to_testing(aops, hops, tid, close_tasks)
    assert (tid, "verdict") in {(i["ticket"], i["kind"]) for i in needs_you(ws)}
    aops.set_section(tid, "Requirements", "r, and more")
    kinds = {(i["ticket"], i["kind"]) for i in needs_you(ws)}
    assert (tid, "verdict") not in kinds and (tid, "re-approve") in kinds
    assert "Accept, mark done" not in dash.get("/").text
    page = dash.get(f"/t/{tid}").text
    assert "Accept, mark done" not in page
    assert "Re-approve requirements" in page and "Requirements changed after approval" in page


def test_epic_verdict_is_not_offered_while_a_child_changed(dash, ws, aops, hops, close_tasks):
    e = aops.new("Billing", type="epic")
    aops.set_section(e.id, "Requirements", "the epic")
    aops.set_section(e.id, "Acceptance criteria", "- [ ] all done")
    cid = _ready(aops, "child", epic=e.id)
    aops.set_section(cid, "Plan", "1. do it")
    hops.approve(e.id, "requirements")
    aops.claim(cid)
    close_tasks(aops, cid)
    aops.set_section(cid, "Verification", "- AC1: ran it")
    aops.move(cid, "testing")
    assert "Accept the epic and close" in dash.get(f"/t/{e.id}").text
    aops.set_section(cid, "Requirements", "r, and more")
    page = dash.get(f"/t/{e.id}").text
    assert "Accept the epic and close" not in page
    assert "Accept, mark done" not in dash.get(f"/t/{cid}").text
    assert (cid, "verdict") not in {(i["ticket"], i["kind"]) for i in needs_you(ws)}


# -- I2: the dashboard verdict binds the criteria and evidence the card showed ------------------------------------

def _seen_in(html, tid):
    forms = re.findall(r'<form method="post" action="/t/%s/verdict".*?</form>' % tid, html, re.S)
    assert forms, "no verdict form"
    return [re.search(r'name="seen" value="([^"]+)"', f).group(1) for f in forms]


@pytest.mark.parametrize("where", ["/", "/t/{tid}"])
def test_verdict_forms_post_the_verdict_hash(dash, ws, aops, hops, close_tasks, where):
    tid = _ready(aops)
    _to_testing(aops, hops, tid, close_tasks)
    want = epics.verdict_hash([store.load(ws, tid)[1]], ws)
    assert set(_seen_in(dash.get(where.format(tid=tid)).text, tid)) == {want}


def test_dashboard_verdict_without_or_with_a_stale_hash_is_refused(dash, ws, aops, hops, close_tasks):
    tid = _ready(aops)
    _to_testing(aops, hops, tid, close_tasks)
    seen = _seen_in(dash.get(f"/t/{tid}").text, tid)[0]
    r = dash.post(f"/t/{tid}/verdict", data={"verdict": "done"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    aops.set_section(tid, "Verification", "- AC1: something else")
    r = dash.post(f"/t/{tid}/verdict", data={"verdict": "done", "seen": seen}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert store.load(ws, tid)[1].status == "testing"
    seen = _seen_in(dash.get(f"/t/{tid}").text, tid)[0]
    r = dash.post(f"/t/{tid}/verdict", data={"verdict": "done", "seen": seen}, follow_redirects=False)
    assert "err=" not in r.headers["location"]
    assert store.load(ws, tid)[1].status == "done"


# -- I3: the charter form marks children the approval cannot cover ------------------------------------------------

def _epic_with(aops):
    e = aops.new("Billing", type="epic")
    aops.set_section(e.id, "Requirements", "the epic")
    aops.set_section(e.id, "Acceptance criteria", "- [ ] all done")
    good = _ready(aops, "good", epic=e.id)
    stub = aops.new("stub", epic=e.id).id
    return e.id, good, stub


def _charter(html):
    return html.split('id="epic-approve"', 1)[1].split("</section>", 1)[0]


def test_charter_form_marks_unready_children_and_disables_approve(dash, ws, aops, hops):
    eid, good, stub = _epic_with(aops)
    form = _charter(dash.get(f"/t/{eid}").text)
    child = re.search(r'<details class="charter-child[^"]*" data-child="%s".*?</details>\s*</details>' % stub, form, re.S)
    assert child and "not ready" in child.group(0).lower() and "Requirements, Acceptance criteria empty" in child.group(0)
    button = re.search(r'<button type="submit" class="btn btn-primary"[^>]*>', form).group(0)
    assert "disabled" in button
    assert stub in form.split("<button", 1)[1] or "cannot approve" in form.lower()
    assert "ready" not in dash.get(f"/t/{eid}").text.split('class="status-card', 1)[1].split("</section>", 1)[0].lower()
    aops.set_section(stub, "Requirements", "r")
    aops.set_section(stub, "Acceptance criteria", "- [ ] a")
    form = _charter(dash.get(f"/t/{eid}").text)
    assert "disabled" not in re.search(r'<button type="submit" class="btn btn-primary"[^>]*>', form).group(0)
    assert "not ready" not in form.lower()


def test_charter_readiness_is_the_backends(ws, aops, hops):
    from orch.errors import ValidationError
    eid, good, stub = _epic_with(aops)
    why = epics.charter_blocker(store.load(ws, stub)[1])
    assert why and "Requirements, Acceptance criteria empty" in why
    assert epics.charter_blocker(store.load(ws, good)[1]) is None
    with pytest.raises(ValidationError, match=re.escape(why)):
        hops.approve(eid, "requirements")


# -- M6: a re-approval waits since the edit that changed the gate, not since `orch check` noticed --------------------

def test_reapprove_age_counts_from_the_edit_not_from_gate_invalidated(ws, aops, hops):
    from datetime import timedelta

    from orch.clock import now as clock_now
    from orch.clock import stamp_s
    from orch.core.events import Event, read_events
    from orch.dashboard.data.decisions import decisions
    tid = _ready(aops)
    hops.approve(tid, "requirements")
    aops.set_section(tid, "Requirements", "r, and more")  # the edit that invalidates the approval
    later = clock_now() + timedelta(hours=3)
    evs = read_events(ws) + [Event(10_000, stamp_s(later), tid, "gate.invalidated", "system", "check",
                                   {"gate": "requirements"}, None)]
    d = next(x for x in decisions(ws, now=later + timedelta(hours=1), events=evs)
             if x.ticket == tid and x.kind == "re-approve")
    assert d.age_minutes is not None and d.age_minutes >= 4 * 60 - 1


# -- fix round 1 ----------------------------------------------------------------------------------------------------

def _changed_in_testing(aops, hops, close_tasks):
    tid = _ready(aops)
    _to_testing(aops, hops, tid, close_tasks)
    aops.set_section(tid, "Requirements", "r, and more")
    return tid


def test_ops_refuses_a_done_verdict_on_a_changed_approval_and_allows_send_back(ws, aops, hops, human, close_tasks):
    from orch.core.ops import Ops
    from orch.errors import ValidationError
    tid = _changed_in_testing(aops, hops, close_tasks)
    seen = epics.verdict_hash([store.load(ws, tid)[1]], ws)
    with pytest.raises(ValidationError, match="requirements of .* changed since approval: re-approve or send back"):
        Ops(ws, human).verdict(tid, "done", expected_hash=seen)
    Ops(ws, human).verdict(tid, "follow-up", "redo it", expected_hash=seen)
    assert store.load(ws, tid)[1].status == "in-progress"


def test_dashboard_done_verdict_on_a_changed_approval_is_refused(dash, ws, aops, hops, close_tasks):
    tid = _changed_in_testing(aops, hops, close_tasks)
    seen = epics.verdict_hash([store.load(ws, tid)[1]], ws)
    r = dash.post(f"/t/{tid}/verdict", data={"verdict": "done", "seen": seen}, follow_redirects=False)
    assert "err=" in r.headers["location"] and store.load(ws, tid)[1].status == "testing"


def test_epic_verdict_refuses_a_child_with_a_changed_approval(ws, aops, hops, human, close_tasks):
    from orch.core.ops import Ops
    from orch.errors import ValidationError
    e = aops.new("Billing", type="epic")
    aops.set_section(e.id, "Requirements", "the epic")
    aops.set_section(e.id, "Acceptance criteria", "- [ ] all done")
    cid = _ready(aops, "child", epic=e.id)
    aops.set_section(cid, "Plan", "1. do it")
    hops.approve(e.id, "requirements")
    aops.claim(cid)
    close_tasks(aops, cid)
    aops.set_section(cid, "Verification", "- AC1: ran it")
    aops.move(cid, "testing")
    aops.set_section(cid, "Requirements", "r, and more")
    seen = epics.verdict_hash(epics.open_children(ws, store.load(ws, e.id)[1]), ws)
    with pytest.raises(ValidationError, match="changed since approval"):
        Ops(ws, human).verdict(e.id, "done", expected_hash=seen)
    assert store.load(ws, cid)[1].status == "testing" and store.load(ws, e.id)[1].status == "open"


@pytest.mark.parametrize("call", [
    lambda ops, tid: ops.approve(tid, "requirements"),
    lambda ops, tid: ops.answer(tid, "Q1", "A"),
    lambda ops, tid: ops.verdict(tid, "follow-up", "redo"),
])
def test_every_human_decision_needs_the_hash_of_what_was_read(ws, aops, human, call):
    from orch.core.ops import Ops
    from orch.core.questions import parse_ask_file
    from orch.errors import ValidationError
    tid = _ready(aops)
    aops.ask(tid, parse_ask_file("questions:\n  - text: One repo?\n    options: [yes, no]\n"))
    with pytest.raises(ValidationError, match="needs the hash of what you read"):
        call(Ops(ws, human), tid)


def test_addon_verdict_intent_needs_the_verdict_hash(ws, aops, hops, human, close_tasks):
    from orch.addons import intents
    from orch.addons.api import Intent
    from orch.errors import ValidationError
    tid = _ready(aops)
    _to_testing(aops, hops, tid, close_tasks)
    with pytest.raises(ValidationError, match="expected_hash"):
        intents.execute(ws, Intent("verdict", ref=tid, value="done"), allowed_ref=tid, tickets=False, actor=human,
                        source="resolve")
    seen = epics.verdict_hash([store.load(ws, tid)[1]], ws)
    intents.execute(ws, Intent("verdict", ref=tid, value="done", expected_hash=seen), allowed_ref=tid,
                    tickets=False, actor=human, source="resolve")
    assert store.load(ws, tid)[1].status == "done"
    from orch.core import ledger
    assert [e for e in ledger.entries(ws) if e["kind"] == "verdict"][-1]["verdict_hash"] == seen


def test_the_ticket_document_publishes_the_verdict_hash(ws, aops, hops, close_tasks):
    from orch.core.events import latest_testing_round, read_events
    from orch.core.schema import SCHEMA_VERSION, ticket_document, ticket_schema
    assert SCHEMA_VERSION == "1.9.0"  # 1.4: together on approve-requirements needs (F2); 1.5: artifact_items
    tid = _ready(aops)
    assert ticket_document(ws, store.load(ws, tid)[1])["verdict"] is None
    _to_testing(aops, hops, tid, close_tasks)
    t = store.load(ws, tid)[1]
    doc = ticket_document(ws, t)
    assert doc["verdict"] == {"hash": epics.verdict_hash([t], ws), "round": latest_testing_round(read_events(ws, tid))}
    import jsonschema
    jsonschema.validate(doc, ticket_schema())

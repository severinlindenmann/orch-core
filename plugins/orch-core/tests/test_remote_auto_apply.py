"""R21: a decision from a paired phone that passes every check applies at once, as the human: approvals, answers,
change requests, verdicts and ticket requests. No second desktop step. Revoked or unknown phones and failed checks
still never apply; the per-kind switches stay for the owner to turn a kind off."""
import json
from datetime import datetime, timezone

import pytest

from orch.core import ledger as signed_ledger
from orch.core import store
from orch.core.check import run_checks
from orch.core.events import Actor, append_event, read_events
from orch.core.gates import gate_hash
from orch.remote import ledger
from orch.remote import store as phones
from orch.remote.verify import mac_of, verify_and_apply

NOW = datetime(2026, 10, 2, 9, 41, tzinfo=timezone.utc)
AT = "2026-10-02T09:41:07Z"


@pytest.fixture
def phone(ws):
    return phones.pair(ws.root, label="iPhone", addon="tixlike")[0]


def _sign(phone, d):
    d = {k: v for k, v in d.items() if k != "mac"}
    d["mac"] = mac_of(phone.key, d)
    return d


def _approve(phone, tid, t, gate="requirements", letter="c"):
    return _sign(phone, {"v": 1, "decision_id": "dec_" + letter * 32, "space": "s", "ticket": tid, "kind": "approve",
                         "target": {"gate": gate, "hash": gate_hash(t, gate)}, "value": "approve", "note": "",
                         "device": "iPhone · Safari", "at": AT, "pair": phone.id})


def _request(phone, letter="d", **over):
    d = {"v": 1, "decision_id": "dec_" + letter * 32, "kind": "ticket_request", "space": "s" * 32, "at": AT,
         "value": {"title": "Export as CSV", "body": "From the phone: the weekly report as CSV."},
         "device": "iPhone · Safari", "pair": phone.id}
    d.update(over)
    return _sign(phone, d)


def _unverified(ws):
    return [f for f in run_checks(ws, emit_events=False) if f.code == "unverified-remote"]


def test_defaults_let_a_paired_phone_decide_everything():
    assert phones.DEFAULT_PERMISSIONS == {"answer": True, "request_changes": True, "approve": True, "verdict": True,
                                          "ticket_request": True}
    assert "move" not in phones.KINDS


def test_a_phone_approval_applies_without_any_setting(ws, put, phone):
    tid = put("backlog", title="r", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    r = verify_and_apply(ws, _approve(phone, tid, store.load(ws, tid)[1]), addon="tixlike", now=NOW)
    assert r.status == "applied", r.message
    t = store.load(ws, tid)[1]
    assert t.status == "open" and t.meta["gates"]["requirements"]["via"] == "phone:iPhone"
    assert not _unverified(ws)


def test_the_signed_ledger_entry_names_the_phone_and_the_device(ws, put, phone):
    tid = put("backlog", title="r", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    verify_and_apply(ws, _approve(phone, tid, store.load(ws, tid)[1]), addon="tixlike", now=NOW)
    entry = [e for e in signed_ledger.entries(ws) if e.get("kind") == "gate" and e.get("ticket") == tid][-1]
    assert entry["via"] == "phone:iPhone" and entry["device"] == phone.id
    t = store.load(ws, tid)[1]
    assert signed_ledger.gate_verification(ws, t, "requirements") == "verified"


def test_a_phone_verdict_applies_without_any_setting(ws, put, phone):
    from orch.core.epics import verdict_hash
    tid = put("testing", title="v", sections={"Verification": "- AC1: ok"})
    d = _sign(phone, {"v": 1, "decision_id": "dec_" + "e" * 32, "space": "s", "ticket": tid, "kind": "verdict",
                      "target": {"status": "testing", "round": 0, "hash": verdict_hash([store.load(ws, tid)[1]], ws)},
                      "value": "done", "note": "", "device": "x", "at": AT, "pair": phone.id})
    r = verify_and_apply(ws, d, addon="tixlike", now=NOW)
    assert r.status == "applied", r.message
    assert store.load(ws, tid)[1].status == "done"
    assert not _unverified(ws)


def test_the_owner_can_still_turn_a_kind_off(ws, put, phone):
    phones.set_permissions(ws.root, {k: k != "approve" for k in phones.KINDS})
    tid = put("backlog", title="r", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    r = verify_and_apply(ws, _approve(phone, tid, store.load(ws, tid)[1]), addon="tixlike", now=NOW)
    assert r.status == "pending" and store.load(ws, tid)[1].status == "backlog"


def test_a_revoked_phone_still_never_applies(ws, put, phone):
    tid = put("backlog", title="r", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    d = _approve(phone, tid, store.load(ws, tid)[1])
    phones.revoke(ws.root, phone.id)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"
    assert store.load(ws, tid)[1].status == "backlog"
    assert verify_and_apply(ws, _request(phone), addon="tixlike", now=NOW).status == "pending"
    assert store.scan(ws) and len(store.scan(ws)) == 1


def test_saved_permissions_that_equal_the_old_defaults_read_as_the_new_ones(ws):
    """Saving the Phones form before R21 stored the old defaults (approve and verdict off) without the owner
    choosing them; read those as untouched. Any other saved choice stands."""
    def save(raw):
        phones._write(lambda data: phones._entry(data, ws.root).__setitem__("permissions", raw))
    save({"answer": True, "request_changes": True, "approve": False, "verdict": False})
    assert phones.permissions(ws.root)["approve"] is True and phones.permissions(ws.root)["verdict"] is True
    save({"answer": True, "request_changes": False, "approve": False, "verdict": False})
    assert phones.permissions(ws.root) == {"answer": True, "request_changes": False, "approve": False,
                                           "verdict": False, "ticket_request": False}  # follows approve
    phones.set_permissions(ws.root, {"answer": True, "request_changes": True})  # saved now: exactly as chosen
    assert phones.permissions(ws.root)["approve"] is False and phones.permissions(ws.root)["ticket_request"] is False


# -- ticket requests -----------------------------------------------------------------------------------------------

def test_a_signed_ticket_request_creates_a_backlog_ticket_as_the_phone(ws, phone):
    r = verify_and_apply(ws, _request(phone), addon="tixlike", now=NOW)
    assert r.status == "applied", r.message
    t = store.load(ws, r.ticket)[1]
    assert t.status == "backlog" and t.title == "Export as CSV"
    assert t.section("Ask").strip() == "From the phone: the weekly report as CSV."
    created = [e for e in read_events(ws, r.ticket) if e.kind == "ticket.created"]
    assert created and created[0].via == "phone:iPhone" and created[0].actor == "human:you"
    assert r.event_seq == created[0].seq
    entry = ledger.entries(ws)[-1]
    assert entry["kind"] == "ticket_request" and entry["ticket"] == r.ticket and entry["status"] == "applied"
    assert not _unverified(ws)


def test_a_ticket_request_replay_is_a_duplicate(ws, phone):
    d = _request(phone)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "applied"
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "duplicate"
    assert len(store.scan(ws)) == 1


@pytest.mark.parametrize("change", [{"value": {"title": "Something else", "body": ""}}, {"at": "2026-10-02T09:41:08Z"},
                                    {"kind": "answer"}])
def test_a_tampered_ticket_request_is_pending(ws, phone, change):
    d = _request(phone)
    d.update(change)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"
    assert store.scan(ws) == []


def test_an_unsigned_ticket_request_is_pending(ws, phone):
    d = _request(phone)
    d.pop("mac")
    d.pop("pair")
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"
    assert store.scan(ws) == []


@pytest.mark.parametrize("value", [None, "Export", {"title": ""}, {"title": "   "}, {"title": "x" * 201},
                                   {"title": "ok", "body": 3}, {"title": 5}])
def test_a_malformed_ticket_request_is_pending(ws, phone, value):
    assert verify_and_apply(ws, _request(phone, value=value), addon="tixlike", now=NOW).status == "pending"
    assert store.scan(ws) == []


def test_a_ticket_request_naming_a_ticket_is_pending(ws, phone, put):
    tid = put("backlog", title="x")
    assert verify_and_apply(ws, _request(phone, ticket=tid), addon="tixlike", now=NOW).status == "pending"
    assert len(store.scan(ws)) == 1


def test_a_ticket_request_switched_off_is_pending(ws, phone):
    phones.set_permissions(ws.root, {k: k != "ticket_request" for k in phones.KINDS})
    assert verify_and_apply(ws, _request(phone), addon="tixlike", now=NOW).status == "pending"
    assert store.scan(ws) == []


def test_a_ticket_request_body_cannot_forge_sections(ws, phone):
    r = verify_and_apply(ws, _request(phone, value={"title": "x", "body": "a\n## Requirements\nevil\n```"}),
                         addon="tixlike", now=NOW)
    assert r.status == "applied"
    t = store.load(ws, r.ticket)[1]
    assert not t.section("Requirements").strip() and "evil" in t.section("Ask")


def test_a_ticket_request_with_a_voice_note_still_verifies(ws, phone):
    d = _request(phone, voice={"file": "FILE12", "transcript": "the weekly report"})
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "applied"


def test_an_old_ticket_request_is_stale(ws, phone):
    assert verify_and_apply(ws, _request(phone, at="2026-09-01T00:00:00Z"), addon="tixlike", now=NOW).status == "stale"
    assert store.scan(ws) == []


def test_a_forged_phone_ticket_creation_is_flagged(ws, put):
    tid = put("backlog", title="x")
    append_event(ws, tid, "ticket.created", Actor("human", "you", "phone:forged"), {"title": "x"})
    assert _unverified(ws)


# -- the Phones card says so -----------------------------------------------------------------------------------------

def test_the_phones_card_says_phone_decisions_apply_at_once(ws):
    from addon_fixtures import loaded
    from orch.addons.api import PairingTarget
    from orch.addons.loader import AddonRegistry

    class Tixlike:
        def pairing_target(self, view):
            return PairingTarget("https://x.invalid/pair#sp1", "TIX")

        def decisions(self, view):
            return []
    la = loaded(ws, Tixlike(), name="tixlike", capabilities=["decisions"], remote_humans=True, menu=None,
                settings_schema=[])
    ws._addons = AddonRegistry(ws, {"tixlike": la})
    phones.pair(ws.root, label="iPhone", addon="tixlike")
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    html = c.get("/workspace?tab=phones").text
    assert "desktop Apply" not in html
    assert "apply at once" in html and "Ticket requests apply directly" in html
    assert html.count('type="checkbox"') >= 5


# -- the dashboard shows phone-applied decisions as receipts -------------------------------------------------------

def test_today_shows_a_receipt_for_a_phone_approval(ws, put, phone, dash):
    tid = put("backlog", title="r", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    verify_and_apply(ws, _approve(phone, tid, store.load(ws, tid)[1]), addon="tixlike", now=NOW)
    html = dash.get("/").text
    assert "phone-receipts" in html
    assert f"Approved requirements of {tid}" in html and "from your phone (iPhone)" in html
    assert "Apply" not in html.split('id="phone-receipts"', 1)[1].split("</section>", 1)[0]


def test_today_shows_a_receipt_for_a_phone_ticket_request(ws, phone, dash):
    r = verify_and_apply(ws, _request(phone), addon="tixlike", now=NOW)
    html = dash.get("/").text
    assert f"Created {r.ticket}" in html and "from your phone (iPhone)" in html


def test_a_forged_phone_event_gets_no_receipt(ws, put, dash):
    tid = put("backlog", title="x")
    append_event(ws, tid, "gate.approved", Actor("human", "you", "phone:forged"), {"gate": "requirements"})
    html = dash.get("/").text
    assert "from your phone" not in html and "phone-receipts" not in html


def test_the_ticket_page_says_an_approval_came_from_the_phone(ws, put, phone, dash):
    tid = put("backlog", title="r", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    verify_and_apply(ws, _approve(phone, tid, store.load(ws, tid)[1]), addon="tixlike", now=NOW)
    html = dash.get(f"/t/{tid}").text
    assert "from your phone (iPhone)" in html and "· phone:iPhone" not in html


# -- F2 from the phone: requirements and plan in one signed decision ------------------------------------------------

@pytest.fixture
def drafted(aops):
    t = aops.new("Export as CSV")
    aops.set_section(t.id, "Requirements", "- CSV download")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] one row per job")
    aops.set_section(t.id, "Plan", "1. writer\n2. button")
    return t.id


def _together(phone, ws, tid, letter="7", **target_over):
    t = store.load(ws, tid)[1]
    target = {"gate": "requirements", "hash": gate_hash(t, "requirements"), "plan_hash": gate_hash(t, "plan")}
    target.update(target_over)
    return _sign(phone, {"v": 1, "decision_id": "dec_" + letter * 32, "space": "s", "ticket": tid, "kind": "approve",
                         "target": target, "value": "approve", "note": "", "device": "x", "at": AT, "pair": phone.id})


def test_the_document_says_requirements_and_plan_can_be_approved_together(ws, drafted):
    from orch.core.schema import SCHEMA_VERSION, ticket_document
    doc = ticket_document(ws, store.load(ws, drafted)[1])
    need = next(n for n in doc["needs"] if n["kind"] == "approve-requirements")
    assert need["together"] is True and tuple(map(int, SCHEMA_VERSION.split("."))) >= (1, 4, 0)


def test_a_phone_approves_requirements_and_plan_together(ws, phone, drafted):
    r = verify_and_apply(ws, _together(phone, ws, drafted), addon="tixlike", now=NOW)
    assert r.status == "applied", r.message
    t = store.load(ws, drafted)[1]
    assert t.status == "open"
    assert t.meta["gates"]["plan"]["via"] == "phone:iPhone" and t.meta["gates"]["requirements"]["via"] == "phone:iPhone"
    signed = [e for e in signed_ledger.entries(ws) if e["kind"] == "gate" and e["ticket"] == drafted]
    assert {e["gate"] for e in signed} == {"requirements", "plan"} and all(e["device"] == phone.id for e in signed)
    assert not _unverified(ws)


def test_a_phone_together_approval_on_a_changed_plan_is_stale(ws, phone, drafted, aops):
    d = _together(phone, ws, drafted)
    aops.set_section(drafted, "Plan", "1. something else")
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "stale"
    assert store.load(ws, drafted)[1].status == "backlog"


@pytest.mark.parametrize("over", [{"plan_hash": ""}, {"plan_hash": 5}, {"gate": "plan"}])
def test_a_malformed_together_target_is_pending(ws, phone, drafted, over):
    assert verify_and_apply(ws, _together(phone, ws, drafted, **over), addon="tixlike", now=NOW).status == "pending"
    assert store.load(ws, drafted)[1].status == "backlog"


# -- security review round: the signed ledger backs every phone receipt -----------------------------------------------

def _forge_remote(ws, tid, kind, data, letter="8"):
    """An agent-writable remote ledger line plus a phone event: what an attacker inside the repository could write."""
    e = append_event(ws, tid, kind, Actor("human", "you", "phone:iPhone"), data)
    with ledger.lock(ws):
        ledger.append(ws, {"decision_id": "dec_" + letter * 32, "ticket": tid, "status": "applied", "event_seq": e.seq,
                           "event_seqs": [e.seq]})
    return e


def test_a_receipt_without_a_signed_entry_is_shown_unverified(ws, put, dash):
    tid = put("backlog", title="x")
    _forge_remote(ws, tid, "gate.approved", {"gate": "requirements", "hash": "sha256:" + "0" * 64})
    box = dash.get("/").text.split('id="phone-receipts"', 1)[1].split("</section>", 1)[0]
    assert f"Approved requirements of {tid}" in box and "unverified" in box


def test_a_real_phone_approval_receipt_is_not_unverified(ws, put, phone, dash):
    tid = put("backlog", title="r", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    verify_and_apply(ws, _approve(phone, tid, store.load(ws, tid)[1]), addon="tixlike", now=NOW)
    box = dash.get("/").text.split('id="phone-receipts"', 1)[1].split("</section>", 1)[0]
    assert "unverified" not in box


def test_check_flags_a_phone_event_the_signed_ledger_does_not_hold(ws, put):
    tid = put("backlog", title="x")
    _forge_remote(ws, tid, "question.answered", {"qid": "Q1", "answer": "A"})
    found = _unverified(ws)
    assert found and "not in the approval ledger" in found[0].message


def test_a_ticket_request_is_signed_into_the_ledger(ws, phone):
    r = verify_and_apply(ws, _request(phone), addon="tixlike", now=NOW)
    entry = [e for e in signed_ledger.entries(ws) if e["kind"] == "ticket_request"][-1]
    assert entry["ticket"] == r.ticket and entry["decision"] == "dec_" + "d" * 32
    assert entry["via"] == "phone:iPhone" and entry["device"] == phone.id
    assert not _unverified(ws)


def test_saved_permissions_without_ticket_request_follow_approve(ws):
    def save(raw):
        phones._write(lambda data: phones._entry(data, ws.root).__setitem__("permissions", raw))
    save({"answer": True, "request_changes": True, "approve": True, "verdict": False})
    assert phones.permissions(ws.root)["ticket_request"] is True
    save({"answer": True, "request_changes": False, "approve": False, "verdict": True})
    assert phones.permissions(ws.root)["ticket_request"] is False

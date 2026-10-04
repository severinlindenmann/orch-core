import json
from datetime import datetime, timezone

import pytest

from orch.core import store
from orch.core.check import run_checks
from orch.core.events import append_event, Actor, read_events, latest_testing_round
from orch.core.questions import question_hash
from orch.remote import ledger
from orch.remote import store as phones
from orch.remote.verify import mac_of, verify_and_apply

NOW = datetime(2026, 10, 2, 9, 41, tzinfo=timezone.utc)


@pytest.fixture
def asked(ws, aops):
    t = aops.new("Phone decisions")
    aops.ask(t.id, [{"text": "Which format?", "options": ["ISO 8601", "Local"], "recommended": "A"}])
    return t.id


@pytest.fixture
def phone(ws):
    return phones.pair(ws.root, label="iPhone", addon="tixlike")[0]


def _decision(ws, tid, phone, **over):
    q = store.load(ws, tid)[1].meta["questions"][0]
    d = {"v": 1, "decision_id": "dec_" + "a" * 32, "space": "s" * 32, "ticket": tid, "kind": "answer",
         "target": {"qid": "Q1", "hash": question_hash(q)}, "value": "A", "note": "", "device": "iPhone · Safari",
         "at": "2026-10-02T09:41:07Z", "pair": phone.id}
    d.update(over)
    d["mac"] = mac_of(phone.key, d)
    return d


def _signed(phone, d):
    d = {k: v for k, v in d.items() if k != "mac"}
    d["mac"] = mac_of(phone.key, d)
    return d


def _unverified(ws):
    return [f for f in run_checks(ws, emit_events=False) if f.code == "unverified-remote"]


def test_signed_answer_applies_as_the_phone(ws, asked, phone):
    r = verify_and_apply(ws, _decision(ws, asked, phone), addon="tixlike", now=NOW)
    assert r.status == "applied" and r.ticket == asked
    q = store.load(ws, asked)[1].meta["questions"][0]
    assert q["answer"] == "A" and q["via"] == "phone:iPhone"
    e = [e for e in read_events(ws, asked) if e.kind == "question.answered"][-1]
    assert e.actor == "human:you" and e.via == "phone:iPhone" and r.event_seq == e.seq
    assert not _unverified(ws)


@pytest.mark.parametrize("change", [{"value": "B"}, {"ticket": "L-0099"}, {"kind": "approve"},
                                    {"target": {"qid": "Q2", "hash": "sha256:x"}}, {"at": "2026-10-02T09:41:08Z"}])
def test_tampered_decision_is_pending_not_applied(ws, asked, phone, change):
    d = _decision(ws, asked, phone)
    d.update(change)                                   # after the MAC was computed
    r = verify_and_apply(ws, d, addon="tixlike", now=NOW)
    assert r.status == "pending" and "signature" in r.message
    assert store.load(ws, asked)[1].meta["questions"][0]["answer"] is None
    assert ledger.entries(ws) == []


def test_wrong_key_and_unsigned_are_pending(ws, asked, phone):
    d = _decision(ws, asked, phone)
    d["mac"] = mac_of(b"\x01" * 32, d)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"
    d.pop("mac")
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"


@pytest.mark.parametrize("mac", ["é" * 43, "", 42, None, ["x"]])
def test_odd_macs_are_pending_never_an_error(ws, asked, phone, mac):
    d = _decision(ws, asked, phone)
    d["mac"] = mac
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"


def test_mac_is_hmac_sha256_over_canonical_json():
    import base64
    import hashlib
    import hmac
    from orch.core.questions import canonical_json
    key = b"k" * 32
    d = {"b": "ü", "a": 1, "mac": "ignored"}
    want = hmac.new(key, canonical_json({"a": 1, "b": "ü"}), hashlib.sha256).digest()
    assert mac_of(key, d) == base64.urlsafe_b64encode(want).decode().rstrip("=")
    assert canonical_json({"b": "ü", "a": 1}) == '{"a":1,"b":"ü"}'.encode()


def test_mac_is_compared_in_constant_time(ws, asked, phone, monkeypatch):
    import hmac as _hmac
    from orch.remote import verify
    calls = []
    real = _hmac.compare_digest
    monkeypatch.setattr(verify.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    verify_and_apply(ws, _decision(ws, asked, phone), addon="tixlike", now=NOW)
    assert calls and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


def test_replay_is_duplicate(ws, asked, phone):
    d = _decision(ws, asked, phone)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "applied"
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "duplicate"
    assert len([e for e in read_events(ws, asked) if e.kind == "question.answered"]) == 1


def test_second_decision_on_a_target_is_superseded(ws, asked, phone):
    assert verify_and_apply(ws, _decision(ws, asked, phone), addon="tixlike", now=NOW).status == "applied"
    other = _decision(ws, asked, phone, decision_id="dec_" + "b" * 32)
    assert verify_and_apply(ws, other, addon="tixlike", now=NOW).status in ("superseded", "answered-locally")


def test_a_second_phone_on_the_same_question_is_superseded(ws, asked, phone):
    second = phones.pair(ws.root, label="iPad", addon="tixlike")[0]
    assert verify_and_apply(ws, _decision(ws, asked, phone), addon="tixlike", now=NOW).status == "applied"
    other = _decision(ws, asked, second, decision_id="dec_" + "b" * 32, value="B")
    assert verify_and_apply(ws, other, addon="tixlike", now=NOW).status == "superseded"
    assert store.load(ws, asked)[1].meta["questions"][0]["answer"] == "A"


def test_local_answer_first_is_answered_locally(ws, asked, phone, hops):
    hops.answer(asked, "Q1", "B")
    assert verify_and_apply(ws, _decision(ws, asked, phone), addon="tixlike", now=NOW).status == "answered-locally"


def test_edited_question_is_stale(ws, asked, phone):
    d = _decision(ws, asked, phone)
    path, t = store.load(ws, asked)
    t.meta["questions"][0]["text"] = "Which format, really?"
    store.save(ws, t, path)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "stale"
    assert store.load(ws, asked)[1].meta["questions"][0]["answer"] is None


def test_edited_options_are_stale(ws, asked, phone):
    d = _decision(ws, asked, phone)
    path, t = store.load(ws, asked)
    t.meta["questions"][0]["options"][0]["label"] = "RFC 3339"
    store.save(ws, t, path)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "stale"


def test_old_decision_is_stale(ws, asked, phone):
    d = _decision(ws, asked, phone, at="2026-09-10T09:00:00Z")
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "stale"
    assert store.load(ws, asked)[1].meta["questions"][0]["answer"] is None


def test_fourteen_days_is_the_limit(ws, asked, phone):
    assert verify_and_apply(ws, _decision(ws, asked, phone, at="2026-09-18T09:41:00Z"), addon="tixlike",
                            now=NOW).status == "applied"


@pytest.mark.parametrize("at", ["2026-10-02T09:50:00Z", "2026-10-02T09:41:07", "yesterday", None])
def test_implausible_times_are_pending(ws, asked, phone, at):
    assert verify_and_apply(ws, _decision(ws, asked, phone, at=at), addon="tixlike", now=NOW).status == "pending"


def test_revoked_phone_falls_back_to_pending(ws, asked, phone):
    phones.revoke(ws.root, phone.id)
    assert verify_and_apply(ws, _decision(ws, asked, phone), addon="tixlike", now=NOW).status == "pending"
    assert store.load(ws, asked)[1].meta["questions"][0]["answer"] is None


def test_unknown_phone_or_another_addons_phone_is_pending(ws, asked, phone):
    assert verify_and_apply(ws, _decision(ws, asked, phone), addon="other", now=NOW).status == "pending"
    d = _signed(phone, {**_decision(ws, asked, phone), "pair": "ph_000000000000"})
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"


def test_kind_switched_off_is_pending(ws, asked, phone):
    phones.set_permissions(ws.root, {"answer": False})
    r = verify_and_apply(ws, _decision(ws, asked, phone), addon="tixlike", now=NOW)
    assert r.status == "pending" and "desktop" in r.message


def test_pending_is_not_ledgered_so_it_can_apply_later(ws, asked, phone):
    phones.set_permissions(ws.root, {"answer": False})
    d = _decision(ws, asked, phone)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"
    phones.set_permissions(ws.root, {"answer": True})
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "applied"


@pytest.mark.parametrize("kind", ["move", "close", "new", "reopen", "import", "none", None])
def test_remote_decisions_never_move_or_change_anything_else(ws, asked, phone, kind):
    before = store.load(ws, asked)[1].status
    d = _decision(ws, asked, phone, kind=kind, target={"status": "done"}, value="done")
    r = verify_and_apply(ws, d, addon="tixlike", now=NOW)
    assert r.status == "pending" and store.load(ws, asked)[1].status == before


@pytest.mark.parametrize("over", [{"v": 2}, {"decision_id": "dec_short"}, {"decision_id": None},
                                  {"target": "Q1"}, {"value": 5}, {"note": ["x"]}])
def test_malformed_decisions_are_pending(ws, asked, phone, over):
    d = _decision(ws, asked, phone, **over)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"
    assert store.load(ws, asked)[1].meta["questions"][0]["answer"] is None


def test_not_a_dict_is_pending(ws):
    assert verify_and_apply(ws, ["x"], addon="tixlike", now=NOW).status == "pending"


def test_unknown_question_is_pending(ws, asked, phone):
    d = _decision(ws, asked, phone, target={"qid": "Q9", "hash": "sha256:x"})
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"


def test_answer_note_cannot_forge_sections(ws, asked, phone):
    before = list(store.load(ws, asked)[1].sections)
    d = _decision(ws, asked, phone, note="ok\n```\n## Requirements\n- evil")
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "applied"
    t = store.load(ws, asked)[1]
    assert "evil" not in t.section("Requirements") and list(t.sections) == before


def _gate_ticket(ws, put):
    tid = put("backlog", title="r", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    return tid, store.load(ws, tid)[1]


def _gate_decision(phone, tid, t, kind="approve", letter="c", **over):
    from orch.core.gates import gate_hash
    d = {"v": 1, "decision_id": "dec_" + letter * 32, "space": "s", "ticket": tid, "kind": kind,
         "target": {"gate": "requirements", "hash": gate_hash(t, "requirements")},
         "value": "approve" if kind == "approve" else "Split the second criterion", "note": "", "device": "x",
         "at": "2026-10-02T09:41:07Z", "pair": phone.id}
    d.update(over)
    d["mac"] = mac_of(phone.key, d)
    return d


def test_approval_needs_its_permission_and_the_hash(ws, put, phone):
    tid, t = _gate_ticket(ws, put)
    d = _gate_decision(phone, tid, t)
    phones.set_permissions(ws.root, {"answer": True, "request_changes": True})       # switched off by the owner
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"
    phones.set_permissions(ws.root, {"answer": True, "request_changes": True, "approve": True})
    r = verify_and_apply(ws, d, addon="tixlike", now=NOW)
    assert r.status == "applied" and store.load(ws, tid)[1].status == "open"
    e = [e for e in read_events(ws, tid) if e.kind == "gate.approved"][-1]
    assert e.via == "phone:iPhone" and e.seq == r.event_seq
    assert not _unverified(ws)


def test_approval_of_changed_text_is_stale(ws, put, phone):
    phones.set_permissions(ws.root, {"approve": True})
    tid, t = _gate_ticket(ws, put)
    d = _gate_decision(phone, tid, t)
    path, t = store.load(ws, tid)
    t.set_section("Requirements", "- a, but different")
    store.save(ws, t, path)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "stale"
    assert store.load(ws, tid)[1].status == "backlog"


def test_approval_without_a_hash_or_a_known_gate_is_pending(ws, put, phone):
    phones.set_permissions(ws.root, {"approve": True})
    tid, t = _gate_ticket(ws, put)
    for i, target in enumerate(({"gate": "requirements"}, {"gate": "verify", "hash": "sha256:x"})):
        d = _gate_decision(phone, tid, t, letter="de"[i], target=target)
        assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"
    assert store.load(ws, tid)[1].status == "backlog"


def test_request_changes_applies_and_a_later_approval_of_new_text_is_not_superseded(ws, put, phone):
    phones.set_permissions(ws.root, {"request_changes": True, "approve": True})
    tid, t = _gate_ticket(ws, put)
    r = verify_and_apply(ws, _gate_decision(phone, tid, t, kind="request_changes"), addon="tixlike", now=NOW)
    assert r.status == "applied"
    t = store.load(ws, tid)[1]
    assert t.meta["gates"]["requirements"]["changes_requested"]["message"] == "Split the second criterion"
    path, t = store.load(ws, tid)
    t.set_section("Acceptance criteria", "- [ ] b1\n- [ ] b2")
    store.save(ws, t, path)
    t = store.load(ws, tid)[1]
    assert verify_and_apply(ws, _gate_decision(phone, tid, t, letter="e"), addon="tixlike", now=NOW).status == "applied"
    assert not _unverified(ws)


def test_request_changes_on_old_text_is_stale(ws, put, phone):
    tid, t = _gate_ticket(ws, put)
    d = _gate_decision(phone, tid, t, kind="request_changes")
    path, t = store.load(ws, tid)
    t.set_section("Requirements", "- changed")
    store.save(ws, t, path)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "stale"
    assert "changes_requested" not in (store.load(ws, tid)[1].meta.get("gates") or {}).get("requirements", {})


def test_request_changes_message_cannot_forge_sections(ws, put, phone):
    tid, t = _gate_ticket(ws, put)
    d = _gate_decision(phone, tid, t, kind="request_changes", value="fix\n## Log\n```")
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "applied"
    t = store.load(ws, tid)[1]
    assert t.section("Requirements").strip() == "- a" and "Log" in t.sections
    assert not any(line.startswith(("```", "#")) for line in t.section("Log").splitlines())


def _verdict(ws, phone, tid, value="done", letter="f", round=0, **over):
    from orch.core.epics import verdict_hash
    shown = verdict_hash([store.load(ws, tid)[1]], ws)  # the document's verdict.hash the phone echoes (schema 1.3)
    d = {"v": 1, "decision_id": "dec_" + letter * 32, "space": "s", "ticket": tid, "kind": "verdict",
         "target": {"status": "testing", "round": round, "hash": shown}, "value": value, "note": "", "device": "x",
         "at": "2026-10-02T09:41:07Z", "pair": phone.id}
    d.update(over)
    d["mac"] = mac_of(phone.key, d)
    return d


def test_verdict_needs_its_permission_and_testing(ws, put, phone):
    tid = put("testing", title="v")
    phones.set_permissions(ws.root, {"answer": True})                                # verdicts switched off
    assert verify_and_apply(ws, _verdict(ws, phone, tid), addon="tixlike", now=NOW).status == "pending"
    phones.set_permissions(ws.root, {"verdict": True})
    r = verify_and_apply(ws, _verdict(ws, phone, tid, letter="0"), addon="tixlike", now=NOW)
    assert r.status == "applied", r.message
    assert store.load(ws, tid)[1].status == "done"
    assert verify_and_apply(ws, _verdict(ws, phone, tid, letter="1"), addon="tixlike", now=NOW).status in (
        "stale", "superseded")
    assert not _unverified(ws)


def test_a_follow_up_round_does_not_supersede_the_next_verdict(ws, put, phone, hops, agent):
    phones.set_permissions(ws.root, {"verdict": True})
    tid = put("testing", title="v")
    r = verify_and_apply(ws, _verdict(ws, phone, tid, "follow-up", note="the export is missing"), addon="tixlike", now=NOW)
    assert r.status == "applied" and store.load(ws, tid)[1].status == "in-progress"
    path, t = store.load(ws, tid)
    t.meta["status"] = "testing"
    store.save(ws, t, path)
    append_event(ws, tid, "ticket.moved", agent, {"from": "in-progress", "to": "testing"})  # the next round
    new_round = latest_testing_round(read_events(ws, tid))
    assert new_round > 0
    assert verify_and_apply(ws, _verdict(ws, phone, tid, letter="2", round=new_round), addon="tixlike", now=NOW).status == "applied"


def test_a_verdict_signed_for_an_earlier_round_is_stale(ws, put, phone, agent):
    """Two round-0 verdicts: the ledger key alone (no verdict.given event happened in between) would not have
    noticed a plain move out of and back into testing; the round on the target must."""
    phones.set_permissions(ws.root, {"verdict": True})
    tid = put("testing", title="v")
    signed_early = _verdict(ws, phone, tid, letter="3", round=0)
    append_event(ws, tid, "ticket.moved", agent, {"from": "testing", "to": "in-progress"})
    append_event(ws, tid, "ticket.moved", agent, {"from": "in-progress", "to": "testing"})  # a new round, round 0 still "decided" at the ledger key level
    assert latest_testing_round(read_events(ws, tid)) != 0
    assert verify_and_apply(ws, signed_early, addon="tixlike", now=NOW).status == "stale"
    assert not _unverified(ws)


def test_verdict_off_testing_is_stale(ws, put, phone):
    phones.set_permissions(ws.root, {"verdict": True})
    tid = put("in-progress", title="v")
    assert verify_and_apply(ws, _verdict(ws, phone, tid), addon="tixlike", now=NOW).status == "stale"


def test_verdict_target_needs_a_round(ws, put, phone):
    phones.set_permissions(ws.root, {"verdict": True})
    tid = put("testing", title="v")
    d = _verdict(ws, phone, tid, letter="4")
    d["target"] = {k: v for k, v in d["target"].items() if k != "round"}
    d["mac"] = mac_of(phone.key, d)
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "pending"


def test_stale_and_superseded_are_ledgered_so_a_retry_is_duplicate(ws, asked, phone):
    d = _decision(ws, asked, phone, at="2026-09-01T00:00:00Z")
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "stale"
    path, t = store.load(ws, asked)
    t.meta["questions"][0]["text"] = "changed"
    store.save(ws, t, path)
    d2 = _decision(ws, asked, phone, decision_id="dec_" + "9" * 32, target={"qid": "Q1", "hash": "sha256:old"})
    assert verify_and_apply(ws, d2, addon="tixlike", now=NOW).status == "stale"
    assert verify_and_apply(ws, d2, addon="tixlike", now=NOW).status == "duplicate"


def test_ledger_holds_no_secret(ws, asked, phone):
    d = _decision(ws, asked, phone)
    verify_and_apply(ws, d, addon="tixlike", now=NOW)
    text = ledger.path(ws).read_text(encoding="utf-8")
    assert d["mac"] not in text and phones.b64u(phone.key) not in text
    assert ledger.entries(ws)[0]["status"] == "applied"


def test_check_flags_a_phone_event_without_ledger(ws, asked):
    append_event(ws, asked, "question.answered", Actor("human", "you", "phone:forged"), {"qid": "Q1", "answer": "A"})
    found = _unverified(ws)
    assert found and found[0].level == "error" and found[0].ticket == asked


def test_check_ignores_a_ledger_entry_that_was_not_applied(ws, asked):
    e = append_event(ws, asked, "question.answered", Actor("human", "you", "phone:forged"), {"qid": "Q1"})
    with ledger.lock(ws):
        ledger.append(ws, {"decision_id": "dec_" + "7" * 32, "ticket": asked, "status": "stale", "event_seq": e.seq})
    assert _unverified(ws)


def test_unknown_ticket_is_unlinked(ws, asked, phone):
    d = _decision(ws, asked, phone, ticket="L-0999")
    assert verify_and_apply(ws, d, addon="tixlike", now=NOW).status == "unlinked"


def test_provider_context_requires_the_manifest_flag(ws):
    from addon_fixtures import loaded
    from orch.addons.runner import AddonRunError
    la = loaded(ws, object(), name="plain")
    with pytest.raises(AddonRunError):
        la.ctx.provider_context().remote_decision({})


def test_provider_context_hands_the_decision_to_core(ws, asked, phone):
    from addon_fixtures import loaded
    la = loaded(ws, object(), name="tixlike", capabilities=["decisions"], remote_humans=True, menu=None,
                settings_schema=[])
    r = la.ctx.provider_context().remote_decision(_decision(ws, asked, phone))
    assert r.status == "applied"
    assert store.load(ws, asked)[1].meta["questions"][0]["via"] == "phone:iPhone"


def test_provider_context_refuses_a_remote_decision_while_a_page_renders(ws, asked, phone):
    from addon_fixtures import loaded
    from orch.addons.runner import AddonRunError, rendering
    la = loaded(ws, object(), name="tixlike", capabilities=["decisions"], remote_humans=True, menu=None,
                settings_schema=[])
    with rendering():
        with pytest.raises(AddonRunError, match="while a page renders"):
            la.ctx.provider_context().remote_decision(_decision(ws, asked, phone))
    assert store.load(ws, asked)[1].meta["questions"][0].get("via") != "phone:iPhone"



# -- fix round 1: a phone verdict echoes the document's verdict hash ------------------------------------------------

def test_a_verdict_without_the_hash_is_refused_until_the_phone_app_sends_it(ws, put, phone):
    phones.set_permissions(ws.root, {"verdict": True})
    tid = put("testing", title="v", sections={"Verification": "- AC1: ok"})
    d = _verdict(ws, phone, tid, letter="5")
    d["target"] = {k: v for k, v in d["target"].items() if k != "hash"}
    d["mac"] = mac_of(phone.key, d)
    r = verify_and_apply(ws, d, addon="tixlike", now=NOW)
    assert r.status == "pending" and "update the phone app" in r.message
    assert store.load(ws, tid)[1].status == "testing"


def test_a_verdict_on_changed_evidence_is_stale_and_the_hash_is_ledgered(ws, put, phone, aops):
    from orch.remote import ledger as remote_ledger
    phones.set_permissions(ws.root, {"verdict": True})
    tid = put("testing", title="v", sections={"Verification": "- AC1: ok"})
    d = _verdict(ws, phone, tid, letter="6")
    path, t = store.load(ws, tid)
    t.set_section("Verification", "- AC1: something else")
    store.save(ws, t, path)
    r = verify_and_apply(ws, d, addon="tixlike", now=NOW)
    assert r.status == "stale" and "changed" in r.message
    assert store.load(ws, tid)[1].status == "testing"
    entry = [json.loads(x) for x in remote_ledger.path(ws).read_text(encoding="utf-8").splitlines()][-1]
    assert entry["hash"] == d["target"]["hash"]


def test_a_phone_done_verdict_on_a_changed_approval_is_refused(ws, put, phone):
    from orch.core.gates import gate_hash
    phones.set_permissions(ws.root, {"verdict": True})
    tid = put("testing", title="v", sections={"Requirements": "r", "Acceptance criteria": "- [ ] a",
                                              "Verification": "- AC1: ok"})
    path, t = store.load(ws, tid)
    t.meta["gates"]["requirements"] = {"approved": "2026-10-01T09:00Z", "via": "tty",
                                       "hash": gate_hash(t, "requirements"), "hash_v": 2}
    t.set_section("Requirements", "r, and more")
    store.save(ws, t, path)
    r = verify_and_apply(ws, _verdict(ws, phone, tid, letter="7"), addon="tixlike", now=NOW)
    assert r.status == "stale" and "changed since approval" in r.message
    assert store.load(ws, tid)[1].status == "testing"
    r = verify_and_apply(ws, _verdict(ws, phone, tid, "follow-up", letter="8", note="redo"), addon="tixlike", now=NOW)
    assert r.status == "applied" and store.load(ws, tid)[1].status == "in-progress"

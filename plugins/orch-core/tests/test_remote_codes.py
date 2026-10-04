"""Every RemoteResult carries a stable machine-readable `code` (the addon API 2.3 enum), beside its human message."""
import pytest

from orch.core import store
from orch.core.events import append_event
from orch.remote import store as phones
from orch.remote.verify import CODES, verify_and_apply
from test_remote_auto_apply import _together, drafted  # noqa: F401
from test_remote_verify import NOW, _decision, _gate_decision, _signed, _verdict, asked, phone  # noqa: F401


def _run(ws, d):
    r = verify_and_apply(ws, d, addon="tixlike", now=NOW)
    assert r.code in CODES, r
    return r


def test_each_not_applied_path_has_its_code(ws, asked, phone, put):  # noqa: F811
    d = _decision(ws, asked, phone)
    other = lambda letter, **over: _decision(ws, asked, phone, decision_id="dec_" + letter * 32, **over)  # noqa: E731
    cases = {
        "malformed": {**d, "v": 2},
        "kind-not-allowed": other("1", kind="move"),
        "not-paired": _signed(phone, {**d, "pair": "ph_000000000000"}),
        "bad-signature": {**d, "value": "B"},
        "implausible-time": other("2", at="2026-10-02T09:50:00Z"),
        "too-old": other("3", at="2026-09-10T09:00:00Z"),
        "no-such-ticket": other("4", ticket="L-0999"),
        "question-not-found": other("5", target={"qid": "Q9", "hash": "sha256:x"}),
        "changed-since": other("6", target={"qid": "Q1", "hash": "sha256:old"}),
    }
    for code, decision in cases.items():
        r = _run(ws, decision)
        assert r.code == code and r.message, (code, r)
    phones.set_permissions(ws.root, {"answer": False})
    assert _run(ws, d).code == "kind-switched-off"


def test_applied_duplicate_and_superseded_codes(ws, asked, phone):  # noqa: F811
    d = _decision(ws, asked, phone)
    r = _run(ws, d)
    assert (r.status, r.code) == ("applied", "applied")
    assert _run(ws, d).code == "already-handled"
    assert _run(ws, _decision(ws, asked, phone, decision_id="dec_" + "b" * 32)).code == "superseded"


def test_answered_locally_code(ws, asked, phone, hops):  # noqa: F811
    hops.answer(asked, "Q1", "B")
    assert _run(ws, _decision(ws, asked, phone)).code == "answered-locally"


def test_gate_and_verdict_codes(ws, put, phone, agent, hops):  # noqa: F811
    phones.set_permissions(ws.root, {"approve": True, "verdict": True})
    tid = put("backlog", title="g", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    t = store.load(ws, tid)[1]
    d = _gate_decision(phone, tid, t)
    hops.approve(tid, "requirements")  # approved on the desktop first
    assert _run(ws, d).code == "already-approved"
    assert _run(ws, _verdict(ws, phone, put("in-progress", title="v"))).code == "wrong-status"
    vt = put("testing", title="v2")
    early = _verdict(ws, phone, vt, letter="e", round=0)
    append_event(ws, vt, "ticket.moved", agent, {"from": "testing", "to": "in-progress"})
    append_event(ws, vt, "ticket.moved", agent, {"from": "in-progress", "to": "testing"})
    assert _run(ws, early).code == "wrong-round"


def test_ticket_request_codes(ws, phone):  # noqa: F811
    phones.set_permissions(ws.root, {"ticket_request": True})
    d = {"v": 1, "decision_id": "dec_" + "7" * 32, "kind": "ticket_request", "value": {"title": ""},
         "at": "2026-10-02T09:41:07Z", "pair": phone.id}
    assert _run(ws, _signed(phone, d)).code == "malformed"
    d = _signed(phone, {**d, "value": {"title": "From the phone"}})
    r = _run(ws, d)
    assert (r.status, r.code) == ("applied", "applied")
    assert _run(ws, d).code == "already-handled"


def test_refused_final_code_when_core_refuses_the_intent(ws, put, phone, monkeypatch):  # noqa: F811
    from orch.addons import intents
    from orch.errors import ValidationError
    phones.set_permissions(ws.root, {"approve": True})
    tid = put("backlog", title="g", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    t = store.load(ws, tid)[1]

    def refuse(*a, **k):
        raise ValidationError("core said no")
    monkeypatch.setattr(intents, "execute", refuse)
    r = _run(ws, _gate_decision(phone, tid, t))
    assert (r.status, r.code, r.message) == ("stale", "refused-final", "core said no")


@pytest.mark.parametrize("code", CODES)
def test_codes_are_kebab_case_words(code):
    assert code == code.lower() and code.replace("-", "").isalpha()


def test_refused_retry_is_pending_so_it_can_apply_later(ws, put, phone, monkeypatch):  # noqa: F811
    from orch.addons import intents
    from orch.errors import OrchError
    phones.set_permissions(ws.root, {"approve": True})
    tid = put("backlog", title="g", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    t = store.load(ws, tid)[1]

    def refuse(*a, **k):
        raise OrchError("try again")
    monkeypatch.setattr(intents, "execute", refuse)
    r = _run(ws, _gate_decision(phone, tid, t))
    assert (r.status, r.code) == ("pending", "refused-retry")


def test_together_codes(ws, phone, drafted, aops, monkeypatch):  # noqa: F811
    from orch.core.ops import Ops
    from orch.errors import OrchError, ValidationError
    phones.set_permissions(ws.root, {"approve": True})
    stale = _together(phone, ws, drafted)
    aops.set_section(drafted, "Plan", "1. something else")
    r = _run(ws, stale)
    assert (r.status, r.code) == ("stale", "changed-since") and "plan" in r.message

    def refuse(exc):
        def raiser(*a, **k):
            raise exc
        return raiser
    monkeypatch.setattr(Ops, "approve_together", refuse(ValidationError("core said no")))
    r = _run(ws, _together(phone, ws, drafted, letter="8"))
    assert (r.status, r.code) == ("stale", "refused-final")
    monkeypatch.setattr(Ops, "approve_together", refuse(OrchError("try again")))
    r = _run(ws, _together(phone, ws, drafted, letter="9"))
    assert (r.status, r.code) == ("pending", "refused-retry")


def _request(phone, **value):  # noqa: F811
    d = {"v": 1, "decision_id": "dec_" + "8" * 32, "kind": "ticket_request", "value": {"title": "T", **value},
         "at": "2026-10-02T09:41:07Z", "pair": phone.id}
    return _signed(phone, d)


def test_ticket_request_core_refusal_is_final_and_ledgered(ws, phone, monkeypatch):  # noqa: F811
    from orch.core.ops import Ops
    from orch.errors import ValidationError
    from orch.remote import ledger

    def refuse(*a, **k):
        raise ValidationError("core said no")
    monkeypatch.setattr(Ops, "new", refuse)
    phones.set_permissions(ws.root, {"ticket_request": True})
    d = _request(phone)
    r = _run(ws, d)
    assert (r.status, r.code, r.message) == ("stale", "refused-final", "core said no")
    assert ledger.seen(ws, d["decision_id"])
    assert _run(ws, d).code == "already-handled"


def test_ticket_request_lock_busy_is_retry(ws, phone, monkeypatch):  # noqa: F811
    from orch.core.ops import Ops
    from orch.errors import LockBusyError
    from orch.remote import ledger
    phones.set_permissions(ws.root, {"ticket_request": True})

    def busy(*a, **k):
        raise LockBusyError("busy")
    monkeypatch.setattr(Ops, "new", busy)
    d = _request(phone)
    r = _run(ws, d)
    assert (r.status, r.code) == ("pending", "refused-retry")
    assert not ledger.seen(ws, d["decision_id"])

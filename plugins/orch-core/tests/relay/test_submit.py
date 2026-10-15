"""R2: ``relay/submit.py``: the closed event/scope table, the lock-free checks, the relay_fields roll-forward hook."""

from __future__ import annotations

import pytest

from orch import canon, crypto
from orch.relay import framing, submit
from orch.relay.framing import Identity, Refusal

from .bridgekit import T0, Phone, ulid
from .helpers import WS

P = Phone()
IDENT = Identity(WS, "d_" + P.device, "p_" + P.person, "sha256:" + "1" * 64, 2, "sha256:" + "4" * 64)


def _args(**over):
    log = ulid()
    fields = {"key": "DEMO-0001", "ticket_type": "bug", "title": "t", "owner": "p_" + P.person}
    ev = P.event(log, "ticket.created", eid=over.pop("eid", None) or ulid(), **{**fields, **over})
    return {"log": log, "events": [ev]}


def check(args, *, op="new_ticket", idem=None, ident=IDENT, pub=None):
    return submit.check_events(op, args, ident, pub or P.dk_pub, idem=idem or args["events"][0]["id"])


def code(fn):
    with pytest.raises(Refusal) as e:
        fn()
    return e.value.code


def test_a_good_event_passes_and_comes_back_unchanged():
    a = _args()
    log, events = check(a)
    assert log == a["log"] and events == a["events"]


def test_the_table_and_ops_agree():
    """I1: both types need ``decide`` (11A.14 route scope, L2, the 14.8 phone profile ``["look","decide"]``)."""
    assert submit.EVENTS == {"new_ticket": ("ticket.created", "decide"), "answer": ("question.answered", "decide")}
    for op, (typ, scope) in submit.EVENTS.items():
        assert framing.OPS[op].event == typ and framing.OPS[op].scope == scope


def test_an_event_type_outside_the_table_is_forbidden_scope():
    a = _args()
    assert code(lambda: check(a, op="answer")) == "forbidden_scope"
    assert code(lambda: check(a, op="member_remove")) == "forbidden_scope"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a["events"].append(dict(a["events"][0])),
        lambda a: a["events"].clear(),
        lambda a: a.update(log="workspace"),
        lambda a: a.update(extra=1),
    ],
)
def test_shape_is_p1_invalid_input(mutate):
    a = _args()
    mutate(a)
    idem = a["events"][0]["id"] if a.get("events") and isinstance(a["events"][0].get("id"), str) else ulid()
    assert code(lambda: check(a, idem=idem)) == "invalid.input"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(seq=1),
        lambda e: e.update(at="2026-01-01T00:00:00Z"),
        lambda e: e.update(prev=None),
        lambda e: e.update(ws_seq=1),
        lambda e: e.update(host_sig="x"),
        lambda e: e.update(source_sha=[]),
        lambda e: e.update(id="nope"),
        lambda e: e.update(v=1),
        lambda e: e.update(hash_v=2),
        lambda e: e.update(based_on="sha256:" + "0" * 64),
        lambda e: e.update(ticket_type="no-such-type"),
        lambda e: e.update(key="DEMO-1"),
        lambda e: e.update(extra_field=1),
        lambda e: e.update(owner="p_" + "9" * 32),
        lambda e: e.pop("title"),
        lambda e: e.pop("auth"),
        lambda e: e.update(sig="not base64url!"),
    ],
)
def test_p3_the_submitted_subset_schema_is_validation_event(mutate):
    """M2 / I3: a host field, a non-ULID id, ``v``/``hash_v``, a creation's ``based_on``, the F1 schema (unknown
    ticket type, key format, extra or missing fields, the owner rule): ``validation.event``, before the signature."""
    a = _args()
    mutate(a["events"][0])
    ev = a["events"][0]
    assert code(lambda: check(a, idem=ev["id"] if type(ev.get("id")) is str else ulid())) == "validation.event"


@pytest.mark.parametrize("text", ["e\u0301", "a\u202eb", "a\rb", "a\x7fb", "\U000323b0"])
def test_p3_the_text_rules_are_validation_text(text):
    """F1 §11.3 (NFD, bidi controls, CR, DEL, a code point unassigned in Unicode 16.0): ``validation.text``, checked
    before the signature (such an event cannot even be signed: its signing bytes refuse the text)."""
    a = _args()
    a["events"][0]["title"] = text
    assert code(lambda: check(a)) == "validation.text"


def test_idem_must_be_the_event_id_p1():
    """M2: 14.6 P1 says ``invalid.input`` for idem != id."""
    assert code(lambda: check(_args(), idem=ulid())) == "invalid.input"


def test_the_lock_free_checks_never_judge_freshness():
    """Ruling A: the window is the host's, after dedup; the lock-free checks pass an old or future event id."""
    assert check(_args(eid=ulid(T0 - framing.FRESH_PAST_MS - 10**9)))
    assert check(_args(eid=ulid(T0 + framing.FRESH_FUTURE_MS + 10**9)))


def _signed_by(phone, log, ev):
    ev = {k: v for k, v in ev.items() if k != "sig"}
    ev["sig"] = crypto.b64u(crypto.sign(phone.dk, canon.person_signing_bytes(WS, log, ev)))
    return ev


def test_the_actor_must_be_exactly_the_requesting_device_and_person():
    """An event naming P as actor, validly signed by another member's device that sends it: op.not_allowed."""
    other = Phone()
    a = _args()
    a["events"][0] = _signed_by(other, a["log"], a["events"][0])
    ident = Identity(WS, "d_" + other.device, "p_" + other.person, IDENT.cert_digest, 2, IDENT.list_hash)
    assert code(lambda: check(a, ident=ident, pub=other.dk_pub)) == "op.not_allowed"
    mine = dict(a["events"][0], actor={"kind": "person", "id": ident.person_id, "device": ident.device_id},
                owner=ident.person_id)  # fmt: skip
    a["events"][0] = _signed_by(other, a["log"], mine)
    assert check(a, ident=ident, pub=other.dk_pub)  # the control: its own actor passes


@pytest.mark.parametrize("auth", ["convenience", "secure-enclave", "webauthn"])
def test_only_the_column_r_auth_kinds(auth):
    a = _args()
    ev = dict(a["events"][0])
    if auth is None:
        del ev["auth"]
    else:
        ev["auth"] = auth
    a["events"][0] = _signed_by(P, a["log"], ev)  # validly signed: only the auth rule refuses it
    assert code(lambda: check(a)) == "op.not_allowed"


@pytest.mark.parametrize("auth", sorted(submit.AUTH))
def test_the_allowed_auth_kinds_pass_when_signed(auth):
    log = ulid()
    ev = P.event(log, "ticket.created", key="DEMO-0001", ticket_type="bug", title="t", owner="p_" + P.person, auth=auth)
    assert check({"log": log, "events": [ev]})


def test_the_signature_must_verify_under_this_device_over_this_log():
    """M2: 14.6 P7 says ``sig.invalid``."""
    a = _args()
    a["events"][0]["title"] = "changed after signing"
    assert code(lambda: check(a)) == "sig.invalid"
    b = _args()
    b["log"] = ulid()  # signed for another log
    assert code(lambda: check(b)) == "sig.invalid"
    c = _args()
    assert code(lambda: check(c, pub=Phone().dk_pub)) == "sig.invalid"
    d = _args()
    d["events"][0]["sig"] = crypto.b64u(b"\x00" * 64)
    assert code(lambda: check(d)) == "sig.invalid"


def test_a_missing_signature_is_p4_op_not_allowed():
    a = _args()
    del a["events"][0]["sig"]
    assert code(lambda: check(a)) in ("op.not_allowed", "validation.event")


@pytest.mark.parametrize("code_", ["validation.event", "validation.text", "sig.invalid", "invalid.input"])
def test_the_lock_free_codes_are_definite(code_):
    assert framing.retry_class(code_) == "D" and code_ in framing.RETRY


# -- relay_fields ------------------------------------------------------------------------------------------------------


def _record(events, log="L"):
    actor = events[0]["actor"]
    return {"principal": "r:" + actor["device"], "args_normalised": {"log": log}, "signed_events": events}


def test_relay_fields_rolls_forward_through_append_and_commits():
    ev = _args()["events"][0]
    calls = []

    def append(log, events, extras, person, device):
        calls.append((log, person, device, extras))
        return [7], None

    fields = submit.make_relay_fields(append)(_record([ev]))
    assert fields["step"] == "committed" and fields["result"]["data"] == {"log": "L", "seqs": [7]}
    assert calls == [("L", ev["actor"]["id"], ev["actor"]["device"], [{}])]


def test_relay_fields_fails_with_the_landed_seqs_on_an_error():
    class Err:
        exit = 5

        def error(self):
            return {"code": "event.bad_base", "message": "m", "retryable": False}

    ev = _args()["events"][0]
    fields = submit.make_relay_fields(lambda *a: ([3], Err()))(_record([ev, ev]))
    assert fields == {"step": "failed", "error": Err().error(), "exit": 5, "seqs": [3]}


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"principal": "r:d_x", "args_normalised": {"log": "L"}, "signed_events": []},
        {"principal": "r:d_x", "args_normalised": {"log": "L", "events": [{"actor": {"device": "d_x"}}]}},
        {"principal": "r:d_other", "args_normalised": {"log": "L"}, "signed_events": [{"actor": {"device": "d_x"}}]},
    ],
)
def test_relay_fields_keeps_what_it_cannot_say(record):
    assert submit.make_relay_fields(lambda *a: pytest.fail("never appended"))(record) == "keep"


def _answer(**over):
    log = ulid()
    fields = {"question": "q1", "hash": "sha256:" + "0" * 64, "option": "y", **over}
    return {"log": log, "events": [P.event(log, "question.answered", **fields)]}


@pytest.mark.parametrize("over", [{"bogus": 1}, {"option": 5}, {"hash": "md5:0"}])
def test_a_malformed_answer_is_validation_event_at_p3(over):
    """Review V21: P3 runs for ``question.answered`` too (not only for ``ticket.created``)."""
    assert code(lambda: check(_answer(**over), op="answer")) == "validation.event"

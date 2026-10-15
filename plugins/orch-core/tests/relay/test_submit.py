"""R2: ``relay/submit.py``: the closed event/scope table, the lock-free checks, the relay_fields roll-forward hook."""

from __future__ import annotations

import pytest

from orch import crypto
from orch.relay import framing, submit
from orch.relay.framing import Identity, Refusal

from .bridgekit import T0, Phone, ulid
from .helpers import WS

P = Phone()
IDENT = Identity(WS, "d_" + P.device, "p_" + P.person, "sha256:" + "1" * 64, 2, "sha256:" + "4" * 64)


def _args(**over):
    log = ulid()
    fields = {"key": "DEMO-1", "ticket_type": "task", "title": "t", "owner": "p_" + P.person}
    ev = P.event(log, "ticket.created", eid=over.pop("eid", None) or ulid(), **{**fields, **over})
    return {"log": log, "events": [ev]}


def check(args, *, op="new_ticket", idem=None, now=T0, ident=IDENT, pub=None):
    return submit.check_events(op, args, ident, pub or P.dk_pub, idem=idem or args["events"][0]["id"], now_ms=now)


def code(fn):
    with pytest.raises(Refusal) as e:
        fn()
    return e.value.code


def test_a_good_event_passes_and_comes_back_unchanged():
    a = _args()
    log, events = check(a)
    assert log == a["log"] and events == a["events"]


def test_the_table_and_ops_agree():
    assert submit.EVENTS == {"new_ticket": ("ticket.created", "operate"), "answer": ("question.answered", "decide")}
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
        lambda a: a["events"][0].update(seq=1),
        lambda a: a["events"][0].update(host_sig="x"),
        lambda a: a["events"][0].update(source_sha=[]),
        lambda a: a["events"][0].update(id="nope"),
    ],
)
def test_shape_is_a_validation_code(mutate):
    a = _args()
    mutate(a)
    idem = a["events"][0]["id"] if a.get("events") and isinstance(a["events"][0].get("id"), str) else ulid()
    assert code(lambda: check(a, idem=idem)) == "invalid.input"


def test_idem_must_be_the_event_id():
    assert code(lambda: check(_args(), idem=ulid())) == "idem.invalid"


def test_the_event_id_must_be_fresh():
    old = _args(eid=ulid(T0 - framing.FRESH_PAST_MS - 1))
    assert code(lambda: check(old)) == "event.expired"
    future = _args(eid=ulid(T0 + framing.FRESH_FUTURE_MS + 1))
    assert code(lambda: check(future)) == "event.expired"
    assert check(_args(eid=ulid(T0 - framing.FRESH_PAST_MS)))  # the edges pass
    assert check(_args(eid=ulid(T0 + framing.FRESH_FUTURE_MS)))


def test_the_actor_must_be_exactly_the_requesting_device_and_person():
    other = Phone()
    a = _args()
    ident = Identity(WS, "d_" + other.device, "p_" + other.person, IDENT.cert_digest, 2, IDENT.list_hash)
    assert code(lambda: check(a, ident=ident, pub=other.dk_pub)) == "op.not_allowed"


@pytest.mark.parametrize("auth", ["convenience", "secure-enclave", "webauthn", None])
def test_only_the_column_r_auth_kinds(auth):
    a = _args()
    ev = a["events"][0]
    if auth is None:
        del ev["auth"]
    else:
        ev["auth"] = auth
    assert code(lambda: check(a)) == "op.not_allowed"


@pytest.mark.parametrize("auth", sorted(submit.AUTH))
def test_the_allowed_auth_kinds_pass_when_signed(auth):
    log = ulid()
    ev = P.event(log, "ticket.created", key="DEMO-1", ticket_type="task", title="t", owner="p_" + P.person, auth=auth)
    assert check({"log": log, "events": [ev]})


def test_the_signature_must_verify_under_this_device_over_this_log():
    a = _args()
    a["events"][0]["title"] = "changed after signing"
    assert code(lambda: check(a)) == "op.not_allowed"
    b = _args()
    b["log"] = ulid()  # signed for another log
    assert code(lambda: check(b)) == "op.not_allowed"
    c = _args()
    assert code(lambda: check(c, pub=Phone().dk_pub)) == "op.not_allowed"
    d = _args()
    d["events"][0]["sig"] = crypto.b64u(b"\x00" * 64)
    assert code(lambda: check(d)) == "op.not_allowed"


# -- relay_fields ------------------------------------------------------------------------------------------------------


def _record(events, log="L"):
    actor = events[0]["actor"]
    return {"principal": "r:" + actor["device"], "args_normalised": {"log": log, "events": events}}


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
        {"principal": "r:d_x", "args_normalised": {"log": "L", "events": []}},
        {"principal": "r:d_other", "args_normalised": {"log": "L", "events": [{"actor": {"device": "d_x"}}]}},
    ],
)
def test_relay_fields_keeps_what_it_cannot_say(record):
    assert submit.make_relay_fields(lambda *a: pytest.fail("never appended"))(record) == "keep"

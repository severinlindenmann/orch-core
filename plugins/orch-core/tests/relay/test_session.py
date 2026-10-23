"""R1 session and directory calls against the recorded HTTP fixtures (``session_*``, ``host_hold``, ``card_*``,
``members``, ``checkpoint_get``): requests matched as recorded, responses replayed, every applicable refusal mapped."""

from __future__ import annotations

import os

import pytest

from orch import canon, crypto
from orch.relay import retry, session
from orch.relay.session import Fenced, RelayMisbehaving
from orch.wsproto import wire

from . import corpus
from .helpers import (
    GENESIS,
    GENESIS_CERT,
    GENESIS_PIN,
    V_ORIGIN,
    WS,
    WSK_PUB,
    Alarms,
    Clock,
    Holder,
    directory,
)
from .helpers import session as make_session
from .mock import Expect, MockRelay, fixture_expect, json_reply

CHALLENGE = corpus.case("session_challenge", "success")["response"]["json"]["challenge"]


@pytest.fixture
def relay():
    m = MockRelay()
    yield m
    assert m.errors == []
    m.close()


def sess(relay, **kw):
    """A session whose link origin is the fixtures' (signed and compared) and whose transport reaches the mock."""
    return make_session(relay.origin(), V_ORIGIN, **kw)


def login_check(body: bytes) -> bool:
    o = canon.loads_strict(body)
    if set(o) != {"challenge", "kind", "id", "sig"} or o["kind"] != "workspace" or o["id"] != WS:
        return False
    ch = crypto.unb64u(o["challenge"], 32)
    return crypto.verify(WSK_PUB, crypto.unb64u(o["sig"], 64), wire.auth_bytes(V_ORIGIN, ch, "workspace", WS))


def login_expects(token="FAKE-workspace", *, fresh=False):
    """A challenge (the fixture's, or a fresh one: a host never signs one challenge twice) and a checked login."""
    ch = (
        Expect(
            "POST",
            "/v2/session/challenge",
            reply=json_reply(200, {"challenge": crypto.b64u(os.urandom(32)), "expires_ms": 1_790_000_060_000}),
        )
        if fresh
        else fixture_expect("session_challenge", "success")
    )
    return [
        ch,
        Expect(
            "POST",
            "/v2/session/login",
            {},
            {"Content-Type": "application/json"},
            login_check,
            json_reply(200, {"token": token, "expires_ms": 1_790_000_900_000}),
        ),
    ]


# --- challenge and login --------------------------------------------------------------------------------------------


def test_challenge_fixture(relay):
    relay.expect(fixture_expect("session_challenge", "success"))
    assert sess(relay).challenge() == crypto.unb64u(CHALLENGE)


def test_challenge_rate_limited_fixture(relay):
    relay.expect(fixture_expect("session_challenge", "refusal_429_rate_limited", body=None))
    with pytest.raises(retry.RelayRefused) as e:
        sess(relay).challenge()
    assert (e.value.status, e.value.code, e.value.retry_after) == (429, "rate_limited", 20)
    assert retry.classify(e.value, membership=False) == retry.TRANSIENT


def test_login_signs_the_fixture_challenge_for_the_stored_origin(relay):
    relay.expect(*login_expects())
    s = sess(relay)
    s.login()
    assert s.logged_in


LOGIN_REFUSALS = [c["name"] for c in corpus.fixture("session_login")["cases"] if c["name"] != "success"]


@pytest.mark.parametrize("name", LOGIN_REFUSALS)
def test_login_refusal_fixtures(relay, name):
    c = corpus.case("session_login", name)
    resp = c["response"]
    relay.expect(fixture_expect("session_challenge", "success"), fixture_expect("session_login", name, body=None))
    with pytest.raises(retry.RelayRefused) as e:
        sess(relay).login()
    assert (e.value.status, e.value.code) == (resp["status"], resp["json"]["error"])
    assert e.value.retry_after == (int(resp["headers"]["Retry-After"]) if "Retry-After" in resp["headers"] else None)
    want = {"refusal_429_quota_exceeded": retry.QUOTA}.get(name, retry.PERMANENT)
    assert retry.classify(e.value, membership=False) == want


def test_login_too_large_is_refused_locally_never_sent(relay):
    s = sess(relay)
    with pytest.raises(wire.WireError) as e:
        s.call("POST", "/v2/session/login", "R", body={"x": "a" * 16_385}, auth=False)
    assert e.value.code == "too_large" and relay.seen == []


def test_logout_fixtures(relay):
    relay.expect(*login_expects(token="FAKE-device"), fixture_expect("session_logout", "success"))
    s = sess(relay)
    s.login()
    s.logout()
    assert not s.logged_in
    s.logout()  # nothing to do
    relay.expect(
        *login_expects(token="FAKE-device", fresh=True), fixture_expect("session_logout", "expired_token_retry")
    )
    s.login()
    s.logout()


# --- holder ---------------------------------------------------------------------------------------------------------


def test_hold_success_saves_the_generation(relay):
    relay.expect(*login_expects(), fixture_expect("host_hold", "success"))
    h = Holder()
    got = sess(relay, holder=h).hold()
    assert (got.gen, got.lease_s, got.cursor) == (1, 40, 0)
    assert h.saved == [1]


def test_hold_renew_fixture(relay):
    relay.expect(*login_expects(), fixture_expect("host_hold", "renew"))
    h = Holder(gen=1)
    s = sess(relay, holder=h)
    assert s.hold().gen == 1 and s.gen == 1 and h.saved == []


def test_explicit_takeover_fixture(relay):
    relay.expect(*login_expects(), fixture_expect("host_hold", "explicit_takeover"))
    h = Holder(gen=1)
    assert sess(relay, holder=h).operator_takeover().gen == 2
    assert h.saved == [2]


def test_running_renewal_fenced_alarms_and_stops_writes(relay):
    """While running, a renewal answered ``fenced`` stops the worker (the restart rule applies only at start)."""
    relay.expect(
        *login_expects(),
        fixture_expect("host_hold", "success"),
        fixture_expect("host_hold", "refusal_409_fenced", body=None),
    )
    alarms = Alarms()
    h = Holder()
    s = sess(relay, holder=h, alarms=alarms)
    assert s.hold().gen == 1
    with pytest.raises(Fenced):
        s.hold()
    assert alarms.codes() == ["fenced"] and s.fenced and h.saved == [1]
    n = len(relay.seen)
    with pytest.raises(Fenced):
        s.call("PUT", f"/v2/workspaces/{WS}/card", "R", body={}, holder=True)
    with pytest.raises(Fenced):
        s.hold()
    assert len(relay.seen) == n  # refused locally


def test_holder_conflict_waits_out_the_lease(relay):
    relay.expect(
        *login_expects(),
        fixture_expect("host_hold", "refusal_409_holder_conflict"),
        fixture_expect("host_hold", "success"),
    )
    clock = Clock()
    assert sess(relay, clock=clock).hold().gen == 1
    assert clock.slept == [40.0]


@pytest.mark.parametrize("name", ["refusal_403_not_eligible", "refusal_400_malformed"])
def test_hold_other_refusals(relay, name):
    relay.expect(*login_expects(), fixture_expect("host_hold", name, body=None))
    relay.script[-1].query = None
    with pytest.raises(retry.RelayRefused) as e:
        sess(relay).hold()
    assert retry.classify(e.value, membership=False) == retry.PERMANENT


def test_unauthenticated_relogs_in_once_and_resends_identical_bytes(relay):
    first = fixture_expect("host_hold", "refusal_401_unauthenticated", body=None)
    first.headers = None
    relay.expect(*login_expects(), first, *login_expects(fresh=True), fixture_expect("host_hold", "success"))
    s = sess(relay)
    assert s.hold().gen == 1
    holds = [x for x in relay.seen if x.path.endswith("/host/hold")]
    assert len(holds) == 2 and holds[0].body == holds[1].body


def test_unauthenticated_twice_is_raised(relay):
    a = fixture_expect("host_hold", "refusal_401_unauthenticated", body=None)
    b = fixture_expect("host_hold", "refusal_401_unauthenticated", body=None)
    a.headers = b.headers = None
    relay.expect(*login_expects(), a, *login_expects(fresh=True), b)
    with pytest.raises(retry.RelayRefused) as e:
        sess(relay).hold()
    assert e.value.code == "unauthenticated"


def test_hold_answer_for_another_generation_is_misbehaving(relay):
    relay.expect(
        *login_expects(),
        Expect("POST", f"/v2/workspaces/{WS}/host/hold", reply=json_reply(200, {"gen": 7, "lease_s": 40, "cursor": 0})),
    )
    with pytest.raises(RelayMisbehaving):
        sess(relay, holder=Holder(gen=1)).hold()


# --- card -----------------------------------------------------------------------------------------------------------

CARD = corpus.S2["card"]["card"]


def holder_session(relay, **kw):
    relay.expect(*login_expects(), fixture_expect("host_hold", "success"))
    s = sess(relay, **kw)
    s.hold()
    return s


def test_first_card_without_session_fixture(relay):
    relay.expect(fixture_expect("card_put", "first_card_without_session"))
    assert directory(sess(relay)).put_card(CARD, first=True) == 1


def test_card_put_under_w_g_fixture(relay):
    s = holder_session(relay)
    relay.expect(fixture_expect("card_put", "success"))
    assert directory(s).put_card(CARD, first=False) == 1


CARD_REFUSALS = [
    c["name"]
    for c in corpus.fixture("card_put")["cases"]
    if c["name"].startswith("refusal_")
    and c["name"] not in ("refusal_413_too_large", "refusal_400_malformed", "refusal_401_unauthenticated")
]


@pytest.mark.parametrize("name", CARD_REFUSALS)
def test_card_put_refusal_fixtures(relay, name):
    s = holder_session(relay, alarms=(alarms := Alarms()))
    c = corpus.case("card_put", name)
    e_ = fixture_expect("card_put", name, body=None)
    e_.headers = None
    relay.expect(e_)
    code = c["response"]["json"]["error"]
    if code == "fenced":
        with pytest.raises(Fenced):
            directory(s).put_card(CARD, first=False)
        assert alarms.codes() == ["fenced"]
        return
    with pytest.raises(retry.RelayRefused) as e:
        directory(s).put_card(CARD, first=False)
    assert (e.value.status, e.value.code) == (c["response"]["status"], code)


def test_first_card_on_a_held_card_returns_none(relay):
    e_ = fixture_expect("card_put", "refusal_401_unauthenticated", body=None)
    e_.headers = None
    relay.expect(e_)
    assert directory(sess(relay)).put_card(CARD, first=True) is None


def test_invalid_own_card_is_never_sent(relay):
    bad = {**CARD, "o": {**CARD["o"], "card_seq": 2}}  # signature no longer covers it
    with pytest.raises(ValueError):
        directory(sess(relay)).put_card(bad, first=True)
    other_origin = make_session(relay.origin(), "https://relay.example")
    with pytest.raises(ValueError):
        directory(other_origin).put_card(CARD, first=True)  # relay_url is not the stored origin
    assert relay.seen == []


def test_card_get_fixture_verifies(relay):
    relay.expect(*login_expects(), fixture_expect("card_get", "success"))
    relay.script[-1].headers = None
    assert directory(sess(relay)).get_card() == CARD


@pytest.mark.parametrize(
    "tamper",
    [
        lambda c: {**c, "o": {**c["o"], "card_seq": 9}},
        lambda c: {**c, "sealed": crypto.b64u(b"x" * 40)},
        lambda c: corpus.S2["card"]["cases"][13]["card"],  # card_signed_by_another_wsk
        lambda c: {**c, "x": 1},
    ],
)
def test_card_get_tampered_is_misbehaving(relay, tamper):
    relay.expect(
        *login_expects(), Expect("GET", f"/v2/workspaces/{WS}/card", reply=json_reply(200, {"card": tamper(CARD)}))
    )
    with pytest.raises(RelayMisbehaving):
        directory(sess(relay)).get_card()


def test_card_get_404_is_none(relay):
    relay.expect(*login_expects(), fixture_expect("card_get", "refusal_404_not_found"))
    relay.script[-1].headers = None
    assert directory(sess(relay)).get_card() is None


@pytest.mark.parametrize("name", ["refusal_403_not_eligible", "refusal_403_revoked", "refusal_400_cert_expired"])
def test_card_get_refusals(relay, name):
    relay.expect(*login_expects(), fixture_expect("card_get", name))
    relay.script[-1].headers = None
    with pytest.raises(retry.RelayRefused):
        directory(sess(relay)).get_card()


# --- members --------------------------------------------------------------------------------------------------------

ML = corpus.case("members", "success")["response"]["json"]["member_list"]


def test_members_fixture_verifies_under_the_wsk(relay):
    relay.expect(*login_expects(), fixture_expect("members", "success"))
    relay.script[-1].headers = None
    assert directory(sess(relay)).get_members() == ML


@pytest.mark.parametrize(
    "ml",
    [
        {"o": {**ML["o"], "epoch": 2}, "sig": ML["sig"]},
        {"o": {**ML["o"], "workspace_id": corpus.IDS["workspace_b"]}, "sig": ML["sig"]},
        {"o": ML["o"], "sig": crypto.b64u(bytes(64))},
        {**ML, "extra": 1},
    ],
)
def test_members_tampered_is_misbehaving(relay, ml):
    relay.expect(
        *login_expects(), Expect("GET", f"/v2/workspaces/{WS}/members", reply=json_reply(200, {"member_list": ml}))
    )
    with pytest.raises(RelayMisbehaving):
        directory(sess(relay)).get_members()


def test_members_404_is_none(relay):
    relay.expect(*login_expects(), fixture_expect("members", "refusal_404_not_found"))
    relay.script[-1].headers = None
    assert directory(sess(relay)).get_members() is None


# --- checkpoint -----------------------------------------------------------------------------------------------------

CP_HASH = corpus.case("checkpoint_get", "success")["request"]["query"]["hash"]


def test_checkpoint_fixture_verifies(relay):
    relay.expect(*login_expects(), fixture_expect("checkpoint_get", "success"))
    relay.script[-1].headers = None
    got = directory(sess(relay)).get_checkpoint(genesis=GENESIS, hash=CP_HASH)
    assert got is not None and got.hash == CP_HASH and got.n == 1 and got.transitions_head == 0


def _cp_reply(**change):
    obj = {k: v for k, v in corpus.case("checkpoint_get", "success")["response"]["json"].items()}
    obj.update(change)
    return Expect("GET", f"/v2/workspaces/{WS}/checkpoint", reply=json_reply(200, obj))


def _other_pin():
    o = {**GENESIS_PIN["o"], "genesis": "sha256:" + "11" * 32}
    sig = crypto.sign(corpus.priv("phone.sig"), b"orch/v2/sig/genesis-pin|" + canon.dumps(o))
    return {"o": o, "sig": crypto.b64u(sig)}


@pytest.mark.parametrize(
    "change,why",
    [
        ({"transitions_head": -1}, "negative head"),
        ({"genesis": {"pin": GENESIS_PIN}}, "missing cert"),
        ({"genesis": {"pin": {**GENESIS_PIN, "sig": crypto.b64u(bytes(64))}, "cert": GENESIS_CERT}}, "pin sig"),
        ({"genesis": {"pin": _other_pin(), "cert": GENESIS_CERT}}, "pin for another genesis"),
        (
            {"genesis": {"pin": GENESIS_PIN, "cert": corpus.S2["certs"]["signed"]["bob_phone"]}},
            "cert of another person",
        ),
        (
            {"genesis": {"pin": GENESIS_PIN, "cert": corpus.S2["certs"]["signed"]["agent"]}},
            "cert of a device without operate",
        ),
        ({"checkpoint": {"o": {}, "sig": ""}}, "unsigned checkpoint"),
    ],
)
def test_checkpoint_tampered_is_misbehaving(relay, change, why):
    relay.expect(*login_expects(), _cp_reply(**change))
    with pytest.raises(RelayMisbehaving):
        directory(sess(relay)).get_checkpoint(genesis=GENESIS)


def test_checkpoint_wrong_requested_hash_is_misbehaving(relay):
    relay.expect(*login_expects(), _cp_reply())
    with pytest.raises(RelayMisbehaving):
        directory(sess(relay)).get_checkpoint(genesis=GENESIS, hash="sha256:" + "00" * 32)


def test_checkpoint_other_genesis_is_misbehaving(relay):
    relay.expect(*login_expects(), _cp_reply())
    with pytest.raises(RelayMisbehaving):
        directory(sess(relay)).get_checkpoint(genesis="sha256:" + "22" * 32)


@pytest.mark.parametrize("name,expect", [("refusal_404_not_found", None), ("refusal_410_checkpoint_gone", "gone")])
def test_checkpoint_absent_or_gone(relay, name, expect):
    relay.expect(*login_expects(), fixture_expect("checkpoint_get", name))
    relay.script[-1].headers = None
    hash_ = corpus.case("checkpoint_get", name)["request"]["query"]["hash"]
    d = directory(sess(relay))
    if expect is None:
        assert d.get_checkpoint(genesis=GENESIS, hash=hash_) is None
    else:
        with pytest.raises(retry.RelayRefused) as e:
            d.get_checkpoint(genesis=GENESIS, hash=hash_)
        assert (e.value.status, e.value.code) == (410, "checkpoint_gone")


def test_genesis_put_checked_before_sending(relay):
    s = holder_session(relay)
    with pytest.raises(ValueError):
        directory(s).put_genesis(GENESIS_PIN, GENESIS_CERT, genesis="sha256:" + "33" * 32)
    relay.expect(
        Expect(
            "PUT",
            f"/v2/workspaces/{WS}/genesis",
            headers={"Authorization": "Bearer FAKE-workspace", "Orch-Holder": "1", "Content-Type": "application/json"},
            body=canon.dumps({"pin": GENESIS_PIN, "cert": GENESIS_CERT}),
        )
    )
    directory(s).put_genesis(GENESIS_PIN, GENESIS_CERT, genesis=GENESIS)


# --- byte bounds ----------------------------------------------------------------------------------------------------


def test_oversized_success_body_is_refused(relay):
    from orch.net import http

    relay.expect(
        Expect("POST", "/v2/session/challenge", reply=json_reply(200, {"challenge": "A" * 20_000, "expires_ms": 1}))
    )
    with pytest.raises(http.TransportError) as e:
        sess(relay).challenge()
    assert e.value.kind == "too_large"


def test_deep_success_body_is_misbehaving(relay):
    from .mock import Reply

    relay.expect(Expect("POST", "/v2/session/challenge", reply=Reply(body=b"[" * 40 + b"]" * 40)))
    with pytest.raises(RelayMisbehaving):
        sess(relay).challenge()


@pytest.mark.parametrize(
    "obj",
    [
        {"challenge": crypto.b64u(bytes(31)), "expires_ms": 1},
        {"challenge": crypto.b64u(bytes(32)) + "=", "expires_ms": 1},
        {"challenge": crypto.b64u(bytes(32)), "expires_ms": 1.5},
        {"challenge": crypto.b64u(bytes(32))},
        {"challenge": crypto.b64u(bytes(32)), "expires_ms": 1, "x": 1},
    ],
)
def test_malformed_challenge_answers(relay, obj):
    relay.expect(Expect("POST", "/v2/session/challenge", reply=_raw(obj)))
    with pytest.raises(RelayMisbehaving):
        sess(relay).challenge()


def _raw(obj):
    import json

    from .mock import Reply

    return Reply(body=json.dumps(obj).encode())


@pytest.mark.parametrize("token", ["short", "a b c d e f g h", "x" * 600, "tok\r\nX-Evil: 1", 12])
def test_malformed_token_is_refused(relay, token):
    relay.expect(
        fixture_expect("session_challenge", "success"),
        Expect("POST", "/v2/session/login", reply=_raw({"token": token, "expires_ms": 1})),
    )
    with pytest.raises(RelayMisbehaving):
        sess(relay).login()


def test_session_constants_match_the_contract():
    assert session.TOKEN_LIFETIME_S - session.RELOGIN_MARGIN_S == 780
    assert session.LEASE_S == 40 and session.BAD_SIGNATURE_HOLDOFF_S == 60 and session.ORIGIN_ALARM_AFTER == 3


# --- mutation-driven: checks that are only reached by correctly signed hostile objects ------------------------------


def _resign_checkpoint(o):
    from orch.store.checkpoints import sign_object

    return sign_object(lambda b: crypto.sign(corpus.priv("wsk_a.sig"), b), o)


def test_checkpoint_signed_by_the_wsk_for_another_genesis_is_misbehaving(relay):
    """A WSK-signed checkpoint naming another genesis, served with the genuine pin: refused by the checkpoint check."""
    cp = corpus.case("checkpoint_get", "success")["response"]["json"]["checkpoint"]
    forged = _resign_checkpoint({**cp["o"], "genesis": "sha256:" + "44" * 32})
    relay.expect(*login_expects(), _cp_reply(checkpoint=forged))
    with pytest.raises(RelayMisbehaving):
        directory(sess(relay)).get_checkpoint(genesis=GENESIS)


def test_members_signed_by_the_wsk_for_another_workspace_is_misbehaving(relay):
    o = {**ML["o"], "workspace_id": corpus.IDS["workspace_b"]}
    sig = crypto.sign(corpus.priv("wsk_a.sig"), b"orch/v2/sig/member-list|" + canon.dumps(o))
    ml = {"o": o, "sig": crypto.b64u(sig)}
    relay.expect(
        *login_expects(), Expect("GET", f"/v2/workspaces/{WS}/members", reply=json_reply(200, {"member_list": ml}))
    )
    with pytest.raises(RelayMisbehaving):
        directory(sess(relay)).get_members()


def test_card_of_another_wsk_validly_delegated_is_misbehaving(relay):
    """The owner delegated another WSK for this workspace id (a valid card, just not ours)."""
    from orch.identity.certs import make_delegation

    alice = corpus.priv("person_alice.sig")
    wsk_b = corpus.priv("wsk_b.sig")
    deleg = make_delegation(
        corpus.pub("person_alice.sig"), lambda b: crypto.sign(alice, b), workspace_id=WS,
        wsk_pub=corpus.pub("wsk_b.sig"), client_hosted=False, issued_ms=1,
    )  # fmt: skip
    o = {**CARD["o"], "wsk_pub": crypto.b64u(corpus.pub("wsk_b.sig"))}
    sig = crypto.b64u(crypto.sign(wsk_b, b"orch/v2/sig/card-wsk|" + canon.dumps(o)))
    other = {"delegation": deleg, "o": o, "sig": sig, "sealed": CARD["sealed"]}
    relay.expect(*login_expects(), Expect("GET", f"/v2/workspaces/{WS}/card", reply=json_reply(200, {"card": other})))
    with pytest.raises(RelayMisbehaving):
        directory(sess(relay)).get_card()


def test_holder_conflict_wait_ends_when_stop_is_set(relay):
    """Review L2: the 40 s lease wait honours the stop event."""
    import threading
    import time

    relay.expect(*login_expects(), fixture_expect("host_hold", "refusal_409_holder_conflict"))
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    s = make_session(relay.origin(), V_ORIGIN, stop=stop, clock=_RealSleepClock())
    t0 = time.monotonic()
    with pytest.raises(session.SessionError):
        s.hold()
    assert time.monotonic() - t0 < 5.0


class _RealSleepClock(Clock):
    def sleep(self, s):
        import time

        time.sleep(s)

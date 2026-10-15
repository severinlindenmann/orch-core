"""``orch.relay_owner.client`` against the relay HTTP fixtures (accounts, devices, session, cert queue), on a loopback
fixture relay, and against a hostile relay (oversized, malformed, wrong types, extra fields, replayed challenges,
redirects, contradicting answers). The signed origin is the fixtures' own; nothing leaves the loopback."""

from __future__ import annotations

import json

import pytest

from orch import canon, crypto
from orch.relay_owner import (
    ROUTES,
    OwnerClient,
    OwnerMisbehaving,
    OwnerRefused,
    OwnerRequestError,
    OwnerTransportError,
)
from orch.relay_owner import (
    client as client_mod,
)

from ..relay import corpus
from ..relay.helpers import V_ORIGIN
from ..relay.mock import Expect, MockRelay, Reply, fixture_expect, json_reply
from .kit import PERSON, PHONE, PK, VecSigner, challenge_of, client, login_expects, proof_ok

CERTS = corpus.S2["certs"]["signed"]


@pytest.fixture
def relay():
    m = MockRelay()
    yield m
    assert m.errors == []
    m.close()


def refusals(name: str) -> list[str]:
    return [c["name"] for c in corpus.fixture(name)["cases"] if c["response"]["status"] >= 300]


def expect_refusal(e: pytest.ExceptionInfo, name: str, case: str) -> None:
    resp = corpus.case(name, case)["response"]
    assert (e.value.status, e.value.code) == (resp["status"], resp["json"]["error"])
    ra = resp["headers"].get("Retry-After")
    assert e.value.retry_after == (int(ra) if ra else None)


# --- the route table is the fixtures' closed set ----------------------------------------------------------------------

FIXTURE_ROUTE = {
    "session_challenge": "challenge", "session_login": "login", "session_logout": "logout", "accounts": "account",
    "device_put": "device_put", "device_get": "device_get", "cert_requests_poll": "cert_requests",
    "cert_challenge": "cert_challenge", "cert_rejection": "cert_rejection",
}  # fmt: skip


@pytest.mark.parametrize("fixture,name", sorted(FIXTURE_ROUTE.items()))
def test_every_fixture_refusal_code_is_in_the_routes_closed_set(fixture, name):
    f = corpus.fixture(fixture)
    route = ROUTES[name]
    assert route.template == f"{f['route']['method']} {f['route']['path']}"
    for c in f["cases"]:
        if c["response"]["status"] >= 300:
            assert c["response"]["json"]["error"] in route.codes, (fixture, c["name"])
        else:
            assert set(c["response"]["json"]) == route.ok, (fixture, c["name"])


# --- construction -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin", ["http://relay.example", "https://relay.example/", "https://Relay.example", "ftp://x", "https://a@b"]
)
def test_the_origin_is_checked(origin):
    with pytest.raises(OwnerRequestError):
        OwnerClient(origin, VecSigner())


def test_a_signer_whose_id_is_not_of_its_key_is_refused():
    s = VecSigner()
    s.device_id = bytes(16)
    with pytest.raises(OwnerRequestError):
        OwnerClient(V_ORIGIN, s, transport=object())  # type: ignore[arg-type]


def test_a_proxy_for_an_http_origin_is_refused():
    with pytest.raises(OwnerTransportError) as e:
        OwnerClient("http://127.0.0.1:9", VecSigner(), proxy="proxy.example:3128")
    assert e.value.kind == "request"


# --- challenge and device login ---------------------------------------------------------------------------------------


def test_device_login_signs_the_fixture_challenge_as_a_device_for_the_stored_origin(relay):
    s = VecSigner()
    relay.expect(*login_expects(signer=s))
    c = client(relay, s)
    c.login()
    assert c.logged_in and "FAKE" not in repr(c)
    assert len(s.signed) == 1


def test_the_fixture_login_body_is_a_device_login():
    body = corpus.case("session_login", "success")["request"]["json"]
    assert body["kind"] == "device" and proof_ok(body, body["id"], corpus.pub("phone.sig"))


@pytest.mark.parametrize("name", refusals("session_login"))
def test_login_refusal_fixtures(relay, name):
    relay.expect(fixture_expect("session_challenge", "success"), fixture_expect("session_login", name, body=None))
    with pytest.raises(OwnerRefused) as e:
        client(relay).login()
    expect_refusal(e, "session_login", name)


@pytest.mark.parametrize("name", refusals("session_challenge"))
def test_challenge_refusal_fixtures(relay, name):
    relay.expect(fixture_expect("session_challenge", name, body=None))
    with pytest.raises(OwnerRefused) as e:
        client(relay).challenge()
    expect_refusal(e, "session_challenge", name)
    assert e.value.transient == (name == "refusal_429_rate_limited")


def test_a_challenge_served_twice_is_never_signed_twice(relay):
    ch = challenge_of()
    relay.expect(*login_expects(challenge=ch))
    relay.expect(fixture_expect("session_challenge", "success"))
    s = VecSigner()
    c = client(relay, s)
    c.login()
    with pytest.raises(OwnerMisbehaving):
        c.login()
    assert len(s.signed) == 1


@pytest.mark.parametrize(
    "answer",
    [
        {"challenge": crypto.b64u(b"x" * 31), "expires_ms": 1},
        {"challenge": crypto.b64u(b"x" * 33), "expires_ms": 1},
        {"challenge": 7, "expires_ms": 1},
        {"challenge": crypto.b64u(b"x" * 32), "expires_ms": "1"},
        {"challenge": crypto.b64u(b"x" * 32), "expires_ms": -1},
        {"challenge": crypto.b64u(b"x" * 32)},
        {"challenge": crypto.b64u(b"x" * 32), "expires_ms": 1, "extra": 1},
        [],
    ],
)
def test_a_hostile_challenge_answer_is_misbehaving(relay, answer):
    relay.expect(Expect("POST", "/v2/session/challenge", reply=json_reply(200, answer)))
    with pytest.raises(OwnerMisbehaving):
        client(relay).challenge()


@pytest.mark.parametrize(
    "answer",
    [
        {"token": "short", "expires_ms": 1},
        {"token": "has a space in it", "expires_ms": 1},
        {"token": 12345678, "expires_ms": 1},
        {"token": "A" * 513, "expires_ms": 1},
        {"token": "FAKE-primary", "expires_ms": 1.5},
        {"token": "FAKE-primary", "expires_ms": 1, "admin": True},
    ],
)
def test_a_hostile_login_answer_is_misbehaving_and_no_token_is_kept(relay, answer):
    s = VecSigner()
    relay.expect(
        *login_expects(signer=s)[:1],
        Expect("POST", "/v2/session/login", reply=Reply(200, body=json.dumps(answer).encode())),
    )
    c = client(relay, s)
    with pytest.raises(OwnerMisbehaving):
        c.login()
    assert not c.logged_in


@pytest.mark.parametrize(
    "reply",
    [
        Reply(200, body=b"{not json"),
        Reply(200, body=b'{"token":"FAKE-primary","token":"FAKE-primary","expires_ms":1}'),  # duplicate key
        Reply(200, body=b"[" * 40 + b"]" * 40),
        Reply(200, body=b'{"token":"FAKE-primary","expires_ms":1e3}'),
        Reply(200, body=b"\xff\xfe"),
    ],
)
def test_a_malformed_success_body_is_misbehaving(relay, reply):
    relay.expect(*login_expects()[:1], Expect("POST", "/v2/session/login", reply=reply))
    with pytest.raises(OwnerMisbehaving):
        client(relay).login()


def test_an_oversized_answer_is_a_transport_too_large_and_never_parsed(relay):
    big = canon.dumps({"challenge": crypto.b64u(b"x" * 32), "expires_ms": 1, "pad": "a" * 20_000})
    relay.expect(Expect("POST", "/v2/session/challenge", reply=Reply(200, body=big)))
    with pytest.raises(OwnerTransportError) as e:
        client(relay).challenge()
    assert e.value.kind == "too_large" and e.value.transient


def test_an_oversized_chunked_answer_is_refused_too(relay):
    big = canon.dumps({"challenge": crypto.b64u(b"x" * 32), "expires_ms": 1, "pad": "a" * 20_000})
    relay.expect(Expect("POST", "/v2/session/challenge", reply=Reply(200, body=big, fault="chunked")))
    with pytest.raises(OwnerTransportError) as e:
        client(relay).challenge()
    assert e.value.kind == "too_large"


def test_a_redirect_is_never_followed(relay):
    relay.expect(Expect("POST", "/v2/session/challenge", reply=Reply(302, {"Location": "https://evil.example/"})))
    with pytest.raises(OwnerTransportError) as e:
        client(relay).challenge()
    assert e.value.kind == "redirect" and not e.value.transient
    assert len(relay.seen) == 1


def test_an_unknown_refusal_code_is_not_echoed(relay):
    relay.expect(Expect("POST", "/v2/session/challenge", reply=json_reply(403, {"error": "please_send_your_code"})))
    with pytest.raises(OwnerRefused) as e:
        client(relay).challenge()
    assert e.value.code is None and "please" not in str(e.value)


@pytest.mark.parametrize("ra,want", [("20", 20), ("99999999", 3600), ("-1", None), ("2.5", None), ("٣", None)])
def test_retry_after_is_integer_seconds_capped(relay, ra, want):
    relay.expect(
        Expect("POST", "/v2/session/challenge", reply=json_reply(429, {"error": "rate_limited"}, {"Retry-After": ra}))
    )
    with pytest.raises(OwnerRefused) as e:
        client(relay).challenge()
    assert e.value.retry_after == want


def test_a_401_relogs_in_once_and_resends_the_identical_bytes(relay):
    rej = corpus.case("cert_rejection", "success")
    rid = rej["request"]["path"].split("/")[3]
    relay.expect(*login_expects(token="FAKE-stale1"))
    relay.expect(Expect("POST", rej["request"]["path"], reply=json_reply(401, {"error": "unauthenticated"})))
    relay.expect(*login_expects(challenge=b"\x01" * 32), fixture_expect("cert_rejection", "success"))
    c = client(relay)
    c.post_rejection(rid, rej["request"]["json"]["rejection"])
    bodies = [s.body for s in relay.seen if s.path.endswith("/rejection")]
    assert len(bodies) == 2 and bodies[0] == bodies[1]


def test_a_second_401_is_not_retried_again(relay):
    rej = corpus.case("cert_rejection", "success")
    rid = rej["request"]["path"].split("/")[3]
    relay.expect(*login_expects(token="FAKE-stale1"))
    relay.expect(Expect("POST", rej["request"]["path"], reply=json_reply(401, {"error": "unauthenticated"})))
    relay.expect(*login_expects(token="FAKE-stale2", challenge=b"\x02" * 32))
    relay.expect(Expect("POST", rej["request"]["path"], reply=json_reply(401, {"error": "expired"})))
    with pytest.raises(OwnerRefused) as e:
        client(relay).post_rejection(rid, rej["request"]["json"]["rejection"])
    assert (e.value.status, e.value.code) == (401, "expired")


def test_the_token_is_renewed_before_it_runs_out(relay):
    t = [0.0]
    c = client(relay, mono=lambda: t[0])
    relay.expect(*login_expects(token="FAKE-first1"))
    c.login()
    t[0] += client_mod.TOKEN_LIFETIME_S - client_mod.RELOGIN_MARGIN_S
    relay.expect(*login_expects(challenge=b"\x03" * 32), fixture_expect("cert_requests_poll", "success"))
    assert len(c.cert_requests(PERSON, wait=25)) == 1


def test_logout_is_best_effort_and_forgets_the_token(relay):
    relay.expect(*login_expects(token="FAKE-device"), fixture_expect("session_logout", "success"))
    c = client(relay)
    c.login()
    c.logout()
    assert not c.logged_in
    c.logout()  # nothing to send
    relay.expect(*login_expects(token="FAKE-device", challenge=b"\x04" * 32))
    relay.expect(Expect("POST", "/v2/session/logout", reply=Reply(500)))
    c.login()
    c.logout()
    assert not c.logged_in


# --- POST /v2/accounts ------------------------------------------------------------------------------------------------

ACCOUNT = corpus.case("accounts", "success")["request"]["json"]


def account_check(raw: bytes) -> bool:
    b = json.loads(raw)
    keep = {k: ACCOUNT[k] for k in ("invite", "pk_pub", "device_cert", "challenge")}
    return set(b) == set(ACCOUNT) and {k: b[k] for k in keep} == keep and proof_ok(b, PHONE, corpus.pub("phone.sig"))


def account_expects(reply=None):
    c = corpus.case("accounts", "success")
    out = reply or json_reply(200, c["response"]["json"])
    return [
        fixture_expect("session_challenge", "success"),
        Expect("POST", "/v2/accounts", {}, {"Content-Type": "application/json"}, account_check, out),
    ]


def test_create_account_success_fixture(relay):
    relay.expect(*account_expects())
    c = client(relay, VecSigner("phone"))
    assert c.create_account(ACCOUNT["invite"], PK, ACCOUNT["device_cert"]) == PERSON
    assert not c.logged_in  # an account needs no session


def test_create_account_exact_retry_fixture(relay):
    relay.expect(*account_expects(json_reply(200, corpus.case("accounts", "exact_retry")["response"]["json"])))
    assert client(relay, VecSigner("phone")).create_account(ACCOUNT["invite"], PK, ACCOUNT["device_cert"]) == PERSON


@pytest.mark.parametrize("name", refusals("accounts"))
def test_create_account_refusal_fixtures(relay, name):
    relay.expect(fixture_expect("session_challenge", "success"), fixture_expect("accounts", name, body=None))
    with pytest.raises(OwnerRefused) as e:
        client(relay, VecSigner("phone")).create_account(ACCOUNT["invite"], PK, ACCOUNT["device_cert"])
    expect_refusal(e, "accounts", name)
    assert ACCOUNT["invite"] not in str(e.value) and ACCOUNT["invite"] not in repr(e.value)


def test_create_account_answer_for_another_person_is_misbehaving(relay):
    relay.expect(*account_expects(json_reply(200, {"person_id": "00" * 16})))
    with pytest.raises(OwnerMisbehaving):
        client(relay, VecSigner("phone")).create_account(ACCOUNT["invite"], PK, ACCOUNT["device_cert"])


@pytest.mark.parametrize(
    "invite,pk,cert,signer",
    [
        ("", PK, ACCOUNT["device_cert"], "phone"),
        ("has space", PK, ACCOUNT["device_cert"], "phone"),
        ("x" * 257, PK, ACCOUNT["device_cert"], "phone"),
        (ACCOUNT["invite"], corpus.pub("phone.sig"), ACCOUNT["device_cert"], "phone"),  # cert not under this key
        (ACCOUNT["invite"], b"\x04" + b"\x00" * 64, ACCOUNT["device_cert"], "phone"),  # not a point
        (ACCOUNT["invite"], PK, ACCOUNT["device_cert"], "primary"),  # another device's certificate
        (ACCOUNT["invite"], PK, {"o": {}, "sig": ""}, "phone"),
    ],
)
def test_create_account_is_refused_locally_before_anything_is_sent(relay, invite, pk, cert, signer):
    with pytest.raises(OwnerRequestError) as e:
        client(relay, VecSigner(signer)).create_account(invite, pk, cert)
    assert relay.seen == [] and ACCOUNT["invite"] not in str(e.value)


# --- PUT and GET /v2/devices/{d} --------------------------------------------------------------------------------------

DEVICE_CERT = corpus.case("device_put", "success")["request"]["json"]["cert"]


@pytest.mark.parametrize("name", ["success", "resigned_identical", "exact_retry"])
def test_register_device_fixtures(relay, name):
    relay.expect(fixture_expect("device_put", name))
    cert = corpus.case("device_put", name)["request"]["json"]["cert"]  # resigned_identical: a new signature
    assert client(relay).register_device(cert, PK) == PHONE
    assert relay.seen[0].headers.get("authorization") is None  # the certificate is the authority


@pytest.mark.parametrize("name", refusals("device_put"))
def test_register_device_refusal_fixtures(relay, name):
    relay.expect(fixture_expect("device_put", name, body=None))
    with pytest.raises(OwnerRefused) as e:
        client(relay).register_device(DEVICE_CERT, PK)
    expect_refusal(e, "device_put", name)


def test_register_device_answer_for_another_device_is_misbehaving(relay):
    relay.expect(Expect("PUT", f"/v2/devices/{PHONE}", reply=json_reply(200, {"device_id": "00" * 16})))
    with pytest.raises(OwnerMisbehaving):
        client(relay).register_device(DEVICE_CERT, PK)


def test_register_device_refuses_a_certificate_that_does_not_verify(relay):
    bad = {"o": {**DEVICE_CERT["o"], "scopes_max": ["look"]}, "sig": DEVICE_CERT["sig"]}
    with pytest.raises(OwnerRequestError):
        client(relay).register_device(bad, PK)
    with pytest.raises(OwnerRequestError):
        client(relay).register_device(DEVICE_CERT, corpus.pub("phone.sig"))
    assert relay.seen == []


def test_get_device_success_fixture_is_verified_under_the_pinned_person_key(relay):
    relay.expect(*login_expects(token="FAKE-device"), fixture_expect("device_get", "success"))
    assert client(relay).get_device(PHONE, PK) == corpus.case("device_get", "success")["response"]["json"]["cert"]


def test_get_device_refuses_a_certificate_of_another_key_or_device(relay):
    relay.expect(*login_expects(token="FAKE-device"), fixture_expect("device_get", "success"))
    c = client(relay)
    with pytest.raises(OwnerMisbehaving):
        c.get_device(PHONE, corpus.pub("phone.sig"))
    relay.expect(Expect("GET", f"/v2/devices/{'ab' * 16}", reply=json_reply(200, {"cert": DEVICE_CERT})))
    with pytest.raises(OwnerMisbehaving):
        c.get_device("ab" * 16, PK)


@pytest.mark.parametrize("name", [n for n in refusals("device_get") if n != "refusal_401_unauthenticated"])
def test_get_device_refusal_fixtures(relay, name):
    relay.expect(*login_expects(token="FAKE-device"), fixture_expect("device_get", name, body=None))
    with pytest.raises(OwnerRefused) as e:
        client(relay).get_device(PHONE, PK)
    expect_refusal(e, "device_get", name)


# --- the cert queue routes --------------------------------------------------------------------------------------------


def test_cert_requests_success_fixture(relay):
    relay.expect(*login_expects(), fixture_expect("cert_requests_poll", "success"))
    got = client(relay).cert_requests(PERSON, wait=25)
    assert got == corpus.case("cert_requests_poll", "success")["response"]["json"]["requests"]


@pytest.mark.parametrize("name", [n for n in refusals("cert_requests_poll") if "unauthenticated" not in n])
def test_cert_requests_refusal_fixtures(relay, name):
    relay.expect(*login_expects(), fixture_expect("cert_requests_poll", name, body=None))
    with pytest.raises(OwnerRefused) as e:
        client(relay).cert_requests(PERSON, wait=25)
    expect_refusal(e, "cert_requests_poll", name)


def test_cert_requests_without_wait_and_bad_wait(relay):
    relay.expect(
        *login_expects(),
        Expect("GET", f"/v2/persons/{PERSON}/cert-requests", {}, reply=json_reply(200, {"requests": []})),
    )
    c = client(relay)
    assert c.cert_requests(PERSON) == []
    for w in (-1, 26, "5", True):
        with pytest.raises(OwnerRequestError):
            c.cert_requests(PERSON, wait=w)  # type: ignore[arg-type]
    with pytest.raises(OwnerRequestError):
        c.cert_requests("../admin")


REQ = corpus.case("cert_requests_poll", "success")["response"]["json"]["requests"][0]


@pytest.mark.parametrize(
    "answer",
    [
        {"requests": {}},
        {"requests": [REQ, REQ]},  # a request listed twice
        {"requests": [REQ] * 65},
        {"requests": ["x"]},
        {"requests": [{"o": REQ["o"]}]},
        {"requests": [{**REQ, "extra": 1}]},
        {"requests": [{"o": {**REQ["o"], "request_id": 5}, "sig": REQ["sig"]}]},
        {"requests": [{"o": [], "sig": REQ["sig"]}]},
        {"requests": [], "next": 1},
    ],
)
def test_a_hostile_request_list_is_misbehaving(relay, answer):
    relay.expect(*login_expects(), Expect("GET", f"/v2/persons/{PERSON}/cert-requests", reply=json_reply(200, answer)))
    with pytest.raises((OwnerMisbehaving, OwnerTransportError)):
        client(relay).cert_requests(PERSON)


@pytest.mark.parametrize("route", ["cert_challenge", "cert_rejection"])
@pytest.mark.parametrize("name", ["success", "resigned_identical", "exact_retry"])
def test_post_challenge_and_rejection_fixtures(relay, route, name):
    c = corpus.case(route, name)
    rid = c["request"]["path"].split("/")[3]
    relay.expect(*login_expects(), fixture_expect(route, name))
    cl = client(relay)
    if route == "cert_challenge":
        cl.post_challenge(rid, c["request"]["json"]["challenge"])
    else:
        cl.post_rejection(rid, c["request"]["json"]["rejection"])


@pytest.mark.parametrize("route", ["cert_challenge", "cert_rejection"])
def test_post_challenge_and_rejection_refusal_fixtures(relay, route):
    for name in [n for n in refusals(route) if "unauthenticated" not in n]:
        c = corpus.case(route, "success")
        rid = c["request"]["path"].split("/")[3]
        relay.expect(*login_expects(challenge=crypto.sha256(name.encode())), fixture_expect(route, name, body=None))
        cl = client(relay)
        with pytest.raises(OwnerRefused) as e:
            if route == "cert_challenge":
                cl.post_challenge(rid, c["request"]["json"]["challenge"])
            else:
                cl.post_rejection(rid, c["request"]["json"]["rejection"])
        expect_refusal(e, route, name)


def test_a_challenge_answer_with_fields_is_misbehaving(relay):
    c = corpus.case("cert_challenge", "success")
    rid = c["request"]["path"].split("/")[3]
    relay.expect(*login_expects(), Expect("POST", c["request"]["path"], reply=json_reply(200, {"state": "signed"})))
    with pytest.raises(OwnerMisbehaving):
        client(relay).post_challenge(rid, c["request"]["json"]["challenge"])


def test_an_oversized_request_is_refused_locally(relay):
    with pytest.raises(OwnerRequestError) as e:
        client(relay).post_rejection("ab" * 16, {"o": {"pad": "a" * 20_000}, "sig": ""})
    assert e.value.what == "too_large"
    assert relay.seen == []  # the body is built (and refused) before the login


def test_a_bad_request_id_is_refused_locally(relay):
    with pytest.raises(OwnerRequestError):
        client(relay).post_challenge("../../v2/accounts", {})
    assert relay.seen == []


def test_no_secret_in_logs(relay, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    relay.expect(*account_expects())
    client(relay, VecSigner("phone")).create_account(ACCOUNT["invite"], PK, ACCOUNT["device_cert"])
    relay.expect(*login_expects(token="FAKE-secret-token", challenge=b"\x05" * 32))
    c = client(relay)
    c.login()
    text = caplog.text
    for secret in (ACCOUNT["invite"], "FAKE-secret-token", ACCOUNT["sig"], ACCOUNT["challenge"]):
        assert secret not in text


# --- the primary's own certificate ------------------------------------------------------------------------------------


class VecPK:
    """A vector person key behind the ``PersonKeySource`` surface the builders use."""

    def __init__(self) -> None:
        self._k = corpus.priv("person_alice.sig")
        self.pk_pub = PK
        self.person_id = crypto.person_id(PK)
        self.labels: list[str] = []

    def signer(self, label):
        from orch.crypto.labels import L

        def sign(payload: bytes) -> bytes:
            assert payload.startswith(L[label])
            self.labels.append(label)
            return crypto.sign(self._k, payload)

        return sign

    def derive_label_key(self) -> bytes:
        return b"\x07" * 32


def test_primary_certificate_is_look_only_never_expiring_and_label_sealed():
    from orch.identity import certs

    s = VecSigner("primary")
    cert = client_mod.primary_certificate(VecPK(), s, corpus.pub("primary.kx"), label="Mac\x1b[31m", created_ms=5)
    o = certs.check_cert(cert, PK)
    assert o["scopes_max"] == ["look"] and o["expires_ms"] is None and o["device_id"] == s.device_id.hex()
    assert certs.open_device_label(b"\x07" * 32, crypto.person_id(PK), s.device_id,
                                   crypto.unb64u(o["label_sealed"])) == "Mac[31m"  # fmt: skip

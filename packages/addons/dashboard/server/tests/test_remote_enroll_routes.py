"""#1198 R1-5: enrollment on the remote listener: codes, challenges, registration."""

# ruff: noqa: F811

import os
import stat

import pytest
from passkey_kit import JSON, auth, clock, code_of, enroll, login  # noqa: F401
from remote_kit import (  # noqa: F401
    HTTPS,
    NAME,
    SOURCE,
    app,
    bearer,
    front,
    loop,
    passkey_store,
    remote,
)
from webauthn_kit import Authenticator

from orch_dashboard.passkey_routes import CHALLENGE_TTL, ENROLL_TRIES, ENROLL_TTL


# --- enrollment -------------------------------------------------------------------------------------------------------
def test_enrollment_stores_the_passkey_and_logs_the_browser_in(front, app, passkey_store, auth):
    authn, res = enroll(front, app)
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    assert front.get("/api/session", headers=bearer(token)).json()["login"] == "remote"
    (record,) = passkey_store.read()
    assert record.name == "MacBook, Chrome" and record.alg == -7 and record.created == record.last_used
    mode = stat.S_IMODE(os.stat(passkey_store.path).st_mode)
    assert mode == 0o600 and os.stat(passkey_store.path).st_uid == os.getuid()
    assert auth.listener_of(token) == "remote"


def test_the_options_ask_for_what_the_spec_requires(front, app):
    opts = front.post("/auth/enroll/options", json={"code": app.state.enroll.mint()}, headers=JSON).json()["publicKey"]
    assert opts["rp"]["id"] == NAME and opts["attestation"] == "none"
    assert [p["alg"] for p in opts["pubKeyCredParams"]] == [-7, -8]
    assert opts["authenticatorSelection"] == {
        "residentKey": "required",
        "requireResidentKey": True,
        "userVerification": "required",
    }
    login_opts = front.post("/auth/passkey/options", json={}, headers=JSON).json()["publicKey"]
    assert login_opts["rpId"] == NAME and login_opts["userVerification"] == "required"
    assert login_opts["allowCredentials"] == [] and len(login_opts["challenge"]) == 43


def test_a_second_passkey_reuses_the_user_handle_and_excludes_the_first(front, app, passkey_store):
    first, res = enroll(front, app)
    assert res.status_code == 200
    opts = front.post("/auth/enroll/options", json={"code": app.state.enroll.mint()}, headers=JSON).json()["publicKey"]
    assert opts["user"]["id"] == passkey_store.read()[0].user_handle
    assert [e["id"] for e in opts["excludeCredentials"]] == [passkey_store.read()[0].credential_id]
    second, res = enroll(front, app, name="Phone")
    assert res.status_code == 200 and len(passkey_store.read()) == 2


def test_an_enrollment_code_is_single_use(front, app, passkey_store):
    code = app.state.enroll.mint()
    _, ok = enroll(front, app, code=code)
    assert ok.status_code == 200
    _, again = enroll(front, app, code=code)
    assert again.status_code == 401 and code_of(again) == "enroll.expired"
    assert len(passkey_store.read()) == 1


def test_two_browsers_with_one_code_only_the_first_completes(front, app, passkey_store):
    code = app.state.enroll.mint()
    a, b = Authenticator(NAME), Authenticator(NAME)
    oa = front.post("/auth/enroll/options", json={"code": code}, headers=JSON).json()
    ob = front.post("/auth/enroll/options", json={"code": code}, headers=JSON).json()
    ra = front.post(
        "/auth/enroll/register",
        json={"challenge_id": oa["challenge_id"], "code": code, "name": "A", **a.create(oa["publicKey"])},
        headers=JSON,
    )
    rb = front.post(
        "/auth/enroll/register",
        json={"challenge_id": ob["challenge_id"], "code": code, "name": "B", **b.create(ob["publicKey"])},
        headers=JSON,
    )
    assert (ra.status_code, rb.status_code) == (200, 401) and code_of(rb) == "enroll.expired"
    assert [r.name for r in passkey_store.read()] == ["A"]


def test_an_enrollment_code_lives_fifteen_minutes(front, app, clock):
    code = app.state.enroll.mint()
    clock.now += ENROLL_TTL - 1
    assert front.post("/auth/enroll/options", json={"code": code}, headers=JSON).status_code == 200
    clock.now += 2
    res = front.post("/auth/enroll/options", json={"code": code}, headers=JSON)
    assert res.status_code == 401 and code_of(res) == "enroll.expired"


def test_five_wrong_secrets_burn_only_that_code(front, app):
    mine, other = app.state.enroll.mint(), app.state.enroll.mint()
    ident = mine.partition("-")[0]
    for i in range(ENROLL_TRIES):
        res = front.post(
            "/auth/enroll/options",
            json={"code": f"{ident}-wrong{i:02d}xxxxxxxxxxxx"},
            headers={**JSON, "X-Orch-Client": f"198.51.100.{i + 1}"},
        )
        assert res.status_code == 401 and code_of(res) == "enroll.expired"
    assert front.post("/auth/enroll/options", json={"code": mine}, headers=JSON).status_code == 401  # burned
    assert front.post("/auth/enroll/options", json={"code": other}, headers=JSON).status_code == 200


def test_a_flood_of_wrong_codes_from_one_source_leaves_another_code_usable(front, app):
    code = app.state.enroll.mint()
    statuses = [
        front.post(
            "/auth/enroll/options", json={"code": f"{i:012x}-guess{i:05d}xxxxxxxxxxxx"}, headers=JSON
        ).status_code
        for i in range(30)
    ]
    assert statuses[0] == 401 and 429 in statuses  # the source runs out of budget, not the owner's code
    other = front.post("/auth/enroll/options", json={"code": code}, headers={**JSON, "X-Orch-Client": "198.51.100.77"})
    assert other.status_code == 200
    assert len(app.state.enroll.open) == 1


def test_a_malformed_or_unknown_id_touches_no_code(front, app):
    code = app.state.enroll.mint()
    for bad in ("nodash" * 4, "-" + "x" * 20, "zzzzzzzzzzzz-" + "y" * 22):
        assert front.post("/auth/enroll/options", json={"code": bad}, headers=JSON).status_code == 401
    entry = next(iter(app.state.enroll.open.values()))
    assert entry[2] == 0
    assert front.post("/auth/enroll/options", json={"code": code}, headers=JSON).status_code == 200


def test_a_wrong_and_an_unknown_and_a_dead_code_answer_the_same(front, app, clock):
    dead = app.state.enroll.mint()
    clock.now += ENROLL_TTL + 1
    a = front.post("/auth/enroll/options", json={"code": dead}, headers=JSON)
    b = front.post("/auth/enroll/options", json={"code": "x" * 32}, headers=JSON)
    assert (a.status_code, a.json()) == (b.status_code, b.json())


def test_a_challenge_belongs_to_the_code_that_asked_for_it(front, app, passkey_store):
    one, two = app.state.enroll.mint(), app.state.enroll.mint()
    opts = front.post("/auth/enroll/options", json={"code": one}, headers=JSON).json()
    authn = Authenticator(NAME)
    res = front.post(
        "/auth/enroll/register",
        json={"challenge_id": opts["challenge_id"], "code": two, "name": "x", **authn.create(opts["publicKey"])},
        headers=JSON,
    )
    assert res.status_code == 401 and code_of(res) == "enroll.expired" and passkey_store.read() == []


def test_a_challenge_is_single_use_and_lives_120_seconds(front, app, clock):
    code = app.state.enroll.mint()
    opts = front.post("/auth/enroll/options", json={"code": code}, headers=JSON).json()
    authn = Authenticator(NAME)
    created = authn.create(opts["publicKey"])
    clock.now += CHALLENGE_TTL + 1
    res = front.post(
        "/auth/enroll/register",
        json={"challenge_id": opts["challenge_id"], "code": code, "name": "x", **created},
        headers=JSON,
    )
    assert res.status_code == 401 and code_of(res) == "enroll.expired"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a, o: a.create(o, origin="https://evil.example.com"),
        lambda a, o: Authenticator("evil.example.com").create({**o}, origin=None),
    ],
)
def test_a_registration_for_another_origin_or_rp_is_refused_and_burns_nothing_but_the_challenge(
    front, app, passkey_store, mutate
):
    code = app.state.enroll.mint()
    opts = front.post("/auth/enroll/options", json={"code": code}, headers=JSON).json()
    created = mutate(Authenticator(NAME), opts["publicKey"])
    res = front.post(
        "/auth/enroll/register",
        json={"challenge_id": opts["challenge_id"], "code": code, "name": "x", **created},
        headers=JSON,
    )
    assert res.status_code == 401 and code_of(res) == "enroll.expired" and passkey_store.read() == []
    _, ok = enroll(front, app, code=code)  # the code was not spent by a failed registration
    assert ok.status_code == 200


@pytest.mark.parametrize("name", ["", " ", "a‮b", "a\x00b", "x" * 65, "line\nbreak"])
def test_a_bad_device_name_is_refused(front, app, passkey_store, name):
    _, res = enroll(front, app, name=name)
    assert res.status_code in (400, 401) and passkey_store.read() == []


def test_an_extra_or_oversized_field_is_refused(front, app):
    code = app.state.enroll.mint()
    assert front.post("/auth/enroll/options", json={"code": code, "extra": 1}, headers=JSON).status_code == 400
    assert front.post("/auth/passkey/login", json={"challenge_id": "x"}, headers=JSON).status_code == 400
    big = front.post("/auth/enroll/options", json={"code": "a" * 20_000}, headers=JSON)
    assert big.status_code == 413

"""#1198 R1-5: passkey login on the remote listener, the passkey store, rate limits, session caps."""

# ruff: noqa: F811

import json
import os

import pytest
from conftest import ORIGIN
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

from orch_dashboard.passkey_routes import CHALLENGE_TTL


# --- login ------------------------------------------------------------------------------------------------------------
def test_login_with_an_enrolled_passkey_opens_a_remote_session(front, app, auth, passkey_store):
    authn, _ = enroll(front, app)
    res = login(front, authn)
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    assert auth.login_kind(token) == "remote" and auth.listener_of(token) == "remote"
    assert front.get("/api/session", headers=bearer(token)).status_code == 200
    assert passkey_store.read()[0].sign_count == 1  # recorded, not enforced


def test_a_second_assertion_with_a_lower_counter_still_logs_in(front, app):
    authn, _ = enroll(front, app)
    assert login(front, authn).status_code == 200
    authn.count = 0
    assert login(front, authn).status_code == 200  # synced passkeys report 0: never enforced


def test_login_without_a_user_handle_works_and_a_wrong_one_does_not(front, app):
    authn, _ = enroll(front, app)
    opts = front.post("/auth/passkey/options", json={}, headers=JSON).json()
    got = authn.get(opts["publicKey"], handle=False)
    assert (
        front.post("/auth/passkey/login", json={"challenge_id": opts["challenge_id"], **got}, headers=JSON).status_code
        == 200
    )
    authn.user_handle = b"\x01" * 32
    assert login(front, authn).status_code == 401


def test_a_challenge_cannot_be_replayed(front, app):
    authn, _ = enroll(front, app)
    opts = front.post("/auth/passkey/options", json={}, headers=JSON).json()
    body = {"challenge_id": opts["challenge_id"], **authn.get(opts["publicKey"])}
    assert front.post("/auth/passkey/login", json=body, headers=JSON).status_code == 200
    again = front.post("/auth/passkey/login", json=body, headers=JSON)
    assert again.status_code == 401 and code_of(again) == "passkey.invalid"


def test_a_login_challenge_expires(front, app, clock):
    authn, _ = enroll(front, app)
    opts = front.post("/auth/passkey/options", json={}, headers=JSON).json()
    body = {"challenge_id": opts["challenge_id"], **authn.get(opts["publicKey"])}
    clock.now += CHALLENGE_TTL + 1
    assert front.post("/auth/passkey/login", json=body, headers=JSON).status_code == 401


def test_an_enrollment_challenge_is_not_a_login_challenge(front, app):
    authn = Authenticator(NAME)
    code = app.state.enroll.mint()
    opts = front.post("/auth/enroll/options", json={"code": code}, headers=JSON).json()
    created = authn.create(opts["publicKey"])
    assert (
        front.post(
            "/auth/enroll/register",
            json={"challenge_id": opts["challenge_id"], "code": code, "name": "x", **created},
            headers=JSON,
        ).status_code
        == 200
    )
    opts2 = front.post("/auth/enroll/options", json={"code": app.state.enroll.mint()}, headers=JSON).json()
    got = authn.get(opts2["publicKey"])
    res = front.post("/auth/passkey/login", json={"challenge_id": opts2["challenge_id"], **got}, headers=JSON)
    assert res.status_code == 401 and code_of(res) == "passkey.invalid"


def test_every_failure_of_a_login_looks_the_same(front, app):
    authn, _ = enroll(front, app)
    stranger = Authenticator(NAME)  # a passkey the store never saw
    forged = Authenticator(NAME)
    forged.credential_id = authn.credential_id  # same id, another key: a bad signature

    def other_origin(body):
        pass

    answers = set()
    for who in (stranger, forged):
        res = login(front, who)
        answers.add((res.status_code, json.dumps(res.json(), sort_keys=True)))
    res = front.post(
        "/auth/passkey/login", json={"challenge_id": "A" * 22, **authn.get({"challenge": "A" * 43})}, headers=JSON
    )
    answers.add((res.status_code, json.dumps(res.json(), sort_keys=True)))
    assert len(answers) == 1 and next(iter(answers))[0] == 401


def test_an_assertion_for_another_origin_is_refused(front, app):
    authn, _ = enroll(front, app)
    opts = front.post("/auth/passkey/options", json={}, headers=JSON).json()
    got = authn.get(opts["publicKey"], origin="https://evil.example.com")
    res = front.post("/auth/passkey/login", json={"challenge_id": opts["challenge_id"], **got}, headers=JSON)
    assert res.status_code == 401 and code_of(res) == "passkey.invalid"


def test_a_passkey_without_user_verification_is_refused(front, app):
    authn, _ = enroll(front, app, authn=Authenticator(NAME, uv=False))
    # the registration already needs UV: nothing was stored, so the login finds nothing
    assert login(front, authn).status_code == 401


# --- the store fails closed ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "damage",
    [
        lambda p: os.chmod(p, 0o644),
        lambda p: os.chmod(p, 0o660),
        lambda p: p.write_text("not json"),
        lambda p: p.write_text('{"v":1,"passkeys":[],"x":1}'),
        lambda p: p.write_text('{"v":2,"passkeys":[]}'),
        lambda p: p.write_text('{"v":1,"passkeys":[{"credential_id":"x"}]}'),
        lambda p: p.write_text('{"v":1,"passkeys":[],"passkeys":[]}'),
        lambda p: os.chmod(p.parent, 0o770),
    ],
)
def test_a_store_that_cannot_be_trusted_means_no_login(front, app, passkey_store, damage):
    authn, _ = enroll(front, app)
    damage(passkey_store.path)
    res = login(front, authn)
    assert res.status_code == 401 and code_of(res) == "passkey.invalid"
    os.chmod(passkey_store.path.parent, 0o700)


def test_a_symlinked_store_is_refused(front, app, passkey_store, tmp_path):
    authn, _ = enroll(front, app)
    real = tmp_path / "elsewhere.json"
    os.replace(passkey_store.path, real)
    os.symlink(real, passkey_store.path)
    assert login(front, authn).status_code == 401


def test_enrollment_on_an_untrusted_store_is_refused(front, app, passkey_store):
    passkey_store.path.write_text("garbage")
    os.chmod(passkey_store.path, 0o600)
    res = front.post("/auth/enroll/options", json={"code": app.state.enroll.mint()}, headers=JSON)
    assert res.status_code == 401 and code_of(res) == "enroll.expired"


def test_the_store_keeps_no_duplicate_and_removes_by_id(front, app, passkey_store):
    authn, _ = enroll(front, app)
    (record,) = passkey_store.read()
    from orch.click.passkey_store import StoreError

    with pytest.raises(StoreError):
        passkey_store.add(record)
    assert passkey_store.remove(authn.credential_id) and passkey_store.read() == []
    assert not passkey_store.remove(authn.credential_id)


# --- rate limits and listeners ---------------------------------------------------------------------------------------
def test_login_is_limited_per_source_and_never_touches_the_launch_limiter(front, auth):
    for _ in range(20):
        assert front.post("/auth/passkey/options", json={}, headers=JSON).status_code == 200
    res = front.post("/auth/passkey/options", json={}, headers=JSON)
    assert res.status_code == 429 and code_of(res) == "rate.limited"
    other = front.post("/auth/passkey/options", json={}, headers={**JSON, "X-Orch-Client": "198.51.100.9"})
    assert other.status_code == 200  # another source has its own bucket
    assert len(auth.attempts) == 0  # the launch limiter is untouched


def test_the_routes_do_not_exist_on_loopback_and_read_no_client_address(loop, auth):
    headers = {"Origin": ORIGIN, "X-Orch-Client": SOURCE}
    for path in ("/auth/passkey/options", "/auth/passkey/login", "/auth/enroll/options", "/auth/enroll/register"):
        res = loop.post(path, json={}, headers=headers)
        assert res.status_code in (404, 400), path
    assert loop.post("/auth/passkey/options", json={}, headers=headers).status_code == 404
    assert auth.remote_sources == {} and not auth.remote_all  # no bucket was charged: the header was never read


def test_remote_access_off_means_no_passkey_routes(gateway, auth, tmp_path):
    from conftest import PORT
    from fastapi.testclient import TestClient

    from orch_dashboard.app import create_app

    static = tmp_path / "static"
    static.mkdir()
    off = TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN)
    assert off.post("/auth/passkey/options", json={}, headers={"Origin": ORIGIN}).status_code == 404


# --- session caps ---------------------------------------------------------------------------------------------------
def test_at_most_twenty_remote_sessions_live_and_the_oldest_goes_first(auth, clock):
    tokens = []
    for i in range(25):
        clock.now += 1
        tokens.append(auth.open_remote(f"203.0.113.{i + 1}")["token"])
    alive = [t for t in tokens if auth.valid(t, touch=False)]
    assert alive == tokens[-20:]
    assert len(auth.sessions) == len(auth.kinds) == len(auth.listeners) == len(auth.session_sources) == 20


def test_one_source_holds_at_most_five_sessions_and_cannot_push_out_another(auth, clock):
    other = auth.open_remote("198.51.100.1")["token"]
    mine = []
    for _ in range(8):
        clock.now += 1
        mine.append(auth.open_remote("203.0.113.7")["token"])
    assert [t for t in mine if auth.valid(t, touch=False)] == mine[-5:]
    assert auth.valid(other, touch=False)


def test_an_ipv6_source_is_its_slash_64(auth, clock):
    tokens = []
    for i in range(7):
        clock.now += 1
        tokens.append(auth.open_remote(f"2001:db8:0:1::{i + 1}")["token"])
    assert len([t for t in tokens if auth.valid(t, touch=False)]) == 5


def test_local_sessions_are_not_counted_or_evicted_by_remote_ones(auth, clock):
    local = auth.exchange(auth.launch(43210)["code"], "local")[0]["token"]
    for i in range(30):
        clock.now += 1
        auth.open_remote(f"203.0.113.{i + 1}")
    assert auth.valid(local, touch=False)


def test_login_records_the_source_of_the_session(front, app, auth):
    authn, res = enroll(front, app)
    token = res.json()["token"]
    assert auth.session_sources[token] == SOURCE
    assert login(front, authn, source="198.51.100.4").status_code == 200


def test_expired_and_logged_out_sessions_leave_no_state(auth, clock):
    token = auth.open_remote("203.0.113.9")["token"]
    auth.logout(token)
    assert auth.session_sources == {} and auth.sessions == {}
    token = auth.open_remote("203.0.113.9")["token"]
    clock.now += 43201
    assert not auth.valid(token)
    assert auth.session_sources == {}


def test_a_sign_count_that_does_not_rise_logs_a_warning_keeps_the_higher_value_and_logs_in(
    front, app, passkey_store, caplog
):
    authn, _ = enroll(front, app)
    assert login(front, authn).status_code == 200 and login(front, authn).status_code == 200
    assert passkey_store.read()[0].sign_count == 2
    authn.count = 0  # a clone starts again at 1
    with caplog.at_level("WARNING", logger="orch_dashboard.passkey"):
        assert login(front, authn).status_code == 200
    assert "possible cloned passkey" in caplog.text
    assert passkey_store.read()[0].sign_count == 2

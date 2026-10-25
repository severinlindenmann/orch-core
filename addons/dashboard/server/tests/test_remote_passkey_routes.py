"""#1198 R1-5: passkey login and enrollment on the remote listener, the passkey store, enrollment codes."""

# ruff: noqa: F811

import json
import os
import stat

import pytest
from conftest import ORIGIN
from remote_kit import (  # noqa: F401
    HTTPS,
    NAME,
    SOURCE,
    Clock,
    app,
    bearer,
    front,
    loop,
    passkey_store,
    remote,
)
from webauthn_kit import Authenticator

from orch_dashboard.auth import Auth
from orch_dashboard.passkey_routes import CHALLENGE_TTL, ENROLL_TRIES, ENROLL_TTL

JSON = {"Origin": HTTPS}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def auth(clock):
    return Auth(clock=clock)


def enroll(front, app, *, code=None, name="MacBook, Chrome", authn=None, source=None):
    """A full enrollment with a software authenticator; returns (authenticator, register response)."""
    code = code or app.state.enroll.mint()
    headers = {**JSON, **({"X-Orch-Client": source} if source else {})}
    opts = front.post("/auth/enroll/options", json={"code": code}, headers=headers)
    if opts.status_code != 200:
        return None, opts
    authn = authn or Authenticator(NAME)
    created = authn.create(opts.json()["publicKey"])
    res = front.post(
        "/auth/enroll/register",
        json={"challenge_id": opts.json()["challenge_id"], "code": code, "name": name, **created},
        headers=headers,
    )
    return authn, res


def login(front, authn, *, mutate=None, source=None):
    headers = {**JSON, **({"X-Orch-Client": source} if source else {})}
    opts = front.post("/auth/passkey/options", json={}, headers=headers)
    assert opts.status_code == 200, opts.text
    got = authn.get(opts.json()["publicKey"])
    body = {"challenge_id": opts.json()["challenge_id"], **got}
    if mutate:
        mutate(body)
    return front.post("/auth/passkey/login", json=body, headers=headers)


def code_of(res):
    return res.json()["error"]["code"]


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
    from orch_dashboard.passkey_store import StoreError

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

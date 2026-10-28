"""M2-6: the person's own account security (change password, new authenticator, sign out everywhere). Credentials are
security-critical: every condition of the task has a test here. Real parts (see webapi_helpers); the activity log is real."""

from __future__ import annotations

import base64
import json
import logging

import pytest
from channels_helpers import PW
from webapi_helpers import Web

from orch_hub.activity import ActivityLog
from orch_hub.channels.service import PENDING_TOTP_MS, totp_code

NEW_PW = "a completely new passphrase"


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    x.api.d.activity = ActivityLog(tmp_path / "activity.db", now_ms=x.clock)
    yield x
    x.api.d.activity.close()
    x.close()


def events(w, kind=None):
    return [e for e in w.api.d.activity.list() if kind in (None, e["type"])]


def pw_body(w, user, **kw):
    return {"current_password": PW, "totp": w.env.next_code(user), "new_password": NEW_PW, **kw}


def secret_of(offer) -> bytes:
    s = offer["secret"]
    return base64.b32decode(s + "=" * (-len(s) % 8))


def new_code(w, secret: bytes) -> str:
    return totp_code(secret, w.clock() // 30_000)


def sign_in_works(w, user, password=PW) -> bool:
    s, _, _ = w.call(
        "POST",
        "/session",
        {"user_id": user, "password": password, "totp": w.env.next_code(user), "client": "app"},
        origin=None,
    )
    return s == 200


def alive(w, token) -> bool:
    return w.call("GET", "/session", token=token)[0] == 200


def begin(w, user, token, **kw):
    return w.call("POST", "/me/authenticator/begin", {**w.creds(user), **kw}, token=token)


# --- change password ----------------------------------------------------------------------------------------------------


def test_wrong_current_password_is_refused_and_counted(w):
    tok = w.login(w.ann)
    s, o, _ = w.call("POST", "/me/password", pw_body(w, w.ann, current_password="not the password"), token=tok)
    assert (s, o) == (401, {"error": "bad_credentials"})
    assert w.env.svc.db.q("SELECT COUNT(*) FROM failures WHERE kind='pw'")[0][0] == 1
    assert sign_in_works(w, w.ann)  # nothing changed
    assert not events(w, "password.changed")


def test_wrong_code_is_refused_and_counted_by_the_lockout(w):
    tok = w.login(w.ann)
    s, o, _ = w.call("POST", "/me/password", pw_body(w, w.ann, totp="000000"), token=tok)
    assert (s, o) == (401, {"error": "bad_credentials"})
    assert w.env.svc.db.q("SELECT COUNT(*) FROM failures WHERE kind='totp'")[0][0] == 1
    assert sign_in_works(w, w.ann)


def test_repeated_failures_lock_exactly_as_sign_in_does(w):
    tok = w.login(w.ann)
    for _ in range(5):
        s, o, _ = w.call("POST", "/me/password", pw_body(w, w.ann, current_password="wrong wrong wrong"), token=tok)
        assert (s, o) == (401, {"error": "bad_credentials"})
    s, o, _ = w.call("POST", "/me/password", pw_body(w, w.ann), token=tok)  # now even the right pair is refused
    assert (s, o) == (429, {"error": "rate_limited"})
    assert sign_in_works(w, w.ann) is False  # the same limiter state guards sign-in


def test_weak_and_unchanged_passwords_are_refused_without_spending_anything(w):
    tok = w.login(w.ann)
    s, o, _ = w.call("POST", "/me/password", pw_body(w, w.ann, new_password="short"), token=tok)
    assert (s, o) == (400, {"error": "weak_password"})
    s, o, _ = w.call("POST", "/me/password", pw_body(w, w.ann, new_password=PW), token=tok)
    assert (s, o) == (400, {"error": "same_password"})
    assert w.call("POST", "/me/password", {"totp": "1"}, token=tok)[:2] == (400, {"error": "malformed"})
    assert w.env.svc.db.q("SELECT COUNT(*) FROM failures")[0][0] == 0
    assert sign_in_works(w, w.ann)
    assert not events(w, "password.changed")


def test_change_password_ends_other_sessions_keeps_this_one_and_the_channel_keys(w):
    tok, other, other2 = w.login(w.ann), w.login(w.ann), w.login(w.ann)
    pairing = w.login(w.ann, "app")
    _, cid = w.register_channel(tok)
    assert w.env.svc.channel_state(w.ann, cid) == "active"
    s, o, _ = w.call("POST", "/me/password", pw_body(w, w.ann), token=tok)
    assert (s, o) == (200, {"ok": True})
    assert alive(w, tok)
    assert not any(alive(w, t) for t in (other, other2, pairing))
    assert w.env.db_count("sessions", w.ann) == 1
    assert w.env.svc.channel_state(w.ann, cid) == "active"  # the channel key is untouched
    assert [c["status"] for c in w.env.svc.channels(w.ann)] == ["active"]
    assert sign_in_works(w, w.ann) is False  # the old password is dead
    assert sign_in_works(w, w.ann, NEW_PW)
    rows = events(w, "password.changed")
    assert len(rows) == 1 and rows[0]["actor_id"] == w.ann and rows[0]["subject_user"] == w.ann


def test_other_users_sessions_are_not_touched(w):
    tok, bob = w.login(w.ann), w.login(w.admin)
    assert w.call("POST", "/me/password", pw_body(w, w.ann), token=tok)[0] == 200
    assert alive(w, bob)


def test_the_page_acts_only_for_the_signed_in_person(w):
    tok = w.login(w.admin)
    body = pw_body(w, w.admin, user_id=w.ann)  # a named other user is ignored: there is no such parameter
    assert w.call("POST", "/me/password", body, token=tok)[0] == 200
    assert sign_in_works(w, w.ann)  # ann's password is as it was
    assert sign_in_works(w, w.admin, NEW_PW)


# --- new authenticator ----------------------------------------------------------------------------------------------------


def test_begin_needs_the_current_password_and_code_and_counts_failures(w):
    tok = w.login(w.ann)
    s, o, _ = begin(w, w.ann, tok, password="nope nope nope")
    assert (s, o) == (401, {"error": "bad_credentials"})
    s, o, _ = begin(w, w.ann, tok, totp="000000")
    assert (s, o) == (401, {"error": "bad_credentials"})
    q = w.env.svc.db.q
    assert q("SELECT COUNT(*) FROM failures WHERE kind='pw'")[0][0] == 1
    assert q("SELECT COUNT(*) FROM failures WHERE kind='totp'")[0][0] == 1
    assert q("SELECT COUNT(*) FROM pending_totp")[0][0] == 0
    for _ in range(5):
        begin(w, w.ann, tok, password="nope nope nope")
    assert begin(w, w.ann, tok)[:2] == (429, {"error": "rate_limited"})


def test_new_secret_is_inactive_until_a_valid_code_from_it_and_the_old_one_works_until_then(w):
    tok = w.login(w.ann)
    s, offer, _ = begin(w, w.ann, tok)
    assert s == 200 and set(offer) == {"secret", "uri"} and offer["secret"] in offer["uri"]
    new = secret_of(offer)
    # the NEW secret cannot sign in yet, the OLD one still can
    w.clock.advance(30_000)
    s, _, _ = w.call(
        "POST", "/session", {"user_id": w.ann, "password": PW, "totp": new_code(w, new), "client": "app"}, origin=None
    )
    assert s == 401
    assert sign_in_works(w, w.ann)
    assert alive(w, tok)


def test_wrong_confirm_code_leaves_the_old_secret_active_and_is_counted(w):
    tok = w.login(w.ann)
    begin(w, w.ann, tok)
    old_code = w.env.next_code(w.ann)  # a valid code, but from the OLD secret
    assert w.call("POST", "/me/authenticator/confirm", {"code": old_code}, token=tok)[:2] == (
        400,
        {"error": "bad_code"},
    )
    assert w.call("POST", "/me/authenticator/confirm", {"code": "12345"}, token=tok)[:2] == (400, {"error": "bad_code"})
    assert w.env.svc.db.q("SELECT COUNT(*) FROM failures WHERE kind='totp-confirm'")[0][0] == 2
    assert sign_in_works(w, w.ann)  # the old secret is still the one
    assert w.env.svc.db.q("SELECT COUNT(*) FROM pending_totp")[0][0] == 1  # and the pending one is still waiting
    assert not events(w, "authenticator.rotated")


def test_confirm_guesses_are_limited(w):
    tok = w.login(w.ann)
    _, offer, _ = begin(w, w.ann, tok)
    for _ in range(5):
        assert w.call("POST", "/me/authenticator/confirm", {"code": "000000"}, token=tok)[0] == 400
    good = new_code(w, secret_of(offer))
    assert w.call("POST", "/me/authenticator/confirm", {"code": good}, token=tok)[:2] == (
        429,
        {"error": "rate_limited"},
    )
    assert sign_in_works(w, w.ann)  # still the old secret


def test_confirm_activates_the_new_secret_kills_the_old_and_other_sessions(w):
    tok, other = w.login(w.ann), w.login(w.ann)
    _, cid = w.register_channel(tok)
    _, offer, _ = begin(w, w.ann, tok)
    new = secret_of(offer)
    s, o, _ = w.call("POST", "/me/authenticator/confirm", {"code": new_code(w, new)}, token=tok)
    assert (s, o) == (200, {"ok": True})
    assert alive(w, tok) and not alive(w, other)
    assert w.env.svc.channel_state(w.ann, cid) == "active"
    # the old secret is dead, the new one signs in
    assert sign_in_works(w, w.ann) is False
    w.env.secrets[w.ann] = new
    assert sign_in_works(w, w.ann)
    assert w.env.svc.db.q("SELECT COUNT(*) FROM pending_totp")[0][0] == 0
    assert len(events(w, "authenticator.rotated")) == 1
    # the confirm code is spent: it cannot be replayed at sign-in
    s, _, _ = w.call(
        "POST", "/session", {"user_id": w.ann, "password": PW, "totp": new_code(w, new), "client": "app"}, origin=None
    )
    assert s == 401


def test_the_confirm_code_cannot_be_replayed_in_the_same_step(w):
    tok = w.login(w.ann)
    _, offer, _ = begin(w, w.ann, tok)
    new = secret_of(offer)
    code = new_code(w, new)
    assert w.call("POST", "/me/authenticator/confirm", {"code": code}, token=tok)[0] == 200
    w.env.secrets[w.ann] = new
    s, _, _ = w.call("POST", "/session", {"user_id": w.ann, "password": PW, "totp": code, "client": "app"}, origin=None)
    assert s == 401


def test_pending_secret_expires_after_ten_minutes(w):
    tok = w.login(w.ann)
    _, offer, _ = begin(w, w.ann, tok)
    new = secret_of(offer)
    w.clock.advance(PENDING_TOTP_MS - 31_000)  # still inside the 10 minutes: a code from it would work
    assert PENDING_TOTP_MS == 600_000
    w.clock.advance(31_000)  # now it is over
    s, o, _ = w.call("POST", "/me/authenticator/confirm", {"code": new_code(w, new)}, token=tok)
    assert (s, o) == (410, {"error": "pending_expired"})
    assert w.call("POST", "/me/authenticator/confirm", {"code": new_code(w, new)}, token=tok)[:2] == (
        409,
        {"error": "no_pending"},
    )
    assert sign_in_works(w, w.ann)  # the old secret is untouched
    assert not events(w, "authenticator.rotated")


def test_pending_secret_still_works_just_inside_ten_minutes(w):
    tok = w.login(w.ann)
    _, offer, _ = begin(w, w.ann, tok)
    new = secret_of(offer)
    w.clock.advance(PENDING_TOTP_MS - 1)
    assert w.call("POST", "/me/authenticator/confirm", {"code": new_code(w, new)}, token=tok)[0] == 200


def test_one_pending_secret_per_user_a_new_begin_replaces_it(w):
    tok = w.login(w.ann)
    _, first, _ = begin(w, w.ann, tok)
    _, second, _ = begin(w, w.ann, tok)
    assert first["secret"] != second["secret"]
    assert w.env.svc.db.q("SELECT COUNT(*) FROM pending_totp WHERE user_id=?", (w.ann,))[0][0] == 1
    w.clock.advance(30_000)
    assert w.call("POST", "/me/authenticator/confirm", {"code": new_code(w, secret_of(first))}, token=tok)[0] == 400
    assert w.call("POST", "/me/authenticator/confirm", {"code": new_code(w, secret_of(second))}, token=tok)[0] == 200


def test_confirm_without_a_pending_secret_changes_nothing(w):
    tok = w.login(w.ann)
    assert w.call("POST", "/me/authenticator/confirm", {"code": w.env.next_code(w.ann)}, token=tok)[:2] == (
        409,
        {"error": "no_pending"},
    )
    assert sign_in_works(w, w.ann)


def test_pending_secrets_are_per_user(w):
    tok, adm = w.login(w.ann), w.login(w.admin)
    _, offer, _ = begin(w, w.ann, tok)
    w.clock.advance(30_000)
    code = new_code(w, secret_of(offer))
    assert w.call("POST", "/me/authenticator/confirm", {"code": code}, token=adm)[:2] == (409, {"error": "no_pending"})
    assert sign_in_works(w, w.admin)


def test_an_operator_reset_voids_a_pending_secret(w):
    tok = w.login(w.ann)
    begin(w, w.ann, tok)
    w.env.svc.new_totp(w.ann)
    assert w.env.svc.db.q("SELECT COUNT(*) FROM pending_totp")[0][0] == 0


def test_the_secret_is_shown_once_and_never_logged_or_recorded(w, caplog):
    caplog.set_level(logging.DEBUG)
    tok = w.login(w.ann)
    _, offer, _ = begin(w, w.ann, tok)
    new = secret_of(offer)
    assert w.call("POST", "/me/authenticator/confirm", {"code": new_code(w, new)}, token=tok)[0] == 200
    w.env.secrets[w.ann] = new
    assert w.call("POST", "/me/password", pw_body(w, w.ann), token=tok)[0] == 200
    assert w.call("POST", "/me/sessions/end-all", {}, token=tok)[0] == 200
    dump = json.dumps(events(w))
    for secret in (offer["secret"], offer["uri"], NEW_PW, PW):
        assert secret not in dump
        assert secret not in caplog.text
    for e in events(w):
        assert set(e["detail"]) <= {"count"}
    assert [e["type"] for e in events(w)] == ["sessions.ended", "password.changed", "authenticator.rotated"]


def test_the_secret_is_not_in_the_state_of_the_session_or_whoami(w):
    tok = w.login(w.ann)
    _, offer, _ = begin(w, w.ann, tok)
    s, me, _ = w.call("GET", "/session", token=tok)
    assert s == 200 and offer["secret"] not in json.dumps(me)
    assert w.call("GET", "/me/channels", token=tok)[0] == 200


# --- sign out everywhere --------------------------------------------------------------------------------------------------


def test_end_all_kills_every_session_including_this_one_and_keeps_channel_keys(w):
    tok, other = w.login(w.ann), w.login(w.ann)
    pairing = w.login(w.ann, "app")
    bystander = w.login(w.admin)
    _, cid = w.register_channel(tok)
    s, o, h = w.call("POST", "/me/sessions/end-all", {}, token=tok)
    assert (s, o) == (200, {"ok": True}) and "Max-Age=0" in h["Set-Cookie"]
    assert not any(alive(w, t) for t in (tok, other, pairing))
    assert w.env.db_count("sessions", w.ann) == 0
    assert alive(w, bystander)
    assert w.env.svc.channel_state(w.ann, cid) == "active"  # the key stays
    assert sign_in_works(w, w.ann)  # the person can sign in again with the same password and authenticator
    rows = events(w, "sessions.ended")
    assert len(rows) == 1 and rows[0]["actor_id"] == w.ann and rows[0]["detail"] == {"count": 3}


# --- who may call -------------------------------------------------------------------------------------------------------


def switch_session(w, user):
    tok = w.env.svc.open_session(user)["token"]
    w.browser_tokens.add(tok)
    assert w.env.svc.session_info(tok)["via"] == "switch"
    return tok


def test_a_switch_ticket_session_is_refused_on_all_three_and_changes_nothing(w):
    sw = switch_session(w, w.ann)
    real = w.login(w.ann)
    calls = [
        ("/me/password", pw_body(w, w.ann)),
        ("/me/authenticator/begin", w.creds(w.ann)),
        ("/me/authenticator/confirm", {"code": "123456"}),
        ("/me/sessions/end-all", {}),
    ]
    for path, body in calls:
        s, o, _ = w.call("POST", path, body, token=sw)
        assert (s, o) == (403, {"error": "reauth_required"}), path
    assert alive(w, sw) and alive(w, real)
    assert w.env.svc.db.q("SELECT COUNT(*) FROM pending_totp")[0][0] == 0
    assert w.env.svc.db.q("SELECT COUNT(*) FROM failures")[0][0] == 0  # refused before any credential was looked at
    assert sign_in_works(w, w.ann)
    assert not events(w)


def test_pairing_and_missing_sessions_are_refused(w):
    app = w.login(w.ann, "app")
    for path in ("/me/password", "/me/authenticator/begin", "/me/authenticator/confirm", "/me/sessions/end-all"):
        assert w.call("POST", path, {}, token=app)[0] == 403, path
        assert w.call("POST", path, {})[:2] == (401, {"error": "no_session"}), path
    # a console session sent as a bearer is not a session for these either
    full = w.login(w.ann)
    assert w.call("POST", "/me/sessions/end-all", {}, token=full, cookie=False)[0] == 401
    assert alive(w, full)


def test_forged_requests_are_refused(w):
    tok = w.login(w.ann)
    for kw in ({"csrf": False}, {"origin": "https://evil.example"}, {"ctype": "text/plain"}):
        s, o, _ = w.call("POST", "/me/sessions/end-all", {}, token=tok, **kw)
        assert (s, o) == (403, {"error": "not_allowed"})
    assert alive(w, tok)

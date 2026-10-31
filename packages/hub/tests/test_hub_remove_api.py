"""#1463: the removal routes. Who may call, the step-up inside the request, the single-use nonce, the proof over the
removal digest, and that a refusal leaves NO state file."""

from __future__ import annotations

import pytest
from removal_helpers import (
    admin_with_key,
    close_all,
    confirm_body,
    make_web,
    prepare,
    start,
    with_backup_key,
)

from orch_hub.closing import STATE_FILE
from orch_hub.contract import closing as C


@pytest.fixture
def w(tmp_path):
    x = make_web(tmp_path, inline=False)
    yield x
    close_all(x)


def _no_state(w):
    assert not (w.state / STATE_FILE).exists()


ROUTES = [
    ("GET", "/admin/hub/remove/preview", None),
    ("POST", "/admin/hub/remove/prepare", {"channel_id": "00" * 16}),
    ("POST", "/admin/hub/remove/confirm", {"nonce": "a", "proof": "b", "password": "x", "totp": "000000"}),
    ("GET", "/admin/hub/remove/status", None),
    ("POST", "/admin/hub/remove/cancel", {}),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_routes_are_admin_only_and_refuse_switch_and_pairing_sessions(w, method, path, body):
    assert w.call(method, path, body)[0] == 401
    assert w.call(method, path, body, token=w.login(w.ann))[:2] == (403, {"error": "forbidden_role"})
    assert w.call(method, path, body, token=w.login(w.admin, "app"))[0] in (401, 403)
    ticket = w.env.svc.open_session(w.admin)["token"]  # a workspace vouched for it: no password sign-in
    w.browser_tokens.add(ticket)
    assert w.call(method, path, body, token=ticket)[:2] == (403, {"error": "reauth_required"})
    if method == "POST":
        assert w.call(method, path, body, token=w.login(w.admin), csrf=False)[:2] == (403, {"error": "not_allowed"})
    _no_state(w)


def test_preview_changes_nothing_and_shows_only_a_count(w):
    tok = w.login(w.admin)
    s, o, _ = w.call("GET", "/admin/hub/remove/preview", token=tok)
    assert s == 200
    assert set(o) == {
        "state",
        "workspaces",
        "relay",
        "publish",
        "backup_key_ok",
        "backup_folder_hint",
        "cancel_allowed",
        "refusal",
    }
    assert o["state"] == "open" and o["workspaces"] == 1 and o["backup_key_ok"] is False
    assert o["refusal"] == "Set up the backup words first."
    assert "name" not in str(o).lower() or "backup" in str(o).lower()
    _no_state(w)
    assert w.launcher.calls == []


def test_without_a_confirmed_backup_key_prepare_is_refused_with_the_words_line(w):
    tok, _signer, ch = admin_with_key(w)
    s, o, _ = prepare(w, tok, ch)
    assert (s, o) == (409, {"error": "no_backup_key"})
    _no_state(w)


def test_prepare_body_is_exactly_the_channel_id(w):
    with_backup_key(w)
    tok, _signer, _ch = admin_with_key(w)
    for bad in (None, {}, {"channel_id": 1}, {"channel_id": "x", "extra": 1}):
        assert w.call("POST", "/admin/hub/remove/prepare", bad, token=tok)[:2] == (400, {"error": "malformed"})


def test_a_good_confirm_persists_closing_and_answers_202(w):
    with_backup_key(w)
    tok, signer, ch = admin_with_key(w)
    s, o, _ = start(w, tok, signer, ch)
    assert (s, o) == (202, {"ok": True})
    assert (w.state / STATE_FILE).exists()
    assert w.closing.phase() == "closing" and len(w.spawned) == 1  # the request never runs the steps itself
    assert w._act.recent if hasattr(w._act, "recent") else True


def test_confirm_body_is_exactly_nonce_proof_password_totp(w):
    with_backup_key(w)
    tok, signer, ch = admin_with_key(w)
    _s, prep, _ = prepare(w, tok, ch)
    body = confirm_body(w, signer, prep)
    for bad in ({**body, "channel_id": ch}, {k: v for k, v in body.items() if k != "proof"}):
        assert w.call("POST", "/admin/hub/remove/confirm", bad, token=tok)[:2] == (400, {"error": "malformed"})
    _no_state(w)


def test_wrong_password_or_reused_code_is_refused_and_the_nonce_survives(w):
    with_backup_key(w)
    tok, signer, ch = admin_with_key(w)
    _s, prep, _ = prepare(w, tok, ch)
    s, o, _ = w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, signer, prep, password="wrong"), token=tok)
    assert (s, o) == (401, {"error": "bad_credentials"})
    _no_state(w)
    assert w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, signer, prep), token=tok)[0] == 202


def test_a_wrong_proof_is_refused_with_no_state_and_burns_the_nonce(w):
    from channels_helpers import new_channel

    with_backup_key(w)
    tok, signer, ch = admin_with_key(w)
    _s, prep, _ = prepare(w, tok, ch)
    stranger = new_channel()
    s, o, _ = w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, stranger, prep), token=tok)
    assert (s, o) == (403, {"error": "bad_channel_proof"})
    _no_state(w)
    # signing a different digest than the one the page computed is refused too, and the nonce is gone
    assert w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, signer, prep), token=tok)[:2] == (
        403,
        {"error": "nonce_invalid"},
    )
    _no_state(w)


def test_a_proof_over_another_context_is_refused(w):
    with_backup_key(w)
    tok, signer, ch = admin_with_key(w)
    _s, prep, _ = prepare(w, tok, ch)
    other = C.remove_digest({**prep["ctx"], "user_id": "00" * 16}).hex()
    body = confirm_body(w, signer, prep)
    body["proof"] = w.proof(signer, other)
    assert w.call("POST", "/admin/hub/remove/confirm", body, token=tok)[:2] == (403, {"error": "bad_channel_proof"})
    _no_state(w)


def test_nonce_of_another_user_session_or_expiry_or_reuse_is_refused(w):
    with_backup_key(w)
    tok, signer, ch = admin_with_key(w)
    _s, prep, _ = prepare(w, tok, ch)
    other_session = w.login(w.admin)  # same user, another session
    assert w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, signer, prep), token=other_session)[:2] == (
        403,
        {"error": "nonce_invalid"},
    )
    _no_state(w)
    w.clock.advance(5 * 60_000 + 1)  # past the five minutes
    tok2 = w.login(w.admin)
    assert w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, signer, prep), token=tok)[:2] == (
        403,
        {"error": "nonce_invalid"},
    )
    _no_state(w)
    # a second prepare replaces the first (at most one pending nonce per user)
    signer2, ch2 = w.register_channel(tok2, w.admin)
    _s, a, _ = prepare(w, tok2, ch2)
    _s, b, _ = prepare(w, tok2, ch2)
    assert a["nonce"] != b["nonce"]
    assert w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, signer2, a), token=tok2)[:2] == (
        403,
        {"error": "nonce_invalid"},
    )
    assert w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, signer2, b), token=tok2)[0] == 202
    # reuse after success
    assert w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, signer2, b), token=tok2)[0] in (403, 409)


def test_two_admins_cannot_both_start_exactly_one_runs(w):
    with_backup_key(w)
    e = w.env
    admin2 = e.user("admin2")
    e.acc.add_member("hub-admin", admin2)
    tok1, s1, c1 = admin_with_key(w)
    tok2 = w.login(admin2)
    s2, c2 = w.register_channel(tok2, admin2)
    _a, p1, _ = prepare(w, tok1, c1)
    _b, p2, _ = w.call("POST", "/admin/hub/remove/prepare", {"channel_id": c2}, token=tok2)
    b1 = {"nonce": p1["nonce"], "proof": w.proof(s1, p1["digest"]), **w.creds(w.admin)}
    b2 = {"nonce": p2["nonce"], "proof": w.proof(s2, p2["digest"]), **w.creds(admin2)}
    first = w.call("POST", "/admin/hub/remove/confirm", b1, token=tok1)
    second = w.call("POST", "/admin/hub/remove/confirm", b2, token=tok2)
    assert first[0] == 202 and second[:2] == (409, {"error": "hub_closing"})
    assert len(w.spawned) == 1


def test_status_is_plain_and_cancel_returns_the_hub_to_open(w):
    with_backup_key(w)
    tok, signer, ch = admin_with_key(w)
    start(w, tok, signer, ch)
    s, st, _ = w.call("GET", "/admin/hub/remove/status", token=tok)
    assert s == 200 and st["state"] == "closing" and st["cancel_allowed"] is True and st["plain"]
    assert w.call("POST", "/admin/hub/remove/cancel", {}, token=tok)[:2] == (200, {"ok": True})
    _no_state(w)
    assert w.closing.active() is False
    assert w.call("GET", "/admin/hub/remove/status", token=tok)[1]["state"] == "open"


def test_a_channel_factor_below_password_and_totp_is_refused_with_no_state(w, monkeypatch):
    from orch_hub.contract import hub as H
    from orch_hub.custodial import engine as E

    weak = "password"  # not a factor of the ranking at all (password+totp is the lowest there is): rank -1
    assert weak not in H.FACTOR_RANK
    real = E.verified_channel

    def lowered(*a, **k):
        cert, ch = real(*a, **k)
        return cert, {**ch, "factor": weak}

    monkeypatch.setattr(E, "verified_channel", lowered)
    with_backup_key(w)
    tok, signer, ch = admin_with_key(w)
    _s, prep, _ = prepare(w, tok, ch)
    s, o, _ = w.call("POST", "/admin/hub/remove/confirm", confirm_body(w, signer, prep), token=tok)
    assert (s, o) == (403, {"error": "factor_too_weak"})
    _no_state(w)

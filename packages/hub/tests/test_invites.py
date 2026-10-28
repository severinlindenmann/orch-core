"""M2-5 (#1399 #1401): invitation links (join, reset, recovery), disable / enable; the binding conditions 1-5.

1. a reset for a person who still has a live channel needs a proof from that channel (test_reset_*);
2. no route creates a person and credentials for an admin to read out (test_post_admin_users_is_gone, test_join_*);
3. the token is a 256-bit secret, stored only hashed, single use under concurrency, one generic error (test_token_*,
   test_single_use_*, test_generic_error_*);
4. no group on join: the new person has no role anywhere (test_join_*);
5. disabling takes effect everywhere at once (test_disable_*, test_enable_*)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import urllib.error
import urllib.request

import pytest
from channels_helpers import new_channel
from custodial_helpers import ACTION
from webapi_helpers import ORIGIN, WS, Web

from orch_hub.access import AccessError
from orch_hub.activity import ActivityLog
from orch_hub.channels import service as CS
from orch_hub.channels.service import totp_code
from orch_hub.contract.canon import b64u
from orch_hub.invites import INVITE_TTL_MS, InviteError, Invites, proof_digest, token_hash
from orch_hub.webapi import WebServer
from orch_hub.webapi.api import ROUTES

BAD = (403, {"error": "bad_invite"})
NEW_PW = "a brand new password"


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    store = Invites(str(tmp_path / "hub" / "invites.db"), now_ms=x.clock)
    log = ActivityLog(tmp_path / "activity.db", now_ms=x.clock)
    x.api.d.user_invites, x.api.d.activity, x.tmp = store, log, tmp_path
    yield x
    store.close()
    log.close()
    x.close()


def token_of(link: str) -> str:
    assert "#invite=" in link and "?" not in link
    return link.split("#invite=", 1)[1]


def invite_join(w, name="dora") -> str:
    s, o, _ = w.call("POST", "/admin/invites", {"name": name}, token=w.login(w.admin))
    assert s == 200, o
    return token_of(o["link"])


def issue(w, user, recovery=False) -> str:
    s, o, _ = w.call("POST", f"/admin/users/{user}/reset", {"recovery": recovery}, token=w.login(w.admin))
    assert s == 200, o
    return token_of(o["link"])


def peek(w, token, **kw):
    return w.call("POST", "/invite/peek", {"token": token}, **kw)


def redeem(w, token, password=NEW_PW, proof=None, **kw):
    body = {"token": token, "password": password}
    if proof is not None:
        body["proof"] = proof
    return w.call("POST", "/invite/redeem", body, **kw)


def code_of(secret_b32: str, clock) -> str:
    raw = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    return totp_code(raw, clock() // 30_000)


def sign_in_as(w, who, password, secret):
    w.clock.advance(30_000)
    return w.call(
        "POST",
        "/session",
        {"user_id": who, "password": password, "totp": code_of(secret, w.clock), "client": "browser"},
    )


def make_proof(signer, challenge, token, channel_id=None):
    digest = proof_digest(challenge, token_hash(token))
    return {
        "channel_id": channel_id or signer.channel_id,
        "challenge": challenge,
        "sig": b64u(signer.proof(digest)),
    }


def with_channel(w, user):
    signer, cid = w.register_channel(w.login(user))
    assert signer.channel_id == cid
    return signer, cid


def db_bytes(path) -> bytes:
    out = b""
    for suffix in ("", "-wal", "-journal"):
        p = str(path) + suffix
        if os.path.exists(p):
            out += open(p, "rb").read()
    return out


# --- condition 3: the token ---------------------------------------------------------------------------------------------


def test_token_is_256_bits_and_stored_only_as_a_hash(w):
    tok = invite_join(w)
    assert len(base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4))) == 32
    inv = w.api.d.user_invites
    assert db_bytes(w.tmp / "hub" / "invites.db").count(tok.encode()) == 0
    (row,) = inv._db.execute("SELECT token_hash FROM invites").fetchall()
    assert row[0] == hashlib.sha256(tok.encode()).hexdigest()
    assert not any(tok in str(v) for r in inv._db.execute("SELECT * FROM invites") for v in r)
    # not in the activity log nor the audit chain either, before and after the link is used
    assert redeem(w, tok)[0] == 200
    assert db_bytes(w.tmp / "activity.db").count(tok.encode()) == 0
    assert tok not in json.dumps(w.api.d.activity.list(limit=200))
    assert tok not in json.dumps([(s, r, e) for s, r, e in w.env.audit.store.after(0, 1000)])


def test_the_proof_digest_matches_the_page(w):
    # the same vector as tests/js/invite.test.mjs: the page and the hub compute the same 32 bytes
    d = proof_digest(b64u(bytes(range(32))), token_hash("T" * 43))
    assert d.hex() == "63813eea3b1695c1229a2e8c3e0d8bb1931d3dd515851e62369057c4dbbac73b"
    assert (
        d == hashlib.sha256(b"orch/v2/invite-proof|" + bytes(range(32)) + hashlib.sha256(b"T" * 43).digest()).digest()
    )


def test_the_token_is_never_in_a_path_or_a_query(w):
    assert not [r for r in ROUTES if "token" in r.path]
    tok = invite_join(w)
    assert w.call("POST", f"/invite/peek/{tok}", {"token": tok})[:2] == (404, {"error": "unknown_op"})
    assert w.call("GET", f"/invite/peek?token={tok}")[:2] == (404, {"error": "unknown_op"})
    # a token in the query string of the real route is ignored: the body is the only carrier
    assert w.call("POST", f"/invite/peek?token={tok}", {"token": "x" * 43})[:2] == BAD
    assert peek(w, tok)[0] == 200


def test_single_use_under_concurrent_redeems_at_the_store(tmp_path):
    inv = Invites(str(tmp_path / "invites.db"))
    token, _, _ = inv.create("join", created_by="a" * 32, name="x")
    wins, lost, barrier = [], [], threading.Barrier(16)

    def go():
        barrier.wait()
        try:
            inv.redeem(token, lambda row: wins.append(row.id) or row.id)
        except InviteError as e:
            lost.append(e.code)

    ts = [threading.Thread(target=go) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1 and lost == ["bad_invite"] * 15
    inv.close()


def test_single_use_under_concurrent_redeems_over_http(w):
    tok = invite_join(w, "race")
    got, barrier = [], threading.Barrier(4)

    def go(i):
        barrier.wait()
        got.append(redeem(w, tok, peer=f"203.0.113.{i + 1}")[:2])

    ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(s for s, _ in got) == [200, 403, 403, 403], got
    assert [o for s, o in got if s == 403] == [BAD[1]] * 3
    assert [u["name"] for u in w.env.acc.users()].count("race") == 1  # exactly one account came out of it


def test_a_failed_account_change_gives_the_claim_back(w, monkeypatch):
    tok = invite_join(w)
    ukdir, certs = w.env.keys.state / "uk", w.env.keys._gen_dir() / "uk"
    before = set(os.listdir(ukdir)) if ukdir.exists() else set()
    certs_before = set(os.listdir(certs)) if certs.exists() else set()
    monkeypatch.setattr(w.api.d.access, "add_user", lambda *a, **k: (_ for _ in ()).throw(AccessError("store_error")))
    s, _o, _ = redeem(w, tok)
    assert s == 503
    assert set(os.listdir(ukdir)) == before  # no orphan signing key
    assert (set(os.listdir(certs)) if certs.exists() else set()) == certs_before
    monkeypatch.undo()
    assert peek(w, tok)[0] == 200  # the link was not used up
    # a failure after the user was added removes the user again, and the link still works
    real = w.api.d.channels.set_password
    monkeypatch.setattr(
        w.api.d.channels, "set_password", lambda *a, **k: (_ for _ in ()).throw(CS.ChannelError("store_error"))
    )
    assert redeem(w, tok)[0] == 503
    assert "dora" not in [u["name"] for u in w.env.acc.users()]
    assert set(os.listdir(ukdir)) == before
    monkeypatch.setattr(w.api.d.channels, "set_password", real)
    assert redeem(w, tok)[0] == 200


def test_generic_error_for_unknown_expired_used_and_malformed(w):
    inv = w.api.d.user_invites
    used = invite_join(w, "u1")
    assert redeem(w, used)[0] == 200
    old, _, _ = inv.create("join", created_by=w.admin, name="late")
    w.clock.advance(INVITE_TTL_MS + 1)
    unknown = b64u(os.urandom(32))
    answers = set()
    for t in (used, old, unknown, "short", "x" * 500, "has space and ??? " * 3):
        for call in (lambda t=t: peek(w, t), lambda t=t: redeem(w, t)):
            s, o, _ = call()
            answers.add((s, json.dumps(o, sort_keys=True)))
            w.clock.advance(10_000)  # keep clear of the per-source cap
    assert answers == {(403, json.dumps({"error": "bad_invite"}))}


def test_peek_is_rate_limited_per_source(w):
    codes = {peek(w, b64u(os.urandom(32)))[0] for _ in range(25)}
    assert codes == {403, 429}


def test_expiry_is_seven_days(w):
    tok = invite_join(w)
    w.clock.advance(INVITE_TTL_MS - 1000)
    assert peek(w, tok)[0] == 200
    w.clock.advance(2000)
    assert peek(w, tok)[:2] == BAD
    assert redeem(w, tok)[:2] == BAD


# --- B + condition 2 + condition 4: join ---------------------------------------------------------------------------------


def test_post_admin_users_is_gone_delete_stays(w):
    tok = w.login(w.admin)
    assert w.call("POST", "/admin/users", {"name": "carol"}, token=tok)[:2] == (404, {"error": "unknown_op"})
    carol = w.env.user("carol", password=False)
    assert w.call("DELETE", f"/admin/users/{carol}", token=tok)[0] == 200
    assert not [r for r in ROUTES if (r.method, r.path.endswith("/admin/users")) == ("POST", True)]


def test_join_makes_a_usable_account_and_the_admin_never_sees_a_secret(w):
    atok = w.login(w.admin)
    s, made, _ = w.call("POST", "/admin/invites", {"name": "  dora "}, token=atok)
    assert s == 200 and set(made) == {"link", "expires_ms"}
    assert made["link"].startswith(ORIGIN + "/#invite=") and made["expires_ms"] == w.clock() + INVITE_TTL_MS
    tok = token_of(made["link"])
    # no session needed to look at it; the name is the admin's choice (trimmed)
    assert peek(w, tok)[:2] == (200, {"kind": "join", "name": "dora", "needs_proof": False})
    assert redeem(w, tok, "short")[:2] == (400, {"error": "weak_password"})  # the link is still good
    s, got, _ = redeem(w, tok, NEW_PW)
    assert s == 200 and got["name"] == "dora" and got["kind"] == "join" and got["recovery"] is False
    uid = got["user_id"]
    assert got["totp_secret"] and got["totp_uri"].startswith("otpauth://")
    # sign in by name with the password the person chose and the code from their own secret
    s, o, h = sign_in_as(w, "dora", NEW_PW, got["totp_secret"])
    assert s == 200 and o["user_id"] == uid
    # condition 4: no group, no role anywhere, not an admin
    (me,) = [u for u in w.env.acc.users() if u["user_id"] == uid]
    assert me["groups"] == [] and not w.env.acc.is_hub_admin(uid) and w.env.acc.role_of(uid, WS) is None
    # the admin saw nothing secret in any answer: not the password, not the secret, not the uri
    seen = json.dumps(
        [
            w.call("GET", "/admin/users", token=atok)[1],
            w.call("GET", "/admin/channels", token=atok)[1],
            w.call("GET", "/admin/activity", token=atok)[1],
            made,
        ]
    )
    assert NEW_PW not in seen and got["totp_secret"] not in seen and "otpauth" not in seen
    # used once: the same link is dead now
    assert redeem(w, tok)[:2] == BAD and peek(w, tok)[:2] == BAD


def test_join_name_is_validated_like_a_user_name(w):
    atok = w.login(w.admin)
    for bad in ("", "   ", "bad\nname", "x" * 81):
        assert w.call("POST", "/admin/invites", {"name": bad}, token=atok)[:2] == (400, {"error": "malformed"}), bad
    assert w.call("POST", "/admin/invites", {}, token=atok)[0] == 400
    assert w.call("POST", "/admin/invites", {"name": "x"}, token=w.login(w.ann))[:2] == (
        403,
        {"error": "forbidden_role"},
    )
    assert w.call("POST", "/admin/invites", {"name": "x"})[0] == 401


def test_invites_are_unavailable_without_the_store(w):
    w.api.d.user_invites = None
    assert w.call("POST", "/admin/invites", {"name": "x"}, token=w.login(w.admin))[:2] == (
        503,
        {"error": "unavailable"},
    )
    assert peek(w, "x" * 43)[:2] == (503, {"error": "unavailable"})


# --- D + condition 1: reset needs a proof from a live channel -------------------------------------------------------------


def test_reset_with_a_live_channel_needs_a_proof_from_that_channel(w):
    signer, cid = with_channel(w, w.ann)
    bob = w.env.user("bob")
    bob_signer, _ = with_channel(w, bob)
    old = w.login(w.ann)
    tok = issue(w, w.ann)
    s, p, _ = peek(w, tok)
    assert s == 200 and p["kind"] == "reset" and p["needs_proof"] is True and p["recovery"] is False
    assert p["user_id"] == w.ann and p["name"] == "ann" and p["challenge"]
    # no proof: refused, in plain words' code; the link is not used up
    assert redeem(w, tok)[:2] == (403, {"error": "proof_required"})
    # a proof made with a key that is not hers (a stranger's, and another person's real channel)
    for who in (new_channel(), bob_signer):
        ch = peek(w, tok)[1]["challenge"]
        assert redeem(w, tok, proof=make_proof(who, ch, tok, cid))[:2] == (403, {"error": "proof_required"})
        assert redeem(w, tok, proof=make_proof(who, ch, tok))[:2] == (
            403,
            {"error": "proof_required"},
        )  # challenge used
    # her key, but signed over another link's hash: refused
    ch = peek(w, tok)[1]["challenge"]
    wrong = make_proof(signer, ch, "z" * 43)
    assert redeem(w, tok, proof=wrong)[:2] == (403, {"error": "proof_required"})
    # a challenge nobody issued
    assert redeem(w, tok, proof=make_proof(signer, b64u(os.urandom(32)), tok))[:2] == (403, {"error": "proof_required"})
    # the right key over the challenge and this link: accepted
    ch = peek(w, tok)[1]["challenge"]
    good = make_proof(signer, ch, tok)
    s, got, _ = redeem(w, tok, proof=good)
    assert s == 200 and got["user_id"] == w.ann and got["recovery"] is False and got["totp_secret"]
    # her channel is kept; no recovery note; every old session is dead; the new password and code work, the old not
    assert w.env.svc.active_channel(w.ann, cid) is not None
    assert w.env.acc.recovery_note(w.ann) is None
    assert w.call("GET", "/session", token=old)[:2] == (401, {"error": "no_session"})
    assert sign_in_as(w, w.ann, "correct horse battery", got["totp_secret"])[0] == 401
    assert sign_in_as(w, w.ann, NEW_PW, got["totp_secret"])[0] == 200
    # the used proof is no good a second time
    assert redeem(w, tok, proof=good)[:2] == BAD


def test_a_challenge_is_bound_to_its_link_and_expires_in_five_minutes(w):
    signer, cid = with_channel(w, w.ann)
    tok = issue(w, w.ann)
    ch = peek(w, tok)[1]["challenge"]
    w.clock.advance(5 * 60_000 + 1)
    assert redeem(w, tok, proof=make_proof(signer, ch, tok))[:2] == (403, {"error": "proof_required"})
    inv = w.api.d.user_invites
    assert inv.take_challenge(token_hash(tok), ch) is False  # used up by the attempt above (and expired)
    # a challenge belongs to the link it was issued for: another link's hash does not take it
    ch = peek(w, tok)[1]["challenge"]
    assert inv.take_challenge(token_hash("z" * 43), ch) is False
    assert inv.take_challenge(token_hash(tok), ch) is True
    # a person with no live channel is never handed a challenge
    t2 = issue(w, w.env.user("eve"))
    assert "challenge" not in peek(w, t2)[1]


def test_recovery_link_revokes_every_channel_and_leaves_a_note_and_a_record(w):
    s1, c1 = with_channel(w, w.ann)
    s2, c2 = with_channel(w, w.ann)
    old = w.login(w.ann)
    tok = issue(w, w.ann, recovery=True)
    s, p, _ = peek(w, tok)
    assert p["kind"] == "recovery" and p["needs_proof"] is False and p["recovery"] is True and "challenge" not in p
    w.clock.advance(1234)
    s, got, _ = redeem(w, tok)  # no proof needed, whoever opens the link has the account
    assert s == 200 and got["recovery"] is True
    assert {c["status"] for c in w.env.svc.channels(w.ann)} == {"revoked"}
    assert w.env.svc.active_channel(w.ann, c1) is None and w.env.svc.active_channel(w.ann, c2) is None
    assert w.env.acc.recovery_note(w.ann) == {"by": w.admin, "at_ms": w.clock()}
    assert w.call("GET", "/session", token=old)[:2] == (401, {"error": "no_session"})
    ev = [e for e in w.api.d.activity.list(limit=200) if e["type"] == "recovery.redeemed"]
    assert len(ev) == 1 and ev[0]["actor_id"] == w.admin and ev[0]["actor_kind"] == "person"
    assert ev[0]["subject_user"] == w.ann
    # the channel revocations are on the audit chain (the existing path)
    revokes = [e for _s, _r, e in w.env.audit.store.after(0, 1000) if e.get("change") == "revoke"]
    assert {e["channel_id"] for e in revokes} == {c1, c2}
    # the person's first screen after signing in carries the notice, and dismissing clears it
    s, o, h = sign_in_as(w, w.ann, NEW_PW, got["totp_secret"])
    assert s == 200
    tok2 = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
    w.browser_tokens.add(tok2)
    s, me, _ = w.call("GET", "/session", token=tok2)
    assert me["recovery"] == {"by_name": "admin", "at_ms": w.env.acc.recovery_note(w.ann)["at_ms"]}
    assert w.call("POST", "/me/recovery/dismiss", token=tok2)[:2] == (200, {"ok": True})
    assert w.call("GET", "/session", token=tok2)[1]["recovery"] is None


def test_a_reset_for_a_person_with_no_live_channel_is_a_recovery(w):
    tok = issue(w, w.ann)  # kind reset, but ann has no approving device
    p = peek(w, tok)[1]
    assert p["kind"] == "reset" and p["needs_proof"] is False and p["recovery"] is True
    s, got, _ = redeem(w, tok)
    assert s == 200 and got["recovery"] is True
    assert w.env.acc.recovery_note(w.ann)["by"] == w.admin
    assert [e["type"] for e in w.api.d.activity.list(limit=200)].count("recovery.redeemed") == 1


def test_a_channel_revoked_or_added_after_the_link_changes_the_rule_at_redeem_time(w):
    signer, cid = with_channel(w, w.ann)
    tok = issue(w, w.ann)
    assert peek(w, tok)[1]["needs_proof"] is True
    w.call("POST", f"/channels/{cid}/revoke", token=w.login(w.ann))  # her last device is removed meanwhile
    s, got, _ = redeem(w, tok)  # nothing left to prove with: a recovery now
    assert s == 200 and got["recovery"] is True and w.env.acc.recovery_note(w.ann) is not None
    # and the other way round: a device registered after the link was made is one she must prove with
    bob = w.env.user("bob2")
    t2 = issue(w, bob)
    assert peek(w, t2)[1]["needs_proof"] is False
    with_channel(w, bob)
    assert redeem(w, t2)[:2] == (403, {"error": "proof_required"})


def test_reset_and_recovery_are_admin_only_and_one_live_link_at_a_time(w):
    ann = w.login(w.ann)
    assert w.call("POST", f"/admin/users/{w.admin}/reset", {}, token=ann)[:2] == (403, {"error": "forbidden_role"})
    atok = w.login(w.admin)
    assert w.call("POST", f"/admin/users/{'ab' * 16}/reset", {}, token=atok)[:2] == (404, {"error": "unknown_user"})
    assert w.call("POST", f"/admin/users/{w.ann}/reset", {"recovery": "yes"}, token=atok)[0] == 400
    first = issue(w, w.ann)
    second = issue(w, w.ann, recovery=True)
    assert peek(w, first)[:2] == BAD  # voided by the newer link
    assert peek(w, second)[0] == 200


# --- F + condition 5: disable and enable --------------------------------------------------------------------------------


def _count_hashing(monkeypatch):
    calls = []
    real = CS._pw_hash
    monkeypatch.setattr(CS, "_pw_hash", lambda pw, salt: calls.append(1) or real(pw, salt))
    return calls


def test_disable_takes_effect_everywhere_at_once(w, monkeypatch):
    tok = w.login(w.ann)
    signer, cid = with_channel(w, w.ann)
    s, prep, _ = w.call(
        "POST", "/sign/prepare", {"workspace_id": WS, "channel_id": cid, "action": dict(ACTION)}, token=tok
    )
    assert s == 200 and w.api._prepared and w.engine._pending
    pending_reset = issue(w, w.ann)
    before = w.env.snaps.snapshot(WS, "full")
    assert before["o"]["roles"][w.ann] == "edit"
    atok = w.login(w.admin)
    assert w.call("POST", f"/admin/users/{w.ann}/disable", token=atok)[:2] == (200, {"ok": True})
    # all roles gone from the snapshot, whose version moved up like for a grant change
    after = w.env.snaps.snapshot(WS, "full")
    assert w.ann not in after["o"]["roles"] and after["o"]["version"] == before["o"]["version"] + 1
    assert w.env.acc.role_of(w.ann, WS) is None and w.ann not in w.env.acc.roles(WS)
    assert after["o"]["roles"][w.admin] == "admin"  # others are untouched
    assert cid in after["o"]["revoked_channels"]
    # the session is dead, every channel revoked, open prepared approvals voided
    assert w.call("GET", "/session", token=tok)[:2] == (401, {"error": "no_session"})
    assert {c["status"] for c in w.env.svc.channels(w.ann)} == {"revoked"}
    assert not w.api._prepared and not w.engine._pending
    assert peek(w, pending_reset)[:2] == BAD  # a link made for them is dead too
    # sign-in is refused with the very answer of a wrong password, and the password work was still done
    calls = _count_hashing(monkeypatch)
    wrong = w.call(
        "POST",
        "/session",
        {"user_id": w.ann, "password": "nope nope nope", "totp": "000000", "client": "browser"},
    )
    n_wrong = len(calls)
    w.clock.advance(60_000)
    right = w.call(
        "POST",
        "/session",
        {
            "user_id": w.ann,
            "password": "correct horse battery",
            "totp": w.env.next_code(w.ann),
            "client": "browser",
        },
    )
    assert right[:2] == wrong[:2] == (401, {"error": "bad_credentials"})
    assert n_wrong >= 1 and len(calls) == 2 * n_wrong
    # listed as disabled
    row = [u for u in w.call("GET", "/admin/users", token=atok)[1]["users"] if u["user_id"] == w.ann][0]
    assert row["disabled"] is True
    # activity
    ev = [e for e in w.api.d.activity.list(limit=200) if e["type"] == "user.disabled"]
    assert len(ev) == 1 and ev[0]["actor_id"] == w.admin and ev[0]["subject_user"] == w.ann


def test_a_disabled_user_cannot_prepare_or_sign_even_with_a_stale_channel(w):
    tok = w.login(w.ann)
    signer, cid = with_channel(w, w.ann)
    s, prep, _ = w.call(
        "POST", "/sign/prepare", {"workspace_id": WS, "channel_id": cid, "action": dict(ACTION)}, token=tok
    )
    assert s == 200
    w.env.acc.set_disabled(w.ann, True)  # the flag alone: no channel revoked, no session ended, nothing voided
    assert w.env.svc.active_channel(w.ann, cid) is None  # the stale channel is not live
    s, o, _ = w.call(
        "POST",
        "/sign/sign",
        {"prepared_id": prep["prepared_id"], "channel_id": cid, "channel_proof": w.proof(signer, prep["digest"])},
        token=tok,
    )
    assert s == 401 and w.delivered == []
    with pytest.raises(Exception) as e:  # straight at the engine: the prepared one was not voided, still refused
        w.engine.sign(prep["prepared_id"], cid, w.proof(signer, prep["digest"]))
    assert getattr(e.value, "code", "") in ("channel_revoked", "unknown_prepared") and w.delivered == []
    with pytest.raises(Exception) as e:
        w.engine.prepare(tok, WS, w.ann, cid, dict(ACTION))
    assert getattr(e.value, "code", "") == "unauthorized"
    assert (
        w.call("POST", "/sign/prepare", {"workspace_id": WS, "channel_id": cid, "action": dict(ACTION)}, token=tok)[0]
        == 401
    )


def test_disable_refuses_yourself_and_the_last_admin(w):
    atok = w.login(w.admin)
    assert w.call("POST", f"/admin/users/{w.admin}/disable", token=atok)[:2] == (409, {"error": "self_disable"})
    assert w.env.acc.is_active(w.admin)
    with pytest.raises(AccessError) as e:  # the only admin (the operator path has no actor to compare)
        w.env.acc.set_disabled(w.admin, True)
    assert e.value.code == "last_admin"
    # with a second admin the first may be disabled by the second, but then the second is the last
    w.env.acc.add_member("hub-admin", w.ann)
    atok2 = w.login(w.ann)
    assert w.call("POST", f"/admin/users/{w.admin}/disable", token=atok2)[:2] == (200, {"ok": True})
    with pytest.raises(AccessError) as e:
        w.env.acc.set_disabled(w.ann, True)
    assert e.value.code == "last_admin"
    assert not w.env.acc.is_hub_admin(w.admin) and w.env.acc.is_hub_admin(w.ann)
    assert w.call("POST", f"/admin/users/{'ab' * 16}/disable", token=atok2)[:2] == (404, {"error": "unknown_user"})


def test_a_disabled_admin_has_no_admin_standing(w):
    w.env.acc.add_member("hub-admin", w.ann)
    w.login(w.ann)
    w.env.acc.set_disabled(w.ann, True)
    assert not w.env.acc.is_hub_admin(w.ann) and w.env.acc.roles(WS).get(w.ann) is None
    assert w.env.acc.members("hub-admin") == sorted([w.admin, w.ann])  # membership itself is not edited


def test_disable_touches_only_the_flag(w):
    w.reg.add("cd" * 16, "selective", owner=w.ann)
    w.call("POST", f"/admin/users/{w.ann}/disable", token=w.login(w.admin))
    assert w.reg.owner_alias("cd" * 16) == w.ann  # a workspace's own owner is not rewritten
    assert w.env.acc.groups_of(w.ann) == ["team"]  # group membership is as it was


def test_enable_clears_the_flag_and_issues_a_recovery_link(w):
    atok = w.login(w.admin)
    assert w.call("POST", f"/admin/users/{w.ann}/enable", token=atok)[:2] == (409, {"error": "conflict"})
    w.call("POST", f"/admin/users/{w.ann}/disable", token=atok)
    assert w.call("POST", f"/admin/users/{w.ann}/reset", {}, token=atok)[:2] == (409, {"error": "conflict"})
    s, o, _ = w.call("POST", f"/admin/users/{w.ann}/enable", token=atok)
    assert s == 200 and o["kind"] == "recovery" and o["name"] == "ann" and o["expires_ms"] == w.clock() + INVITE_TTL_MS
    assert w.env.acc.is_active(w.ann) and w.env.acc.role_of(w.ann, WS) == "edit"
    tok = token_of(o["link"])
    p = peek(w, tok)[1]
    assert p["kind"] == "recovery" and p["recovery"] is True and p["needs_proof"] is False
    s, got, _ = redeem(w, tok)
    assert s == 200 and w.env.acc.recovery_note(w.ann)["by"] == w.admin
    assert sign_in_as(w, w.ann, NEW_PW, got["totp_secret"])[0] == 200
    kinds = [e["type"] for e in w.api.d.activity.list(limit=200)]
    for k in ("user.disabled", "user.enabled", "reset.issued", "recovery.redeemed"):
        assert k in kinds, k


# --- H: activity records ------------------------------------------------------------------------------------------------


def test_every_invite_event_is_recorded_with_references_only(w):
    atok = w.login(w.admin)
    tok = invite_join(w, "frank")
    got = redeem(w, tok)[1]
    signer, cid = with_channel(w, w.ann)
    rt = issue(w, w.ann)
    ch = peek(w, rt)[1]["challenge"]
    assert redeem(w, rt, proof=make_proof(signer, ch, rt))[0] == 200
    rec = issue(w, w.ann, recovery=True)
    assert redeem(w, rec)[0] == 200
    w.call("POST", f"/admin/users/{w.ann}/disable", token=atok)
    w.call("POST", f"/admin/users/{w.ann}/enable", token=atok)
    events = w.api.d.activity.list(limit=200)
    kinds = {e["type"] for e in events}
    assert {
        "invite.created",
        "invite.redeemed",
        "reset.issued",
        "recovery.redeemed",
        "user.disabled",
        "user.enabled",
    } <= kinds
    dump = json.dumps(events)
    for secret in (tok, rt, rec, got["totp_secret"], NEW_PW):
        assert secret not in dump
    assert all(set(e["detail"]) <= {"invite_id"} for e in events)
    assert w.api.d.activity.verify()
    created = [e for e in events if e["type"] == "invite.created"]
    assert created and all(e["actor_id"] == w.admin for e in created)
    joined = [e for e in events if e["type"] == "invite.redeemed" and e["subject_user"] == got["user_id"]]
    assert len(joined) == 1 and joined[0]["actor_id"] == got["user_id"]
    assert w.api.d.activity.verify()


def test_the_access_log_never_sees_a_request_body(w, capfd, caplog):
    tok = invite_join(w, "gina")
    s0 = socket.socket()
    s0.bind(("127.0.0.1", 0))
    port = s0.getsockname()[1]
    s0.close()
    w.api._hosts = frozenset({f"127.0.0.1:{port}"})
    w.api.d.allowed_origins = frozenset({f"http://127.0.0.1:{port}"})
    srv = WebServer(w.api, "127.0.0.1", port)
    srv.start()
    try:
        rq = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/invite/peek",
            data=json.dumps({"token": tok}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Orch-Csrf": "1"},
        )
        with urllib.request.urlopen(rq) as r:
            assert r.status == 200 and json.loads(r.read())["name"] == "gina"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v1/invite/redeem",
                    data=json.dumps({"token": tok, "password": "x"}).encode(),
                    method="POST",
                    headers={"Content-Type": "application/json", "X-Orch-Csrf": "1"},
                )
            )
    finally:
        srv.close()
    out, err = capfd.readouterr()
    assert tok not in out + err + caplog.text and "invite" not in out + err

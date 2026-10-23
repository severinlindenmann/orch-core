"""V3-D13: channel registration, prepare/sign over HTTP (the channel signs, the server never does), rate limit."""

from __future__ import annotations

import pytest
from channels_helpers import new_channel, prove
from custodial_helpers import ACTION
from webapi_helpers import ORIGIN, WS, Web

from orch_hub.contract import hub as H
from orch_hub.contract.canon import b64u, cj


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    yield x
    x.close()


def prepared(w, tok, cid, action=None):
    s, o, _ = w.call(
        "POST", "/sign/prepare", {"workspace_id": WS, "channel_id": cid, "action": action or dict(ACTION)}, token=tok
    )
    return s, o


def test_register_prepare_sign_end_to_end(w):
    tok = w.login(w.ann)
    signer, cid = w.register_channel(tok)
    s, o = prepared(w, tok, cid)
    assert s == 200, o
    # the channel recomputes the digest from the ctx it received and shows every field
    assert H.event_digest(WS, o["ctx"]["log"], o["ctx"]["event"]).hex() == o["digest"]
    assert o["shown"]["type"] == "log.added" and o["shown"]["fields"]["actor"]["id"] == "u_" + w.ann
    assert "channel_cert" not in o["shown"]["fields"] and "channel_proof" not in o["ctx"]["event"]
    s, got, _ = w.call(
        "POST",
        "/sign/sign",
        {"prepared_id": o["prepared_id"], "channel_id": cid, "channel_proof": w.proof(signer, o["digest"])},
        token=tok,
    )
    assert s == 200 and got["type"] == "log.added" and got["workspace_id"] == WS
    ((_, _, ev),) = w.delivered
    assert ev["auth"] == "hub" and ev["actor"]["via"] == cid and "sig" in ev
    assert any(r[1]["o"]["type"] == "custodial" for r in w.env.audit.store.after(0, 100))


def test_sign_with_a_proof_from_another_key_signs_nothing(w):
    tok = w.login(w.ann)
    _, cid = w.register_channel(tok)
    s, o = prepared(w, tok, cid)
    s, got, _ = w.call(
        "POST",
        "/sign/sign",
        {"prepared_id": o["prepared_id"], "channel_id": cid, "channel_proof": w.proof(new_channel(), o["digest"])},
        token=tok,
    )
    assert (s, got) == (403, {"error": "bad_channel_proof"}) and w.delivered == []


def test_session_alone_cannot_sign_and_prepared_is_single_use(w):
    tok = w.login(w.ann)
    signer, cid = w.register_channel(tok)
    _, o = prepared(w, tok, cid)
    body = {"prepared_id": o["prepared_id"], "channel_id": cid, "channel_proof": b64u(b"\0" * 64)}
    assert w.call("POST", "/sign/sign", body, token=tok)[0] == 403  # bad proof consumes it
    good = {**body, "channel_proof": w.proof(signer, o["digest"])}
    assert w.call("POST", "/sign/sign", good, token=tok)[:2] == (404, {"error": "unknown_prepared"})
    assert w.delivered == []


def test_another_users_prepared_id_is_unknown(w):
    tok = w.login(w.ann)
    signer, cid = w.register_channel(tok)
    _, o = prepared(w, tok, cid)
    bob = w.env.user("bob")
    w.env.acc.add_member("team", bob)
    btok = w.login(bob)
    s, got, _ = w.call(
        "POST",
        "/sign/sign",
        {"prepared_id": o["prepared_id"], "channel_id": cid, "channel_proof": w.proof(signer, o["digest"])},
        token=btok,
    )
    assert (s, got) == (404, {"error": "unknown_prepared"}) and w.delivered == []


def test_prepare_with_someone_elses_channel_is_refused(w):
    tok = w.login(w.ann)
    _, cid = w.register_channel(tok)
    bob = w.env.user("bob")
    w.env.acc.add_member("team", bob)
    s, o = prepared(w, w.login(bob), cid)
    assert (s, o) == (403, {"error": "unauthorized"})


def test_prepare_in_a_workspace_without_a_role_is_unknown_workspace(w):
    tok = w.login(w.ann)
    _, cid = w.register_channel(tok)
    w.reg.add("ef" * 16, "full")
    s, o, _ = w.call(
        "POST", "/sign/prepare", {"workspace_id": "ef" * 16, "channel_id": cid, "action": dict(ACTION)}, token=tok
    )
    assert (s, o) == (404, {"error": "unknown_workspace"})


def test_read_role_cannot_start_and_unknown_action_is_refused(w):
    w.env.acc.grant(WS, "team", "read")
    tok = w.login(w.ann)
    _, cid = w.register_channel(tok)
    assert prepared(w, tok, cid)[1] == {"error": "forbidden_role"}
    w.env.acc.grant(WS, "team", "edit")
    assert prepared(w, tok, cid, {"type": "role.changed", "log": "workspace"})[1] == {"error": "not_custodial"}
    assert prepared(w, tok, cid, {"type": "log.added", "log": ACTION["log"], "text": "x", "sig": "y"})[1] == {
        "error": "malformed"
    }


def test_prepare_is_rate_limited_per_user(w):
    tok = w.login(w.ann)
    _, cid = w.register_channel(tok)
    codes = [prepared(w, tok, cid)[0] for _ in range(11)]
    assert codes == [200] * 10 + [429]
    other = w.env.user("bob")
    w.env.acc.add_member("team", other)
    assert prepared(w, w.login(other), cid)[0] != 429  # its own bucket (refused for the channel, not for the rate)
    w.clock.t += 61_000
    assert prepared(w, tok, cid)[0] == 200


def test_revoked_channel_cannot_prepare_or_sign(w):
    tok = w.login(w.ann)
    signer, cid = w.register_channel(tok)
    _, o = prepared(w, tok, cid)
    assert w.call("POST", f"/channels/{cid}/revoke", token=tok)[:2] == (200, {"ok": True})
    s, _, _ = w.call(
        "POST",
        "/sign/sign",
        {"prepared_id": o["prepared_id"], "channel_id": cid, "channel_proof": w.proof(signer, o["digest"])},
        token=tok,
    )
    assert s == 403 and w.delivered == []
    assert prepared(w, tok, cid) == (403, {"error": "unauthorized"})


def test_signing_unavailable_without_an_engine(w):
    w.api.d.engine = None
    tok = w.login(w.ann)
    _, cid = w.register_channel(tok)
    assert prepared(w, tok, cid) == (503, {"error": "unavailable"})


def test_registration_rules_over_http(w):
    tok = w.login(w.ann)
    signer = new_channel()
    s, begun, _ = w.call(
        "POST",
        "/channels/register/begin",
        {"kind": "browser", "key_pub": b64u(signer.public), **w.creds(w.ann)},
        token=tok,
    )
    assert s == 200 and begun["challenge"]["attestation"] == "session"
    bad = prove(signer, begun)
    bad = {**bad, "sig": b64u(b"\0" * 64)}  # a signature that is not the key's
    s, o, _ = w.call(
        "POST", "/channels/register/complete", {"challenge_id": begun["challenge_id"], "signed": bad}, token=tok
    )
    assert (s, o) == (403, {"error": "bad_signature"})
    # consumed by the first attempt
    s, o, _ = w.call(
        "POST",
        "/channels/register/complete",
        {"challenge_id": begun["challenge_id"], "signed": prove(signer, begun)},
        token=tok,
    )
    assert o == {"error": "challenge_invalid"}
    # a phone needs the operator's pairing code
    s, o, _ = w.call(
        "POST", "/channels/register/begin", {"kind": "phone", "key_pub": b64u(new_channel().public)}, token=tok
    )
    assert (s, o) == (400, {"error": "malformed"})
    s, o, _ = w.call(
        "POST",
        "/channels/register/begin",
        {"kind": "phone", "key_pub": b64u(new_channel().public), "pairing_code": "nope"},
        token=tok,
    )
    assert (s, o) == (400, {"error": "bad_pairing_code"})
    code = w.env.svc.create_pairing_code(w.ann)["code"]
    p = new_channel()
    s, begun, _ = w.call(
        "POST",
        "/channels/register/begin",
        {"kind": "phone", "key_pub": b64u(p.public), "pairing_code": code},
        token=tok,
    )
    s, got, _ = w.call(
        "POST",
        "/channels/register/complete",
        {"challenge_id": begun["challenge_id"], "signed": prove(p, begun)},
        token=tok,
    )
    assert s == 200 and got["channel_cert"]["o"]["factor"] == "se-unlocked"
    s, o, _ = w.call("GET", "/me/channels", token=tok)
    assert {c["kind"] for c in o["channels"]} == {"phone"}


def test_malformed_key_pub(w):
    tok = w.login(w.ann)
    for bad in ("", "AAAA", b64u(b"\x04" + b"\0" * 64)):
        s, o, _ = w.call(
            "POST", "/channels/register/begin", {"kind": "browser", "key_pub": bad, **w.creds(w.ann)}, token=tok
        )
        assert s == 400 and o["error"] == "malformed"


def test_no_route_takes_event_bytes_or_a_digest_to_sign():
    from orch_hub.webapi import ROUTES

    assert [r.path for r in ROUTES if "sign" in r.path and r.method == "POST"] == [
        "/api/v1/sign/prepare",
        "/api/v1/sign/sign",
    ]
    assert cj({}) == b"{}"


def test_get_prepared_returns_the_same_answer_to_its_owner_only(w):
    tok = w.login(w.ann)
    signer, cid = w.register_channel(tok)
    s, o = prepared(w, tok, cid)
    assert s == 200, o
    s, again, _ = w.call("GET", "/sign/prepared/" + o["prepared_id"], token=tok)
    assert s == 200 and again == o  # same ctx, digest and "shown"; reading does not consume it
    other = w.login(w.admin)
    assert w.call("GET", "/sign/prepared/" + o["prepared_id"], token=other)[:2] == (404, {"error": "unknown_prepared"})
    assert w.call("GET", "/sign/prepared/" + "ab" * 16, token=tok)[:2] == (404, {"error": "unknown_prepared"})
    assert w.call("GET", "/sign/prepared/" + o["prepared_id"])[0] == 401  # no session
    s, got, _ = w.call(
        "POST",
        "/sign/sign",
        {"prepared_id": o["prepared_id"], "channel_id": cid, "channel_proof": w.proof(signer, again["digest"])},
        token=tok,
    )
    assert s == 200, got
    assert w.call("GET", "/sign/prepared/" + o["prepared_id"], token=tok)[:2] == (404, {"error": "unknown_prepared"})


def test_get_prepared_after_expiry_is_prepared_expired(w):
    tok = w.login(w.ann)
    _, cid = w.register_channel(tok)
    _, o = prepared(w, tok, cid)
    w.clock.t = o["expires_ms"] + 1
    tok = w.login(w.ann)  # the first session expired with the clock
    assert w.call("GET", "/sign/prepared/" + o["prepared_id"], token=tok)[:2] == (410, {"error": "prepared_expired"})


def test_browser_registration_needs_password_and_a_fresh_totp(w):
    """A8-3: a session alone (a stolen cookie, a script on the page) cannot mint a browser key."""
    tok = w.login(w.ann)
    key = b64u(new_channel().public)

    def begin(extra):
        return w.call("POST", "/channels/register/begin", {"kind": "browser", "key_pub": key, **extra}, token=tok)

    assert begin({})[:2] == (400, {"error": "malformed"})
    assert begin({"password": "correct horse battery"})[:2] == (400, {"error": "malformed"})
    c = w.creds(w.ann)
    assert begin({**c, "password": "wrong password!!"})[:2] == (401, {"error": "bad_credentials"})
    c = w.creds(w.ann)
    assert begin({**c, "totp": "000000"})[:2] == (401, {"error": "bad_credentials"})
    ok = w.creds(w.ann)
    assert begin(ok)[0] == 200
    assert begin(ok)[:2] == (401, {"error": "bad_credentials"})  # the TOTP step is single use
    assert w.env.svc.channels(w.ann) == []  # nothing registered by the refusals


def test_the_code_used_at_sign_in_does_not_register_a_key(w):
    code = w.env.next_code(w.ann)
    s, o, h = w.call(
        "POST",
        "/session",
        {"user_id": w.ann, "password": "correct horse battery", "totp": code, "client": "browser"},
        origin=ORIGIN,
    )
    assert s == 200
    tok = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
    s, o, _ = w.call(
        "POST",
        "/channels/register/begin",
        {"kind": "browser", "key_pub": b64u(new_channel().public), "password": "correct horse battery", "totp": code},
        token=tok,
        cookie=True,
    )
    assert (s, o) == (401, {"error": "bad_credentials"})


def test_registration_reentry_shares_the_sign_in_limiter(w):
    tok = w.login(w.ann)
    key = b64u(new_channel().public)
    seen = set()
    for _ in range(40):
        s, _o, _ = w.call(
            "POST",
            "/channels/register/begin",
            {"kind": "browser", "key_pub": key, "password": "nope nope nope!", "totp": "123456"},
            token=tok,
        )
        seen.add(s)
        if s == 429:
            break
    assert 429 in seen
    # while limited, even the right credentials are refused
    s, _o, _ = w.call(
        "POST", "/channels/register/begin", {"kind": "browser", "key_pub": key, **w.creds(w.ann)}, token=tok
    )
    assert s in (401, 429)


def test_facts_of_decisions_and_moves_in_shown():
    from orch_hub.webapi.api import _facts

    src = [{"repo": "github.com/acme/app", "ref": "refs/heads/x", "sha": "c0" * 20}]
    ev = {"type": "gate.approved", "gate": "code", "hash": "sha256:" + "ab" * 32, "gate_gen": 3, "source_sha": src}
    assert _facts(ev) == [
        ["Gate", "code"],
        ["Content reference", "abab abab abab"],
        ["Generation", "3"],
        ["Code", "github.com/acme/app c0c0c0c0"],
    ]
    assert _facts({**ev, "type": "verdict.given"})[0] == ["Gate", "verify"]
    assert _facts({"type": "ticket.moved", "from": "testing", "to": "backlog"}) == [["Status", "testing → backlog"]]
    assert _facts({"type": "log.added"}) == []

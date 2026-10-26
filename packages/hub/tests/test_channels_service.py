"""V3-D7: sign-in, registration with proof of possession, certificates against the contract verifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from channels_helpers import PW, Env, new_channel, prove, register

from orch_hub.channels import ChannelError
from orch_hub.contract import hub as H
from orch_hub.contract.canon import b64u

VEC = json.loads((Path(__file__).parent.parent / "vectors" / "vectors_hub.json").read_text())


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def refused(code, fn, *a, **kw):
    with pytest.raises(ChannelError) as ei:
        fn(*a, **kw)
    assert ei.value.code == code, ei.value
    return ei.value


def held(env):
    k = env.keys
    return H.verify_card(k.card(), now_ms=env.clock(), dev_mode=True, pinned_root=k.hk_root_pub)


def test_cert_verifies_with_contract_verifier_and_matches_vector_shape(env):
    u = env.user("ann")
    signer, cert = register(env, u)
    o = H.verify_channel_cert(cert, held(env), now_ms=env.clock(), dev_mode=True)
    assert o["user_id"] == u and o["key_pub"] == b64u(signer.public) and o["factor"] == "password+totp"
    assert o["channel_id"] == H.channel_id(signer.public) and o["dev"] is True
    vec = next(v for k, v in _iter_vectors() if v.get("o", {}).get("kind") == "hub.channel_cert")
    assert set(o) == set(vec["o"]) and set(cert) == {"o", "sig"}
    assert env.svc.active_channel(u, o["channel_id"]) == cert


def _iter_vectors():
    def walk(x, name=""):
        if isinstance(x, dict):
            yield name, x
            for k, v in x.items():
                yield from walk(v, str(k))
        elif isinstance(x, list):
            for v in x:
                yield from walk(v, name)

    yield from walk(VEC)


def test_registration_without_proof_of_possession_is_refused(env):
    u = env.user("ann")
    token = env.sign_in(u)
    mine, other = new_channel(), new_channel()
    begun = env.svc.begin_registration(token, mine.public)
    refused(
        "bad_signature",
        env.svc.complete_registration,
        begun["challenge_id"],
        {"o": begun["challenge"], "sig": b64u(b"\0" * 64)},
    )
    # the challenge is single use: even the right proof afterwards is refused
    refused("challenge_invalid", env.svc.complete_registration, begun["challenge_id"], prove(mine, begun))
    # a proof by another key (someone registering a key they do not hold) is refused
    begun = env.svc.begin_registration(token, mine.public)
    refused(
        "challenge_invalid",  # a valid registration, but for a key the gate was not opened for
        env.svc.complete_registration,
        begun["challenge_id"],
        other.sign_object({**begun["challenge"], "key_pub": b64u(other.public)}),
    )
    # a proof over another challenge does not carry over
    b1 = env.svc.begin_registration(token, mine.public)
    b2 = env.svc.begin_registration(token, mine.public)
    refused("challenge_invalid", env.svc.complete_registration, b2["challenge_id"], prove(mine, b1))
    assert env.svc.channels(u) == []
    assert env.keys.channel_certs(u) == []


def test_registration_needs_a_live_session_and_expires(env):
    u = env.user("ann")
    refused("no_session", env.svc.begin_registration, "nope", new_channel().public)
    token = env.sign_in(u)
    s = new_channel()
    begun = env.svc.begin_registration(token, s.public)
    env.clock.advance(121_000)
    refused("challenge_invalid", env.svc.complete_registration, begun["challenge_id"], prove(s, begun))
    begun = env.svc.begin_registration(token, s.public)
    env.svc.sign_out(token)  # session ended between begin and complete
    refused("no_session", env.svc.complete_registration, begun["challenge_id"], prove(s, begun))
    env.clock.advance(16 * 60_000)
    refused("no_session", env.svc.session_user, token)


def test_invalid_key_and_label_refused(env):
    u = env.user("ann")
    t = env.sign_in(u)
    refused("malformed", env.svc.begin_registration, t, b"\x04" + b"\1" * 64)
    refused("malformed", env.svc.begin_registration, t, new_channel().public, "bad\nlabel")


def test_wrong_password_and_totp_are_rate_limited(env):
    u = env.user("ann")
    for _ in range(5):
        refused("bad_credentials", env.svc.sign_in, u, "wrong password!!", env.next_code(u))
    # locked out even with the right credentials
    refused("rate_limited", env.svc.sign_in, u, PW, env.next_code(u))
    env.clock.advance(15 * 60_000 + 1)
    assert env.svc.sign_in(u, PW, env.next_code(u))["token"]


def test_wrong_totp_counts_and_failure_is_generic(env):
    u = env.user("ann")
    e1 = refused("bad_credentials", env.svc.sign_in, u, PW, "000000")
    e2 = refused("bad_credentials", env.svc.sign_in, "ab" * 16, PW, "000000")  # unknown user: same answer
    assert str(e1) == str(e2)
    for _ in range(4):
        refused("bad_credentials", env.svc.sign_in, u, PW, "000000")
    refused("rate_limited", env.svc.sign_in, u, PW, env.next_code(u))


def test_totp_code_is_single_use_and_success_resets_failures(env):
    u = env.user("ann")
    for _ in range(3):
        refused("bad_credentials", env.svc.sign_in, u, "wrong password!!", env.next_code(u))
    code = env.next_code(u)
    assert env.svc.sign_in(u, PW, code)
    refused("bad_credentials", env.svc.sign_in, u, PW, code)  # replay of the same code
    for _ in range(2):  # counter was reset by the success: 1 replay + 2 more is still under 5
        refused("bad_credentials", env.svc.sign_in, u, "wrong password!!", env.next_code(u))
    assert env.svc.sign_in(u, PW, env.next_code(u))


def test_user_without_credentials_or_removed_cannot_sign_in(env):
    u = env.user("ann", password=False)
    refused("bad_credentials", env.svc.sign_in, u, PW, "123456")
    v = env.user("bob")
    t = env.sign_in(v)
    env.acc.remove_user(v)
    refused("no_session", env.svc.session_user, t)
    refused("bad_credentials", env.svc.sign_in, v, PW, env.next_code(v))


def test_set_password_ends_sessions_and_weak_password_refused(env):
    u = env.user("ann")
    t = env.sign_in(u)
    refused("weak_password", env.svc.set_password, u, "short")
    env.svc.set_password(u, "another long password")
    refused("no_session", env.svc.session_user, t)


def test_max_channels_per_user(env):
    u = env.user("ann")
    env.svc.max_channels = 2
    register(env, u)
    register(env, u)
    s = new_channel()
    t = env.sign_in(u)
    begun = env.svc.begin_registration(t, s.public)
    refused("too_many_channels", env.svc.complete_registration, begun["challenge_id"], prove(s, begun))
    other = env.user("bob")
    register(env, other)  # the limit is per user


def test_same_key_cannot_register_twice(env):
    u = env.user("ann")
    s, _ = register(env, u)
    v = env.user("bob")
    t = env.sign_in(v)
    begun = env.svc.begin_registration(t, s.public)
    refused("key_in_use", env.svc.complete_registration, begun["challenge_id"], prove(s, begun))  # one key, one user
    t = env.sign_in(u)  # the same user again: not a second channel either
    begun = env.svc.begin_registration(t, s.public)
    refused("channel_exists", env.svc.complete_registration, begun["challenge_id"], prove(s, begun))


def test_phone_pairing_gives_se_unlocked_once_with_code(env):
    u = env.user("ann")
    s = new_channel()
    t = env.sign_in(u)
    refused("bad_pairing_code", env.svc.begin_phone_registration, u, "0000-0000-0000", s.public, token=t)
    code = env.svc.create_pairing_code(u)["code"]
    v = env.user("bob")
    refused(
        "bad_pairing_code", env.svc.begin_phone_registration, v, code, s.public, token=env.sign_in(v)
    )  # another user's code
    begun = env.svc.begin_phone_registration(u, code, s.public, "iPhone", token=t)
    cert = env.svc.complete_registration(begun["challenge_id"], prove(s, begun))
    assert cert["o"]["factor"] == "se-unlocked"
    H.verify_channel_cert(cert, held(env), now_ms=env.clock(), dev_mode=True)
    refused("bad_pairing_code", env.svc.begin_phone_registration, u, code, new_channel().public, token=t)  # pairs once


def test_pairing_attempts_are_rate_limited_and_code_expires(env):
    u = env.user("ann")
    s = new_channel()
    code = env.svc.create_pairing_code(u)["code"]
    t = env.sign_in(u)
    for _ in range(5):
        refused("bad_pairing_code", env.svc.begin_phone_registration, u, "1111-1111-1111", s.public, token=t)
    refused("rate_limited", env.svc.begin_phone_registration, u, code, s.public, token=t)
    env.clock.advance(16 * 60_000)
    t = env.sign_in(u)  # the session itself lasts 15 min
    refused("bad_pairing_code", env.svc.begin_phone_registration, u, code, s.public, token=t)  # expired after 10 min


def test_a_password_session_cannot_mint_a_phone_factor(env):
    u = env.user("ann")
    _, cert = register(env, u)
    assert cert["o"]["factor"] == "password+totp"
    assert not hasattr(env.svc, "factor")  # the factor follows the registration path, the caller never names it


def test_secrets_never_in_errors_events_or_cert_files(env, tmp_path, caplog):
    u = env.user("ann")
    with pytest.raises(ChannelError) as ei:
        env.svc.sign_in(u, "wrong password!!", "000000")
    token = env.sign_in(u)
    s = new_channel()
    begun = env.svc.begin_registration(token, s.public)
    env.svc.complete_registration(begun["challenge_id"], prove(s, begun))
    blob = json.dumps(env.svc.events()) + str(ei.value) + caplog.text + json.dumps(env.svc.channels(u))
    for secret in (PW, token, "wrong password!!", env.secrets[u].hex()):
        assert secret not in blob
    raw = (tmp_path / "hub" / "channels.db").read_bytes()
    assert PW.encode() not in raw and token.encode() not in raw  # only hashes are stored


def test_private_db_file_modes(env, tmp_path):
    db = tmp_path / "hub" / "channels.db"
    assert db.stat().st_mode & 0o077 == 0
    env.svc.close()
    db.chmod(0o644)
    from orch_hub.channels import Channels

    with pytest.raises(ChannelError) as ei:
        Channels(env.keys, env.acc, db, dev_mode=True)
    assert "bad_private_file" in str(ei.value)
    db.chmod(0o600)
    env.svc = Channels(env.keys, env.acc, db, now_ms=env.clock, dev_mode=True)


def _race(n, fn):
    import threading

    barrier, out = threading.Barrier(n), []

    def work():
        barrier.wait()
        try:
            out.append(("ok", fn()))
        except ChannelError as e:
            out.append(("err", e.code))

    ts = [threading.Thread(target=work) for _ in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def test_parallel_sign_ins_with_one_totp_code_yield_one_session(env):
    u = env.user("ann")
    code = env.next_code(u)
    out = _race(8, lambda: env.svc.sign_in(u, PW, code))
    assert [k for k, _ in out].count("ok") == 1, out
    assert {v for k, v in out if k == "err"} <= {"bad_credentials", "rate_limited"}
    assert env.db_count("sessions", u) == 1


def test_parallel_wrong_attempts_cannot_exceed_the_failure_limit(env):
    u = env.user("ann")
    out = _race(12, lambda: env.svc.sign_in(u, "wrong password!!", "000000"))
    codes = [v for _, v in out]
    assert codes.count("bad_credentials") == 5 and codes.count("rate_limited") == 7, codes
    # right credentials racing the lockout also lose
    out = _race(4, lambda: env.svc.sign_in(u, PW, env.code(u, 1)))
    assert all(k == "err" for k, _ in out)


def test_parallel_pairing_guesses_are_limited(env):
    u = env.user("ann")
    s = new_channel()
    t = env.sign_in(u)
    out = _race(10, lambda: env.svc.begin_phone_registration(u, "1111-1111-1111", s.public, token=t))
    codes = [v for _, v in out]
    assert codes.count("bad_pairing_code") == 5 and codes.count("rate_limited") == 5, codes


def _forged(signer, begun, label_key="sig_hub_channel_register", **change):
    """A registration object signed by the channel key the way a hostile client would: other label or other fields."""
    from orch_hub.contract.canon import cj
    from orch_hub.contract.labels import L

    o = {**begun["challenge"], **change}
    return {"o": o, "sig": b64u(signer._sig(L[label_key] + cj(o)))}


def test_registration_is_signed_under_its_own_label(env):
    u = env.user("ann")
    token = env.sign_in(u)
    s = new_channel()
    begun = env.svc.begin_registration(token, s.public)
    forged = _forged(s, begun, "sig_hub_channel_proof")  # the custodial proof label: a different domain
    refused("bad_signature", env.svc.complete_registration, begun["challenge_id"], forged)
    assert env.svc.channels(u) == []


def test_factor_follows_the_gate_the_hub_checked_not_the_claim(env):
    u = env.user("ann")
    token = env.sign_in(u)
    s = new_channel()
    # a browser session whose key claims a pairing code: not a valid (kind, attestation) pair
    begun = env.svc.begin_registration(token, s.public)
    refused(
        "malformed",
        env.svc.complete_registration,
        begun["challenge_id"],
        _forged(s, begun, attestation="pairing-code"),
    )
    # a phone paired by code that claims the session gate: factor_mismatch, no cert
    code = env.svc.create_pairing_code(u)["code"]
    begun = env.svc.begin_phone_registration(u, code, s.public, token=env.sign_in(u))
    refused(
        "factor_mismatch",
        env.svc.complete_registration,
        begun["challenge_id"],
        _forged(s, begun, channel_kind="browser", attestation="session"),
    )
    assert env.svc.channels(u) == [] and env.keys.channel_certs(u) == []
    # the honest phone path (a fresh code, the first was spent by its begin) gets se-unlocked
    code = env.svc.create_pairing_code(u)["code"]
    begun = env.svc.begin_phone_registration(u, code, s.public, token=env.sign_in(u))
    cert = env.svc.complete_registration(begun["challenge_id"], prove(s, begun))
    assert cert["o"]["factor"] == "se-unlocked"


def test_register_and_revoke_are_on_the_audit_chain(env):
    from orch_hub.contract import audit as AU

    u = env.user("ann")
    _, cert = register(env, u)
    cid = cert["o"]["channel_id"]
    env.svc.revoke(cid)
    env.svc.revoke(cid)  # idempotent: no second record
    rows = env.audit.store.after(0, 100)
    assert [r[1]["o"]["type"] for r in rows] == ["channel", "channel"]
    ev = [r[2] for r in rows]
    assert [e["change"] for e in ev] == ["register", "revoke"]
    assert all(e["channel_id"] == cid and e["user_id"] == u for e in ev)
    assert ev[0]["factor"] == "password+totp" and ev[0]["actor_user_id"] == u
    assert ev[1]["actor_user_id"] is None  # the operator at the terminal
    assert all(r[1]["o"]["scope_tag"] is None for r in rows)  # hub-wide
    for e in ev:
        AU.check_event("channel", e)


def test_pairing_code_alone_is_not_enough_it_needs_a_live_session_of_the_user(env):
    u = env.user("ann")
    v = env.user("bob")
    s = new_channel()
    code = env.svc.create_pairing_code(u)["code"]
    refused("no_session", env.svc.begin_phone_registration, u, code, s.public)  # no token at all
    refused("no_session", env.svc.begin_phone_registration, u, code, s.public, token=env.sign_in(v))  # another user's
    # a begun phone registration dies with the session that opened it
    t = env.sign_in(u)
    begun = env.svc.begin_phone_registration(u, code, s.public, token=t)
    env.svc.sign_out(t)
    refused("no_session", env.svc.complete_registration, begun["challenge_id"], prove(s, begun))
    assert env.svc.channels(u) == [] and env.keys.channel_certs(u) == []


def test_the_challenge_carries_the_key_and_a_foreign_key_is_challenge_invalid(env):
    u = env.user("ann")
    s, other = new_channel(), new_channel()
    begun = env.svc.begin_registration(env.sign_in(u), s.public)
    assert begun["challenge"]["key_pub"] == b64u(s.public)
    # the other key signs the same nonce and expiry: nobody who sees the nonce can race their own key into the gate
    refused(
        "challenge_invalid",
        env.svc.complete_registration,
        begun["challenge_id"],
        _forged(other, begun, key_pub=b64u(other.public)),
    )


def test_a_failing_audit_issues_nothing_and_says_internal(env):
    u = env.user("ann")
    s = new_channel()
    begun = env.svc.begin_registration(env.sign_in(u), s.public)

    def boom(**_kw):
        raise OSError("disk")

    env.svc.audit = type("A", (), {"channel_change": staticmethod(boom)})()
    refused("internal", env.svc.complete_registration, begun["challenge_id"], prove(s, begun))
    assert env.svc.channels(u) == [] and env.keys.channel_certs(u) == []

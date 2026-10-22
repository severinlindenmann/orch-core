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
    refused("bad_proof", env.svc.complete_registration, begun["challenge_id"], b64u(b"\0" * 64))
    # the challenge is single use: even the right proof afterwards is refused
    refused("challenge_invalid", env.svc.complete_registration, begun["challenge_id"], prove(mine, begun))
    # a proof by another key (someone registering a key they do not hold) is refused
    begun = env.svc.begin_registration(token, mine.public)
    refused("bad_proof", env.svc.complete_registration, begun["challenge_id"], prove(other, begun))
    # a proof over another challenge does not carry over
    b1 = env.svc.begin_registration(token, mine.public)
    b2 = env.svc.begin_registration(token, mine.public)
    refused("bad_proof", env.svc.complete_registration, b2["challenge_id"], prove(mine, b1))
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
    refused("channel_exists", env.svc.complete_registration, begun["challenge_id"], prove(s, begun))


def test_phone_pairing_gives_se_unlocked_once_with_code(env):
    u = env.user("ann")
    s = new_channel()
    refused("bad_pairing_code", env.svc.begin_phone_registration, u, "0000-0000-0000", s.public)
    code = env.svc.create_pairing_code(u)["code"]
    v = env.user("bob")
    refused("bad_pairing_code", env.svc.begin_phone_registration, v, code, s.public)  # another user's code
    begun = env.svc.begin_phone_registration(u, code, s.public, "iPhone")
    cert = env.svc.complete_registration(begun["challenge_id"], prove(s, begun))
    assert cert["o"]["factor"] == "se-unlocked"
    H.verify_channel_cert(cert, held(env), now_ms=env.clock(), dev_mode=True)
    refused("bad_pairing_code", env.svc.begin_phone_registration, u, code, new_channel().public)  # pairs once


def test_pairing_attempts_are_rate_limited_and_code_expires(env):
    u = env.user("ann")
    s = new_channel()
    code = env.svc.create_pairing_code(u)["code"]
    for _ in range(5):
        refused("bad_pairing_code", env.svc.begin_phone_registration, u, "1111-1111-1111", s.public)
    refused("rate_limited", env.svc.begin_phone_registration, u, code, s.public)
    env.clock.advance(16 * 60_000)
    refused("bad_pairing_code", env.svc.begin_phone_registration, u, code, s.public)  # expired after 10 min


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
        Channels(env.keys, env.acc, db)
    assert "bad_private_file" in str(ei.value)
    db.chmod(0o600)
    env.svc = Channels(env.keys, env.acc, db, now_ms=env.clock)

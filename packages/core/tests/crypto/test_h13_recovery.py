"""H13-0: the host-only Drop recovery envelope (orch-v2-host 11A.16.2 ``recovery_envelope``, D7, Q4).

``ct = nonce (12) || AES-256-GCM(HKDF(WK_e, "", "orch/v2/host-drop-recovery|" || ws || u32(e) || object_id ||
u32(version)), nonce, DEK)`` (the version bound per review F6). It is a typed value, never a §10.3 wrap record: no frame
builder, wrap check, ``cj`` or wire dump accepts it, it never appears in a device-only frame, and it never prints,
pickles or copies its ciphertext (review F1)."""

from __future__ import annotations

import copy
import json
import pickle

import pytest

from orch import canon
from orch.crypto import drop, recovery, suite
from orch.crypto.labels import L
from orch.wsproto import wire

WK = bytes(range(32))
WS = bytes.fromhex("705d40abbb8c1c90354a1acaa94c935c")
OBJ = bytes.fromhex("72ac37c7000839d72774be83101c013f")
DEK = bytes(range(100, 132))


def test_key_derivation_is_the_spec_formula_with_the_version():
    info = b"orch/v2/host-drop-recovery|" + WS + (7).to_bytes(4, "big") + OBJ + (1).to_bytes(4, "big")
    assert L["kdf_host_drop_recovery"] + WS + (7).to_bytes(4, "big") + OBJ + (1).to_bytes(4, "big") == info
    assert recovery.recovery_key(WK, WS, 7, OBJ, 1) == suite.hkdf(WK, b"", info)


def test_seal_and_open_round_trip_with_a_fresh_nonce():
    a = recovery.seal_recovery(WK, WS, 3, OBJ, 1, DEK)
    b = recovery.seal_recovery(WK, WS, 3, OBJ, 1, DEK)
    assert a.ct[:12] != b.ct[:12]  # fresh nonce per envelope
    assert (a.v, a.kind, a.object_id, a.version, a.epoch) == (1, "drop_recovery", OBJ, 1, 3)
    assert len(a.ct) == 12 + 32 + 16
    assert recovery.open_recovery(a, WK, WS, OBJ, 1) == DEK


def test_deterministic_seam_matches_aes_gcm():
    env = recovery._seal_recovery_with_nonce(WK, WS, 3, OBJ, 1, DEK, bytes(12))
    k = recovery.recovery_key(WK, WS, 3, OBJ, 1)
    assert env.ct == bytes(12) + suite.aes_gcm_seal(k, bytes(12), DEK, b"")


def test_open_refuses_another_context():
    env = recovery.seal_recovery(WK, WS, 3, OBJ, 1, DEK)
    with pytest.raises(recovery.RecoveryTagError):  # another epoch's WK
        recovery.open_recovery(env, bytes(32), WS, OBJ, 1)
    with pytest.raises(recovery.RecoveryTagError):  # another workspace
        recovery.open_recovery(env, WK, bytes(16), OBJ, 1)
    with pytest.raises(recovery.RecoveryTagError):  # another object (the envelope names OBJ)
        recovery.open_recovery(env, WK, WS, bytes(16), 1)
    with pytest.raises(recovery.RecoveryTagError):  # another version
        recovery.open_recovery(env, WK, WS, OBJ, 2)
    with pytest.raises(recovery.RecoveryTagError):  # the epoch field changed: the key differs
        recovery.open_recovery(recovery.RecoveryEnvelope(OBJ, 1, 4, env.ct), WK, WS, OBJ, 1)
    with pytest.raises(recovery.RecoveryTagError):  # the version field changed
        recovery.open_recovery(recovery.RecoveryEnvelope(OBJ, 2, 3, env.ct), WK, WS, OBJ, 2)
    with pytest.raises(recovery.RecoveryTagError):  # the object field changed and the caller asks for that object
        recovery.open_recovery(recovery.RecoveryEnvelope(bytes(16), 1, 3, env.ct), WK, WS, bytes(16), 1)


def test_open_refuses_tampering():
    env = recovery.seal_recovery(WK, WS, 3, OBJ, 1, DEK)
    flipped = bytearray(env.ct)
    flipped[-1] ^= 1
    with pytest.raises(recovery.RecoveryTagError):
        recovery.open_recovery(recovery.RecoveryEnvelope(OBJ, 1, 3, bytes(flipped)), WK, WS, OBJ, 1)


@pytest.mark.parametrize(
    "kw",
    [
        dict(object_id=b"short"),
        dict(version=0),
        dict(version=2**32),
        dict(epoch=-1),
        dict(epoch=2**32),
        dict(epoch=True),
        dict(ct=b"x" * 59),
        dict(ct=b"x" * 61),
        dict(ct="text"),
        dict(ct=b""),
    ],
)
def test_envelope_fields_are_typed(kw):
    good = dict(object_id=OBJ, version=1, epoch=3, ct=bytes(60))
    good.update(kw)
    with pytest.raises(recovery.RecoveryError):
        recovery.RecoveryEnvelope(**good)


def test_inputs_are_checked_when_sealing():
    for bad in (
        dict(dek=b"x" * 31),
        dict(wk=b"x" * 31),
        dict(ws=b"x" * 15),
        dict(object_id=b"x" * 17),
        dict(version=0),
    ):
        args = dict(wk=WK, ws=WS, epoch=1, object_id=OBJ, version=1, dek=DEK)
        args.update(bad)
        with pytest.raises(recovery.RecoveryError):
            recovery.seal_recovery(**args)


def test_host_storage_round_trip_and_strictness():
    env = recovery.seal_recovery(WK, WS, 3, OBJ, 1, DEK)
    raw = env.to_host_bytes()
    assert canon.loads_strict(raw) == {
        "v": 1,
        "kind": "drop_recovery",
        "object_id": OBJ.hex(),
        "version": 1,
        "epoch": 3,
        "ct": env.ct.hex(),
    }
    assert recovery.RecoveryEnvelope.from_host_bytes(raw) == env
    for bad in (
        raw.replace(b'"v":1', b'"v":2'),
        raw.replace(b"drop_recovery", b"drop_recoverx"),
        raw[:-1] + b',"x":1}',
        raw[:-1],
        b"[]",
    ):
        with pytest.raises(recovery.RecoveryError):
            recovery.RecoveryEnvelope.from_host_bytes(bad)


def test_the_envelope_is_never_json_or_a_wrap_record():
    env = recovery.seal_recovery(WK, WS, 3, OBJ, 1, DEK)
    with pytest.raises(canon.JcsError):
        canon.dumps(env)
    with pytest.raises((wire.WireError, canon.JcsError, TypeError, ValueError)):
        wire.dumps_body(env, cap=1 << 20)
    with pytest.raises(drop.DropError):
        drop.check_wrap_record(env)
    with pytest.raises(drop.DropError):  # its host storage form is JSON, but not a wrap record either
        drop.check_wrap_record(canon.loads_strict(env.to_host_bytes()))
    assert not isinstance(env, dict)
    with pytest.raises(TypeError):
        dict(env)  # not a mapping


def test_the_envelope_never_prints_pickles_or_copies_its_ciphertext():
    """Review F1: ``ct`` opens under a key every member device can derive, so it must not leak by accident."""
    env = recovery.seal_recovery(WK, WS, 3, OBJ, 1, DEK)
    secrets_ = (env.ct.hex(), repr(env.ct), env.ct.hex()[:16], str(len(env.ct)))
    for text in (repr(env), str(env), f"{env}", f"{env!r:>200}", format(env, ""), json.dumps(env, default=str)):
        assert "redacted" in text
        assert not any(s in text for s in secrets_), text
    assert json.dumps({"e": env}, default=str) == json.dumps({"e": repr(env)})
    assert env.__json__() == repr(env)
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError):
            pickle.dumps(env, protocol=proto)
    for fn in (copy.copy, copy.deepcopy, lambda e: e.__getstate__()):
        with pytest.raises(TypeError):
            fn(env)
    import dataclasses

    assert not dataclasses.is_dataclass(env)
    with pytest.raises(TypeError):
        vars(env)


def test_the_envelope_is_immutable():
    env = recovery.seal_recovery(WK, WS, 3, OBJ, 1, DEK)
    with pytest.raises(AttributeError):
        env.epoch = 9  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del env.ct

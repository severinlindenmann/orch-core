"""H13-0: the host-only Drop recovery envelope (orch-v2-host 11A.16.2 ``recovery_envelope``, D7, Q4).

``ct = nonce (12) || AES-256-GCM(HKDF(WK_e, "", "orch/v2/host-drop-recovery|" || ws || u32(e) || object_id), nonce,
DEK)``. It is a typed value, never a §10.3 wrap record: no frame builder, wrap check, ``cj`` or wire dump accepts it,
and it never appears in a device-only frame."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from orch import canon
from orch.crypto import drop, recovery, suite
from orch.crypto.labels import L
from orch.relay import wire

WK = bytes(range(32))
WS = bytes.fromhex("705d40abbb8c1c90354a1acaa94c935c")
OBJ = bytes.fromhex("72ac37c7000839d72774be83101c013f")
DEK = bytes(range(100, 132))


def test_key_derivation_is_the_spec_formula():
    info = b"orch/v2/host-drop-recovery|" + WS + (7).to_bytes(4, "big") + OBJ
    assert L["kdf_host_drop_recovery"] + WS + (7).to_bytes(4, "big") + OBJ == info
    assert recovery.recovery_key(WK, WS, 7, OBJ) == suite.hkdf(WK, b"", info)


def test_seal_and_open_round_trip_with_a_fresh_nonce():
    a = recovery.seal_recovery(WK, WS, 3, OBJ, DEK)
    b = recovery.seal_recovery(WK, WS, 3, OBJ, DEK)
    assert a.ct[:12] != b.ct[:12]  # fresh nonce per envelope
    assert (a.v, a.kind, a.object_id, a.epoch) == (1, "drop_recovery", OBJ, 3)
    assert len(a.ct) == 12 + 32 + 16
    assert recovery.open_recovery(a, WK, WS, OBJ) == DEK


def test_deterministic_seam_matches_aes_gcm():
    env = recovery._seal_recovery_with_nonce(WK, WS, 3, OBJ, DEK, bytes(12))
    k = recovery.recovery_key(WK, WS, 3, OBJ)
    assert env.ct == bytes(12) + suite.aes_gcm_seal(k, bytes(12), DEK, b"")


def test_open_refuses_another_context():
    env = recovery.seal_recovery(WK, WS, 3, OBJ, DEK)
    with pytest.raises(InvalidTag):  # another epoch's WK
        recovery.open_recovery(env, bytes(32), WS, OBJ)
    with pytest.raises(InvalidTag):  # another workspace
        recovery.open_recovery(env, WK, bytes(16), OBJ)
    with pytest.raises(InvalidTag):  # another object (the envelope names OBJ)
        recovery.open_recovery(env, WK, WS, bytes(16))
    with pytest.raises(InvalidTag):  # the epoch field changed: the key differs
        recovery.open_recovery(recovery.RecoveryEnvelope(OBJ, 4, env.ct), WK, WS, OBJ)
    moved = recovery.RecoveryEnvelope(bytes(16), 3, env.ct)
    with pytest.raises(InvalidTag):  # the object field changed and the caller asks for that object
        recovery.open_recovery(moved, WK, WS, bytes(16))


@pytest.mark.parametrize("cut", [0, 1, 12, 27, 59])
def test_open_refuses_truncation_and_tampering(cut):
    env = recovery.seal_recovery(WK, WS, 3, OBJ, DEK)
    short = recovery.RecoveryEnvelope.__new__(recovery.RecoveryEnvelope)
    object.__setattr__(short, "object_id", OBJ)
    object.__setattr__(short, "epoch", 3)
    object.__setattr__(short, "ct", env.ct[:cut])
    with pytest.raises((InvalidTag, recovery.RecoveryError)):
        recovery.open_recovery(short, WK, WS, OBJ)
    flipped = bytearray(env.ct)
    flipped[-1] ^= 1
    with pytest.raises(InvalidTag):
        recovery.open_recovery(recovery.RecoveryEnvelope(OBJ, 3, bytes(flipped)), WK, WS, OBJ)


@pytest.mark.parametrize(
    "kw",
    [
        dict(object_id=b"short"),
        dict(epoch=-1),
        dict(epoch=2**32),
        dict(epoch=True),
        dict(ct=b"x" * 59),
        dict(ct=b"x" * 61),
        dict(ct="text"),
    ],
)
def test_envelope_fields_are_typed(kw):
    good = dict(object_id=OBJ, epoch=3, ct=bytes(60))
    good.update(kw)
    with pytest.raises(recovery.RecoveryError):
        recovery.RecoveryEnvelope(**good)


def test_inputs_are_checked_when_sealing():
    for bad in (dict(dek=b"x" * 31), dict(wk=b"x" * 31), dict(ws=b"x" * 15), dict(object_id=b"x" * 17)):
        args = dict(wk=WK, ws=WS, epoch=1, object_id=OBJ, dek=DEK)
        args.update(bad)
        with pytest.raises(recovery.RecoveryError):
            recovery.seal_recovery(**args)


def test_host_storage_round_trip_and_strictness():
    env = recovery.seal_recovery(WK, WS, 3, OBJ, DEK)
    raw = env.to_host_bytes()
    assert canon.loads_strict(raw) == {
        "v": 1,
        "kind": "drop_recovery",
        "object_id": OBJ.hex(),
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
    env = recovery.seal_recovery(WK, WS, 3, OBJ, DEK)
    with pytest.raises(canon.JcsError):
        canon.dumps(env)
    with pytest.raises((wire.WireError, canon.JcsError, TypeError, ValueError)):
        wire.dumps_body(env, cap=1 << 20)
    with pytest.raises(drop.DropError):
        drop.check_wrap_record(env)
    # its host storage form is JSON, but not a wrap record either
    with pytest.raises(drop.DropError):
        drop.check_wrap_record(canon.loads_strict(env.to_host_bytes()))
    assert not isinstance(env, dict)
    with pytest.raises(TypeError):
        dict(env)  # not a mapping


def test_the_envelope_is_immutable():
    env = recovery.seal_recovery(WK, WS, 3, OBJ, DEK)
    with pytest.raises(AttributeError):
        env.epoch = 9  # type: ignore[misc]

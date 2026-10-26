"""The host-only Drop recovery envelope (orch-v2-host 11A.16.2 ``_PushSealer.recovery_envelope``, D7, Q4; H13-0).

Every frame the host writes gets one, made in the same seal span and stored in ``outbound.sqlite`` ``drop_up.recovery``,
**never in a frame**. A ``stale_epoch`` rewrap and a re-framing after a narrowed audience recover the DEK from it::

    K  = HKDF(WK_e, salt = "", info = "orch/v2/host-drop-recovery|" || ws (16) || u32(e) || object_id (16)
              || u32(version))
    ct = nonce (12, fresh) || AES-256-GCM(K, nonce, DEK, aad = "")

Decisions:

* **Version bound** (review F6): the key binds the object version too, beyond 11A.16.2's formula; files are version
  1 today, documents later would otherwise share one key across versions.
* **Never leaks by accident** (review F1): repr, str, format, ``json.dumps(default=str)``, pickle and copy see only a
  redacted marker or raise (:class:`orch.crypto._secret.Secret`).
* **Typed, not a record.** :class:`RecoveryEnvelope` is an immutable slotted class, not a mapping: ``cj``, the wire dump
  and :func:`orch.crypto.drop.check_wrap_record` all refuse it, and its host storage form (:meth:`to_host_bytes`) has
  a field set no §10.3 wrap record has. So it can never be serialized into a relay frame (a test pins each path).
* **No AAD.** The context (workspace, epoch, object) is bound by the key derivation, which is unique per envelope
  context; the nonce is fresh per seal, so one ``(K, nonce)`` pair never repeats in practice (one envelope per frame).
* **The epoch and the WK.** Which ``WK_e`` may be used (a held epoch at or below the committed one, never a burned one)
  is ``BridgeKeys``' check (H13-1). This module only derives and seals; it never reads a key from anywhere.
"""

from __future__ import annotations

import hmac
import secrets

from orch import canon

from . import suite
from ._closed import close_module, closed
from ._secret import Secret
from .labels import L

__all__ = [
    "RecoveryEnvelope", "RecoveryError", "RecoveryTagError", "open_recovery", "recovery_key", "seal_recovery",
]  # fmt: skip

NONCE_LEN = 12
CT_LEN = NONCE_LEN + 32 + 16
_FIELDS = frozenset({"v", "kind", "object_id", "version", "epoch", "ct"})
_MAX_HOST_BYTES = 512


class RecoveryError(Exception):
    """The one error of this module: a malformed envelope or input, or (as :class:`RecoveryTagError`) a failed
    authentication. Not a ``ValueError``."""


class RecoveryTagError(RecoveryError):
    """The AEAD tag failed: another workspace, epoch, object or key, or a tampered or truncated envelope."""


def _u32(value: object, low: int = 0) -> int:
    if type(value) is not int or not low <= value < 2**32:
        raise RecoveryError(f"a u32 >= {low} expected")
    return value


def _bytes(value: object, n: int, what: str) -> bytes:
    if type(value) is not bytes or len(value) != n:
        raise RecoveryError(f"{what} is {n} bytes")
    return value


class RecoveryEnvelope(Secret):
    """``{v: 1, kind: "drop_recovery", object_id, version, epoch, ct}``: host-local, never sent (D7). It never prints,
    formats, pickles or copies ``ct`` (review F1: ``ct`` opens under a key every member device can derive)."""

    __slots__ = ("object_id", "version", "epoch", "ct")
    v = 1
    kind = "drop_recovery"
    object_id: bytes
    version: int
    epoch: int
    ct: bytes

    def __init__(self, object_id: bytes, version: int, epoch: int, ct: bytes) -> None:
        _bytes(object_id, 16, "object_id")
        _u32(version, 1)
        _u32(epoch)
        _bytes(ct, CT_LEN, "ct")
        self._set(object_id=object_id, version=version, epoch=epoch, ct=ct)

    def __eq__(self, other: object) -> bool:
        if type(other) is not RecoveryEnvelope:
            return NotImplemented
        same = (self.object_id, self.version, self.epoch) == (other.object_id, other.version, other.epoch)
        return hmac.compare_digest(self.ct, other.ct) and same

    def to_host_bytes(self) -> bytes:
        """The host storage form (``drop_up.recovery``): canonical JSON, hex fields. Not a wrap record."""
        return canon.dumps(
            {
                "v": self.v,
                "kind": self.kind,
                "object_id": self.object_id.hex(),
                "version": self.version,
                "epoch": self.epoch,
                "ct": self.ct.hex(),
            }
        )

    @classmethod
    def from_host_bytes(cls, raw: bytes) -> RecoveryEnvelope:
        return _from_host_bytes(cls, raw)


@closed(RecoveryError, RecoveryError, RecoveryTagError)
def _from_host_bytes(cls: type[RecoveryEnvelope], raw: bytes) -> RecoveryEnvelope:
    if type(raw) is not bytes or len(raw) > _MAX_HOST_BYTES:
        raise RecoveryError("not a stored recovery envelope")
    try:
        o = canon.loads_strict(raw)
    except canon.JcsError:
        raise RecoveryError("not a stored recovery envelope") from None
    if type(o) is not dict or set(o) != _FIELDS or o["v"] != cls.v or o["kind"] != cls.kind:
        raise RecoveryError("not a stored recovery envelope")
    if canon.dumps(o) != raw:
        raise RecoveryError("not canonical")
    return cls(_unhex(o["object_id"], 16), o["version"], o["epoch"], _unhex(o["ct"], CT_LEN))


def _unhex(s: object, n: int) -> bytes:
    from .encoding import EncodingError, unhex

    try:
        return unhex(s, n)
    except EncodingError:
        raise RecoveryError("bad hex field") from None


def recovery_key(wk: bytes, ws: bytes, epoch: int, object_id: bytes, version: int) -> bytes:
    """``HKDF(WK_e, "", label || ws || u32(e) || object_id || u32(version))`` (the version bound, review F6)."""
    _bytes(wk, 32, "WK")
    info = L["kdf_host_drop_recovery"] + _bytes(ws, 16, "workspace id") + _u32(epoch).to_bytes(4, "big")
    info += _bytes(object_id, 16, "object_id") + _u32(version, 1).to_bytes(4, "big")
    return suite.hkdf(wk, b"", info)


def _seal_recovery_with_nonce(
    wk: bytes, ws: bytes, epoch: int, object_id: bytes, version: int, dek: bytes, nonce: bytes
) -> RecoveryEnvelope:
    """Test seam (a fixed nonce). Never call outside tests or outside this module."""
    _bytes(dek, 32, "DEK")
    _bytes(nonce, NONCE_LEN, "nonce")
    k = recovery_key(wk, ws, epoch, object_id, version)
    return RecoveryEnvelope(object_id, version, epoch, nonce + suite.aes_gcm_seal(k, nonce, dek, b""))


def seal_recovery(wk: bytes, ws: bytes, epoch: int, object_id: bytes, version: int, dek: bytes) -> RecoveryEnvelope:
    """The envelope for ``dek`` of ``(object_id, version)`` under ``WK_epoch`` of ``ws``, with a fresh nonce."""
    return _seal_recovery_with_nonce(wk, ws, epoch, object_id, version, dek, secrets.token_bytes(NONCE_LEN))


def open_recovery(env: RecoveryEnvelope, wk: bytes, ws: bytes, object_id: bytes, version: int) -> bytes:
    """The DEK. ``(object_id, version)`` is what the caller recovers for; an envelope of another object, version,
    workspace, epoch or key, or a tampered one, raises :class:`RecoveryTagError`."""
    if type(env) is not RecoveryEnvelope:
        raise RecoveryError("not a recovery envelope")
    if env.object_id != object_id or env.version != version:
        raise RecoveryTagError("not this version's envelope")
    k = recovery_key(wk, ws, env.epoch, object_id, version)
    return suite.aes_gcm_open(k, env.ct[:NONCE_LEN], env.ct[NONCE_LEN:], b"")


close_module(globals(), RecoveryError, RecoveryError, RecoveryTagError)

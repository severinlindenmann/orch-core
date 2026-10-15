"""The host-only Drop recovery envelope (orch-v2-host 11A.16.2 ``_PushSealer.recovery_envelope``, D7, Q4; H13-0).

Every frame the host writes gets one, made in the same seal span and stored in ``outbound.sqlite`` ``drop_up.recovery``,
**never in a frame**. A ``stale_epoch`` rewrap and a re-framing after a narrowed audience recover the DEK from it::

    K  = HKDF(WK_e, salt = "", info = "orch/v2/host-drop-recovery|" || ws (16) || u32(e) || object_id (16))
    ct = nonce (12, fresh) || AES-256-GCM(K, nonce, DEK, aad = "")

Decisions:

* **Typed, not a record.** :class:`RecoveryEnvelope` is a frozen slotted class, not a mapping: ``cj``, the wire dump
  and :func:`orch.crypto.drop.check_wrap_record` all refuse it, and its host storage form (:meth:`to_host_bytes`) has
  a field set no §10.3 wrap record has. So it can never be serialized into a relay frame (a test pins each path).
* **No AAD.** The context (workspace, epoch, object) is bound by the key derivation, which is unique per envelope
  context; the nonce is fresh per seal, so one ``(K, nonce)`` pair never repeats in practice (one envelope per frame).
* **The epoch and the WK.** Which ``WK_e`` may be used (a held epoch at or below the committed one, never a burned one)
  is ``BridgeKeys``' check (H13-1). This module only derives and seals; it never reads a key from anywhere.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import ClassVar

from orch import canon

from . import suite
from ._closed import close_module, closed
from .labels import L

__all__ = [
    "RecoveryEnvelope", "RecoveryError", "RecoveryTagError", "open_recovery", "recovery_key", "seal_recovery",
]  # fmt: skip

NONCE_LEN = 12
CT_LEN = NONCE_LEN + 32 + 16
_FIELDS = frozenset({"v", "kind", "object_id", "epoch", "ct"})
_MAX_HOST_BYTES = 512


class RecoveryError(Exception):
    """The one error of this module: a malformed envelope or input, or (as :class:`RecoveryTagError`) a failed
    authentication. Not a ``ValueError``."""


class RecoveryTagError(RecoveryError):
    """The AEAD tag failed: another workspace, epoch, object or key, or a tampered or truncated envelope."""


def _u32(epoch: object) -> int:
    if type(epoch) is not int or not 0 <= epoch < 2**32:
        raise RecoveryError("epoch is a u32")
    return epoch


def _bytes(value: object, n: int, what: str) -> bytes:
    if type(value) is not bytes or len(value) != n:
        raise RecoveryError(f"{what} is {n} bytes")
    return value


@dataclass(frozen=True, slots=True)
class RecoveryEnvelope:
    """``{v: 1, kind: "drop_recovery", object_id, epoch, ct}``: host-local, never sent (D7)."""

    object_id: bytes
    epoch: int
    ct: bytes
    v: ClassVar[int] = 1
    kind: ClassVar[str] = "drop_recovery"

    def __post_init__(self) -> None:
        _bytes(self.object_id, 16, "object_id")
        _u32(self.epoch)
        _bytes(self.ct, CT_LEN, "ct")

    def to_host_bytes(self) -> bytes:
        """The host storage form (``drop_up.recovery``): canonical JSON, hex fields. Not a wrap record."""
        return canon.dumps(
            {
                "v": self.v,
                "kind": self.kind,
                "object_id": self.object_id.hex(),
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
    return cls(object_id=_unhex(o["object_id"], 16), epoch=o["epoch"], ct=_unhex(o["ct"], CT_LEN))


def _unhex(s: object, n: int) -> bytes:
    from .encoding import EncodingError, unhex

    try:
        return unhex(s, n)
    except EncodingError:
        raise RecoveryError("bad hex field") from None


def recovery_key(wk: bytes, ws: bytes, epoch: int, object_id: bytes) -> bytes:
    _bytes(wk, 32, "WK")
    info = L["kdf_host_drop_recovery"] + _bytes(ws, 16, "workspace id") + _u32(epoch).to_bytes(4, "big")
    return suite.hkdf(wk, b"", info + _bytes(object_id, 16, "object_id"))


def _seal_recovery_with_nonce(
    wk: bytes, ws: bytes, epoch: int, object_id: bytes, dek: bytes, nonce: bytes
) -> RecoveryEnvelope:
    """Test seam (a fixed nonce). Never call outside tests."""
    _bytes(dek, 32, "DEK")
    _bytes(nonce, NONCE_LEN, "nonce")
    k = recovery_key(wk, ws, epoch, object_id)
    return RecoveryEnvelope(object_id=object_id, epoch=epoch, ct=nonce + suite.aes_gcm_seal(k, nonce, dek, b""))


def seal_recovery(wk: bytes, ws: bytes, epoch: int, object_id: bytes, dek: bytes) -> RecoveryEnvelope:
    """The envelope for ``dek`` under ``WK_epoch`` of workspace ``ws``, with a fresh nonce drawn here."""
    return _seal_recovery_with_nonce(wk, ws, epoch, object_id, dek, secrets.token_bytes(NONCE_LEN))


def open_recovery(env: RecoveryEnvelope, wk: bytes, ws: bytes, object_id: bytes) -> bytes:
    """The DEK. ``object_id`` is the object the caller recovers for; an envelope of another object, workspace, epoch
    or key, a truncated or tampered one, raises :class:`RecoveryTagError`."""
    if type(env) is not RecoveryEnvelope:
        raise RecoveryError("not a recovery envelope")
    if type(env.ct) is not bytes or len(env.ct) != CT_LEN or env.object_id != object_id:
        raise RecoveryTagError("not this object's envelope")
    k = recovery_key(wk, ws, env.epoch, object_id)
    return suite.aes_gcm_open(k, env.ct[:NONCE_LEN], env.ct[NONCE_LEN:], b"")


close_module(globals(), RecoveryError, RecoveryError, RecoveryTagError)

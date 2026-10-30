"""Verify a signed release envelope (CONTRACT.md §3). Fails closed; the first failing rule names the reason.

The verifier never canonicalises: signatures are checked over the exact decoded bytes, and those bytes are then
parsed and refused unless already canonical. Stored state (``seq_seen``, ``revocations_issued_at``) is passed in and
the caller stores the values of :class:`Verified` only on success.

``seq_seen`` and ``revocations_issued_at`` are required keywords (no default): every update-path caller (install.sh's
verifier, ``orch update``) MUST persist the highest verified seq per channel and pass it back, or a rolled-back
channel pointer is accepted as long as its old envelope is still unexpired. ``None`` means "first install, nothing
seen" and is only correct then (P2-C/P2-D persist it in the install record).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .envelope import (
    Malformed,
    channel_allowed,
    check_catalog,
    check_cert,
    check_manifest,
    check_revocations,
    parse_canonical,
    parse_envelope,
    parse_time,
)
from .keys import embedded_roots, test_hook_active

__all__ = ["Reason", "UpdateRefused", "Verified", "verify_envelope"]

CERT_DOMAIN = b"orch-release-key/1\n"
REVOCATIONS_DOMAIN = b"orch-revocations/1\n"
PAYLOAD_DOMAIN = {"core": b"orch-release/1\n", "addon-catalog": b"orch-addon-catalog/1\n"}
PAYLOAD_FORMAT = {"core": "orch.release/1", "addon-catalog": "orch.addon-catalog/1"}

UNVERIFIED = "update.unverified"
STALE = "update.stale"


class Reason(StrEnum):
    MALFORMED = "malformed"
    UNKNOWN_ROOT = "unknown_root"
    BAD_CERT_SIG = "bad_cert_sig"
    BAD_REVOCATIONS_SIG = "bad_revocations_sig"
    REVOCATIONS_ROLLBACK = "revocations_rollback"
    WRONG_PURPOSE = "wrong_purpose"
    CHANNEL_NOT_ALLOWED = "channel_not_allowed"
    CERT_NOT_YET_VALID = "cert_not_yet_valid"
    CERT_EXPIRED = "cert_expired"
    KEY_REVOKED = "key_revoked"
    KEY_MISMATCH = "key_mismatch"
    BAD_SIG = "bad_sig"
    WRONG_FORMAT = "wrong_format"
    WRONG_CHANNEL = "wrong_channel"
    EXPIRED = "expired"
    ROLLBACK = "rollback"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"

    @property
    def code(self) -> str:
        """The catalog code (CONTRACT.md §4): stale for rollback/expiry, unverified for everything else."""
        return STALE if self in _STALE else UNVERIFIED


_STALE = frozenset({Reason.REVOCATIONS_ROLLBACK, Reason.EXPIRED, Reason.ROLLBACK})


class UpdateRefused(Exception):
    def __init__(self, reason: Reason) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.code = reason.code


@dataclass(frozen=True)
class Verified:
    """What the caller may store on success, plus the verified document (parsed from the verified bytes)."""

    channel: str
    seq: int
    version: str | None  # None for a catalog
    min_serial: int
    revocations_issued_at: str
    document: dict[str, Any]


def _sig_ok(pub: bytes, sig: bytes, msg: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
    except (InvalidSignature, ValueError):
        return False
    return True


def _refuse(reason: Reason) -> UpdateRefused:
    return UpdateRefused(reason)


def verify_envelope(
    envelope: bytes,
    *,
    purpose: str,
    channel: str,
    now: datetime,
    seq_seen: int | None,
    revocations_issued_at: str | None,
    files: Mapping[str, bytes] | None = None,
    roots: Mapping[str, bytes] | None = None,
    allow_expired_manifest: bool = False,
) -> Verified:
    """Run CONTRACT.md §3 in order. Raises :class:`UpdateRefused` (``.reason``, ``.code``); returns :class:`Verified`.

    ``roots`` defaults to the embedded production roots; only tests pass anything else (or enter
    ``keys.enable_test_root_for_tests``). ``files`` (name to bytes) is step 7 and core-only: leave it out for
    ``--check``. ``allow_expired_manifest`` skips ONLY the payload ``expires_at`` check (step 6), for the release
    refresh that re-signs a pointer to an expired manifest; every other check still runs in order. Default False.
    """
    if purpose not in PAYLOAD_DOMAIN:
        raise ValueError(f"unknown purpose {purpose!r}")
    if files is not None and purpose != "core":
        raise ValueError("files are checked for core releases only")
    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    trusted = embedded_roots() if roots is None else roots
    try:
        return _verify(
            envelope, purpose, channel, now, seq_seen, revocations_issued_at, files, trusted, allow_expired_manifest
        )
    except (Malformed, RecursionError):
        raise _refuse(Reason.MALFORMED) from None


def _verify(
    envelope: bytes,
    purpose: str,
    channel: str,
    now: datetime,
    seq_seen: int | None,
    stored_revocations_at: str | None,
    files: Mapping[str, bytes] | None,
    trusted: Mapping[str, bytes],
    allow_expired_manifest: bool,
) -> Verified:
    # 1 outer envelope (all base64 and key-set rules are inside parse_envelope)
    env = parse_envelope(envelope)

    # 2 certificate: trusted root, root signature, then canonical and well formed
    root = trusted.get(env.cert_root_key_id)
    if root is None:
        raise _refuse(Reason.UNKNOWN_ROOT)
    if not _sig_ok(root, env.cert_sig, CERT_DOMAIN + env.cert_bytes):
        raise _refuse(Reason.BAD_CERT_SIG)
    cert = parse_canonical(env.cert_bytes)
    cert_pub = check_cert(cert)

    # 3 revocation list
    rroot = trusted.get(env.revocations_root_key_id)
    if rroot is None:
        raise _refuse(Reason.UNKNOWN_ROOT)
    if not _sig_ok(rroot, env.revocations_sig, REVOCATIONS_DOMAIN + env.revocations_bytes):
        raise _refuse(Reason.BAD_REVOCATIONS_SIG)
    revs = parse_canonical(env.revocations_bytes)
    check_revocations(revs)
    if stored_revocations_at is not None and parse_time(revs["issued_at"]) < parse_time(stored_revocations_at):
        raise _refuse(Reason.REVOCATIONS_ROLLBACK)

    # 4 certificate rules
    if cert["purpose"] != purpose:
        raise _refuse(Reason.WRONG_PURPOSE)
    if not channel_allowed(cert["channels"], channel) or (channel.startswith("test-") and not test_hook_active()):
        raise _refuse(Reason.CHANNEL_NOT_ALLOWED)
    if now < parse_time(cert["not_before"]):
        raise _refuse(Reason.CERT_NOT_YET_VALID)
    if now >= parse_time(cert["not_after"]):
        raise _refuse(Reason.CERT_EXPIRED)
    if cert["serial"] < revs["min_serial"] or cert["serial"] in revs["revoked"]:
        raise _refuse(Reason.KEY_REVOKED)

    # 5 envelope signature over domain + the exact payload bytes
    if env.sig_key_id != cert["key_id"]:
        raise _refuse(Reason.KEY_MISMATCH)
    if not _sig_ok(cert_pub, env.sig_value, PAYLOAD_DOMAIN[purpose] + env.payload):
        raise _refuse(Reason.BAD_SIG)

    # 6 payload
    doc = parse_canonical(env.payload)
    if doc.get("format") != PAYLOAD_FORMAT[purpose]:
        raise _refuse(Reason.WRONG_FORMAT)
    (check_manifest if purpose == "core" else check_catalog)(doc)
    if doc["channel"] != channel:
        raise _refuse(Reason.WRONG_CHANNEL)
    if now >= parse_time(doc["expires_at"]) and not allow_expired_manifest:
        raise _refuse(Reason.EXPIRED)
    if seq_seen is not None and doc["seq"] < seq_seen:
        raise _refuse(Reason.ROLLBACK)

    # 7 downloaded files (core only)
    if files is not None:
        for f in doc["files"]:
            data = files.get(f["name"])
            if data is None or len(data) != f["size"]:
                raise _refuse(Reason.SIZE_MISMATCH)
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise _refuse(Reason.HASH_MISMATCH)

    return Verified(
        channel=doc["channel"],
        seq=doc["seq"],
        version=doc.get("version"),
        min_serial=revs["min_serial"],
        revocations_issued_at=revs["issued_at"],
        document=doc,
    )

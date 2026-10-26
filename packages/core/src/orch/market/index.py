"""The index (``marketplace.json``, ``orch.market/1``): strict parse, the pin and digest pre-checks, the schema, the
semantic rules (spec 3, 5 steps 1 to 3) and the signature (3.1). The answers equal ``tests/addons/market_ref.py``
(a test runs both over the vectors)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from jsonschema import Draft202012Validator

from orch.canon.jcs import JcsError, dumps, loads_strict
from orch.crypto.encoding import EncodingError, unb64u

from . import ADDONS, MarketError, constants
from .url import canonical_market_url, schema

INDEX_LABEL = b"orch/market/1/index|"
ENTRY_LABEL = b"orch/market/1/entry|"
_ORDER_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})(?![\s\S])")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}(?![\s\S])")


def parse(raw: bytes) -> dict[str, Any]:
    """Strict JSON (duplicate keys, floats, NaN, non-ASCII keys, nesting over 16 refused), an object, within the size
    cap. ``market.malformed`` otherwise."""
    if not isinstance(raw, bytes) or len(raw) > constants.MAX_INDEX_BYTES:
        raise MarketError("market.malformed")
    try:
        doc = loads_strict(raw)
    except (JcsError, ValueError):
        raise MarketError("market.malformed") from None
    if not isinstance(doc, dict):
        raise MarketError("market.malformed")
    return doc


def check(doc: Any) -> dict[str, Any]:
    """Step 1 of spec 5: pins and digests first, then the schema, canonical URLs, unique ids, sorted capabilities."""
    if not isinstance(doc, dict) or not isinstance(doc.get(ADDONS), list):
        raise MarketError("market.malformed")
    for e in doc[ADDONS]:
        if isinstance(e, dict):
            if not isinstance(e.get("commit"), str) or _COMMIT.fullmatch(e["commit"]) is None:
                raise MarketError("market.unpinned")
            if not isinstance(e.get("package_sha256"), str) or _DIGEST.fullmatch(e["package_sha256"]) is None:
                raise MarketError("market.bad_digest_format")
    if next(Draft202012Validator(schema()).iter_errors(doc), None) is not None:
        raise MarketError("market.malformed")
    for u in [doc["source"]["git"], *(e["source"]["git"] for e in doc[ADDONS])]:
        canonical_market_url(u)
    ids = [e["id"] for e in doc[ADDONS]]
    if len(set(ids)) != len(ids):
        raise MarketError("market.duplicate_id")
    if any(e["capabilities"] != sorted(e["capabilities"]) for e in doc[ADDONS]):
        raise MarketError("market.malformed")
    return doc


def signed_message(doc: dict[str, Any]) -> bytes:
    """What the official key signs (3.1): the URL, version, expiry, every (id, version, digest) and the revoked list.
    Titles and summaries are display text and not covered."""
    body = {
        "market": doc["source"]["git"],
        "version": doc["version"],
        "expires_at": doc.get("expires_at"),
        "entries": [[e["id"], e["version"], e["package_sha256"]] for e in sorted(doc[ADDONS], key=lambda e: e["id"])],
        "revoked": sorted(doc.get("revoked", [])),
    }
    return INDEX_LABEL + dumps(body)


def _verify(key_b64: str, sig_b64: str, message: bytes) -> bool:
    try:
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(key_b64, 65))
        raw = unb64u(sig_b64, 64)
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        if not (1 <= r < _ORDER_N and 1 <= s < _ORDER_N):
            return False
        key.verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, EncodingError, ValueError):
        return False


def entry_message(entry: dict[str, Any], market: str) -> bytes:
    """What a publisher signs (4.3): the label, the entry with ``publisher`` reduced to its key, and the URL of the
    marketplace it is published in (so a signed entry copied into another marketplace does not verify there)."""
    reduced = {**entry, "publisher": {"key": entry["publisher"]["key"]}}
    return ENTRY_LABEL + dumps({"entry": reduced, "market": market})


def verify_entry(entry: dict[str, Any], market: str) -> bool:
    return _verify(entry["publisher"]["key"], entry["publisher"]["sig"], entry_message(entry, market))


def official_keys() -> tuple[str, ...]:
    """The only verifier input for the official index: the constant of this release, looked up here at call time (so
    a test can replace the constant; nothing else, no environment, file or flag, can)."""
    return constants.OFFICIAL_KEYS


def verify(doc: dict[str, Any], url: str, *, official: bool, keys: tuple[str, ...] | None = None) -> bool:
    """Step 2 of spec 5, before anything is taken from the index. Returns whether a signature was present and
    verified. ``market.source_mismatch`` if the index is not the one added from ``url``. Official: ``expires_at`` and a
    signature by a key of :func:`official_keys` are required (``market.index_unsigned``). Other: a present signature
    must verify against its own key (an unreachable path before #287; kept minimal). ``keys`` replaces the official key
    list for the sandbox's test marketplace (class ``dev``, V3 G3 C5): the same rules, other keys."""
    if doc["source"]["git"] != url:
        raise MarketError("market.source_mismatch")
    sig = doc.get("signature")
    if official:
        if (
            sig is None
            or "expires_at" not in doc
            or sig["key"] not in (official_keys() if keys is None else keys)
            or not _verify(sig["key"], sig["sig"], signed_message(doc))
        ):
            raise MarketError("market.index_unsigned")
        return True
    if sig is None:
        return False
    if not _verify(sig["key"], sig["sig"], signed_message(doc)):
        raise MarketError("market.index_unsigned")
    return True


def is_stale(doc: dict[str, Any], now: float) -> bool:
    return "expires_at" in doc and now > doc["expires_at"]


@dataclass(frozen=True)
class Verdict:
    doc: dict[str, Any]
    verified: bool  # a signature was present and verified: only then may anything be recorded as a floor
    stale: bool
    payload_sha256: str  # sha256 of the signed message: what "the same index version" means


def evaluate(raw: bytes, url: str, *, official: bool, now: float, keys: tuple[str, ...] | None = None) -> Verdict:
    """Steps 1 and 2 of spec 5 on the exact bytes, in order: parse, pins and schema, URL, signature."""
    doc = check(parse(raw))
    verified = verify(doc, url, official=official, keys=keys)
    return Verdict(doc, verified, is_stale(doc, now), hashlib.sha256(signed_message(doc)).hexdigest())


def check_floor(last_version: int | None, last_payload: str | None, v: Verdict) -> None:
    """Step 3: ``market.rollback`` if a verified index is older than the last verified one, or has the same version
    with a different signed payload. An unverified index has no floor to compare with and records none (E2)."""
    if not v.verified or last_version is None:
        return
    version = v.doc["version"]
    if version < last_version or (
        version == last_version and last_payload is not None and v.payload_sha256 != last_payload
    ):
        raise MarketError("market.rollback")

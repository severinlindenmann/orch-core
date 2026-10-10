"""Hash helpers and ``hash_v`` versioning.

Defined by ``docs/architecture/orch-v2-ticket-format.md`` §5 (events, "Gate hash"):

* hash strings are ``"sha256:" + 64 lower-case hex`` (:func:`format_hash`, :func:`parse_hash`);
* ``gate hash = sha256(JCS({workspace_id, uid, gate, schema, hash_v, sections: {name: normalised text},
  fields, policy_hash, people_hash}))`` (:func:`gate_hash`), with JCS as :func:`orch.canon.jcs.dumps`;
* ``hash_v`` names the hashing recipe. Only version 1 exists; every other value (and any non-int) is refused
  (:func:`check_hash_v`), so a future recipe can never be confused with this one.

The format defines no domain-separation label for the gate hash, so none is added.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from . import jcs
from .text import normalize_text

HASH_V = 1
SUPPORTED_HASH_V = frozenset({1})
PREFIX = "sha256:"

# Top-level keys of the version-1 gate hash input, exactly as listed in ticket-format §5.
GATE_KEYS_V1 = frozenset(
    {"workspace_id", "uid", "gate", "schema", "hash_v", "sections", "fields", "policy_hash", "people_hash"}
)

_HEX64 = re.compile(r"[0-9a-f]{64}")

__all__ = [
    "GATE_KEYS_V1",
    "HASH_V",
    "PREFIX",
    "SUPPORTED_HASH_V",
    "HashError",
    "check_hash_v",
    "format_hash",
    "gate_hash",
    "hash_jcs",
    "parse_hash",
    "section_hash",
    "sha256_hex",
]


class HashError(ValueError):
    """Unknown hash_v, malformed hash string, or a malformed hash input."""


def check_hash_v(hash_v: object) -> int:
    if type(hash_v) is not int or hash_v not in SUPPORTED_HASH_V:
        raise HashError(f"unsupported hash_v {hash_v!r}")
    return hash_v


def sha256_hex(data: bytes) -> str:
    """Bare lower-case hex SHA-256 (the form used by artifact ``sha256`` fields)."""
    return hashlib.sha256(data).hexdigest()


def format_hash(digest_hex: str) -> str:
    if not _HEX64.fullmatch(digest_hex):
        raise HashError("digest must be 64 lower-case hex characters")
    return PREFIX + digest_hex


def parse_hash(s: str) -> bytes:
    """Inverse of :func:`format_hash`: return the 32 digest bytes; refuse anything not exactly canonical."""
    if not isinstance(s, str) or not s.startswith(PREFIX) or not _HEX64.fullmatch(s[len(PREFIX) :]):
        raise HashError("not a canonical 'sha256:<64 hex>' hash string")
    return bytes.fromhex(s[len(PREFIX) :])


def hash_jcs(obj: Any) -> str:
    """``sha256:`` hash of the canonical JSON (``cj``) of ``obj``."""
    return format_hash(sha256_hex(jcs.dumps(obj)))


def section_hash(text: str, hash_v: int = HASH_V) -> str:
    """Hash of one prose section: sha256 of the UTF-8 of its normalised text (an assumption, see PR notes)."""
    check_hash_v(hash_v)
    return format_hash(sha256_hex(normalize_text(text).encode("utf-8")))


def gate_hash(payload: dict[str, Any]) -> str:
    """Gate hash of ticket-format §5. ``payload["sections"]`` texts are normalised here; the rest is hashed as is.

    The key set must be exactly :data:`GATE_KEYS_V1`; a different set means a different recipe and a new hash_v.
    """
    if not isinstance(payload, dict):
        raise HashError("gate hash input must be an object")
    check_hash_v(payload.get("hash_v"))
    if set(payload) != GATE_KEYS_V1:
        raise HashError(f"gate hash input keys must be exactly {sorted(GATE_KEYS_V1)}")
    sections = payload["sections"]
    if not isinstance(sections, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in sections.items()
    ):
        raise HashError("sections must map names to text")
    norm = {**payload, "sections": {k: normalize_text(v) for k, v in sections.items()}}
    try:
        return hash_jcs(norm)
    except jcs.JcsError as e:
        raise HashError(str(e)) from None

"""JCS (RFC 8785), NFC/LF text normalisation, hash helpers (hash_v).

See ``docs/architecture/orch-v2-core.md`` §2, ``orch-v2-ticket-format.md`` §3/§5 and
``orch-relay/docs/protocol-v2.md`` §2.3.
"""

from .hashing import (
    GATE_KEYS_V1,
    HASH_V,
    SUPPORTED_HASH_V,
    HashError,
    check_hash_v,
    format_hash,
    gate_hash,
    hash_jcs,
    parse_hash,
    section_hash,
    sha256_hex,
)
from .jcs import JcsError, dumps, dumps_general, loads_strict, validate
from .text import is_normalized, normalize_text

__all__ = [
    "GATE_KEYS_V1",
    "HASH_V",
    "SUPPORTED_HASH_V",
    "HashError",
    "JcsError",
    "check_hash_v",
    "dumps",
    "dumps_general",
    "format_hash",
    "gate_hash",
    "hash_jcs",
    "is_normalized",
    "loads_strict",
    "normalize_text",
    "parse_hash",
    "section_hash",
    "sha256_hex",
    "validate",
]

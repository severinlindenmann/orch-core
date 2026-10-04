"""The one canonical JSON form every hash and signature in orch is computed over (question hashes, epic charters
and verdicts, ledger MACs, remote decisions)."""
from __future__ import annotations

import json


def canonical_json(obj) -> bytes:
    """Sorted keys, compact, UTF-8: byte-identical to a JavaScript canonicalJson of the same object."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

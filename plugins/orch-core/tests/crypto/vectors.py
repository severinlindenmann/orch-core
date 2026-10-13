"""Loader for the suite-2 section of the protocol vectors: the one vendored relay corpus, ``tests/relay/vectors/``
(contract 11A.10; relay commit, MANIFEST revision and file hash in ``tests/relay/vectors/SOURCE``).

The older copy ``tests/vectors/vectors_v2.json`` (relay ``04da0e7``) stays only because ``tests/test_skeleton.py``,
``tests/canon/test_jcs.py``, ``tests/canon/test_f1_vectors.py`` and ``scripts/sync-vectors.sh`` still read it."""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

from orch.crypto import P256_N

PATH = Path(__file__).resolve().parents[1] / "relay" / "vectors" / "vectors_v2.json"
ALL = json.loads(PATH.read_text())
S2 = ALL["suites"]["2"]
KEYS = S2["keys"]


def priv(name: str):
    """The vector key ``name`` (seed mapping ``seed mod (n-1) + 1`` is test-only, S1 spike)."""
    d = int.from_bytes(bytes.fromhex(KEYS[name]["seed"]), "big") % (P256_N - 1) + 1
    return ec.derive_private_key(d, ec.SECP256R1())


def pub(name: str) -> bytes:
    return bytes.fromhex(KEYS[name]["pub"])

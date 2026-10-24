"""The official marketplace keys pinned in this release (M#3): empty (the official index is then index_unsigned, fail
closed) or exactly the current and the next key, each a canonical 65-byte P-256 point, distinct."""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric import ec

from orch.crypto.encoding import unb64u
from orch.market import constants


def test_official_keys_are_empty_or_current_and_next_p256_points():
    keys = constants.OFFICIAL_KEYS
    assert isinstance(keys, tuple) and len(keys) in (0, 2)
    assert len(set(keys)) == len(keys)
    for k in keys:
        assert type(k) is str
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(k, 65))  # canonical and on the curve

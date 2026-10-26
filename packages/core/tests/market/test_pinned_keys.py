"""The official marketplace keys pinned in this release (M#3, V3-D39): exactly the current and the next key, each a
canonical 65-byte P-256 point, distinct. An index signed by anything else is refused."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from orch.crypto.encoding import unb64u
from orch.market import MarketError, constants, index
from tests.market import helpers as H

CURRENT = "BFh8x9dLhMubfTXleS0Bt6Ix-yu9ZiE18B0xRUUNWqsgZdOwt5DZX_Gqs5Ri3p2coFdLk9GhA-Q5WV8Ee0e05l4"
NEXT = "BM-BTpDMGAU68ywX392c5JuCjjwOkkdccxmNiOTvcX2GzIgEM2FjFH56hihyYn7pepkd8HYMolWSzM01L_B0Udw"


def test_official_keys_are_exactly_current_and_next_in_that_order():
    assert constants.OFFICIAL_KEYS == (CURRENT, NEXT)
    assert index.official_keys() == (CURRENT, NEXT)


@pytest.mark.parametrize("k", [CURRENT, NEXT])
def test_each_pinned_key_is_a_canonical_p256_point(k):
    point = unb64u(k, 65)
    assert point[0] == 4  # uncompressed
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)  # on the curve


def test_an_index_signed_by_neither_pinned_key_is_refused():
    other = ec.generate_private_key(ec.SECP256R1())
    for doc in (H.index(), H.index(key=other)):  # signed with the test key / another key: both are not a pinned key
        with pytest.raises(MarketError, match="market.index_unsigned"):
            index.evaluate(H.raw(doc), H.OFFICIAL, official=True, now=1_800_000_000)


def test_an_index_naming_a_pinned_key_with_a_foreign_signature_is_refused():
    doc = H.index()
    doc["signature"]["key"] = CURRENT  # the claimed signer is pinned, the signature is not by it
    with pytest.raises(MarketError, match="market.index_unsigned"):
        index.evaluate(H.raw(doc), H.OFFICIAL, official=True, now=1_800_000_000)

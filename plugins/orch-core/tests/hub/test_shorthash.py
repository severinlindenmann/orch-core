"""orch-hub#55: the short-hash display form (protocol 8.4) against the hub vectors, plus its edges."""

from __future__ import annotations

import pytest

from orch.hubclient.errors import HubRefused
from orch.hubclient.shorthash import short_digest, short_hash

from .vec import V

CASES = [c for c in V["cases"] if c["check"] in ("short_hash", "short_digest")]
H = "sha256:" + "0123456789abcdef" * 4


def test_the_vectors_are_vendored():
    assert {c["check"] for c in CASES} == {"short_hash", "short_digest"} and len(CASES) >= 7


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_vector(case):
    f = short_hash if case["check"] == "short_hash" else short_digest
    arg = case["args"].get("hash", case["args"].get("digest"))
    if "ok" in case["expect"]:
        assert f(arg) == case["expect"]["ok"]["shown"]
    else:
        with pytest.raises(HubRefused) as e:
            f(arg)
        assert e.value.code == case["expect"]["error"]


def test_twelve_lower_case_hex_in_groups_of_four():
    assert short_hash(H) == "0123 4567 89ab"
    assert short_digest(H[7:]) == "0123 4567 89ab"


@pytest.mark.parametrize(
    "bad", [None, 5, b"x", "", H.upper(), H + "0", H[:-1], H.replace("sha256:", ""), "sha256:" + "g" * 64]
)
def test_anything_else_is_malformed(bad):
    with pytest.raises(HubRefused):
        short_hash(bad)

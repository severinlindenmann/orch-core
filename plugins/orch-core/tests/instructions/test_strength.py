"""Passphrase strength for new keys (C7 security review)."""

import pytest

from orch.custody import CustodyError
from orch.custody.strength import check_strength


@pytest.mark.parametrize(
    "bad",
    [
        "short",
        "elevenchars",
        "passwordpassword",
        "Password-Password",
        "aaaaaaaaaaaaaa",
        "abcabcabcabcabc",
        "123456789012",
        "qwertyuiop12",
    ],
)
def test_weak_passphrases_are_refused(bad):
    with pytest.raises(CustodyError):
        check_strength(bad)


@pytest.mark.parametrize("good", ["correct horse battery", "hunter2hunter2", "tulip-river-lantern-9"])
def test_reasonable_passphrases_pass(good):
    check_strength(good)


def test_length_counts_unicode_scalars_after_nfc():
    composed = "\u00e9\u00e1\u00f3\u00fa\u00ed\u00fd"
    decomposed = "e\u0301a\u0301o\u0301u\u0301i\u0301y\u0301"
    check_strength(composed + "xzkvwq")
    check_strength(decomposed + "xzkvwq")  # NFC makes it 12 scalars too
    with pytest.raises(CustodyError):
        check_strength(decomposed + "xzkvw")  # 11

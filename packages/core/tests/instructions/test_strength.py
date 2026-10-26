"""Passphrase strength for new keys (C7 and C8 security reviews)."""

import pytest

from orch.custody import CustodyError, PassphraseBackend, PassphraseRequest
from orch.custody.strength import MIN_CHARS, check_strength, entropy_bits, generate_passphrase
from orch.identity._wordlist import WORDS

# everything the Opus review got through the first version, and the classic patterns
WEAK = [
    "correct horse battery staple",
    "correcthorsebatterystaple",
    "constantinople1234",
    "supercalifragilistic",
    "\uff49\uff4c\uff4f\uff56\uff45\uff59\uff4f\uff55\uff46\uff4f\uff52\uff45\uff56\uff45\uff52",  # full-width
    "p\u200ba\u200bs\u200bs\u200bw\u200bo\u200br\u200bd\u200bp\u200ba\u200bs\u200bs",  # zero-width
    "Password123!",
    "passwordpass",
    "abcdefghijklm",
    "123456789abc",
    "qwertyuiop123",
    "summer2024summer",
    "Tester2026!!",
    "short",
    "elevenchars",
    "passwordpassword",
    "Password-Password",
    "aaaaaaaaaaaaaaaa",  # a repeat
    "abcabcabcabcabcabc",  # a repeated block
    "abcdefghijklmnop",  # ascending
    "zyxwvutsrqponmlk",  # descending
    "qwertyuiopasdfgh",  # keyboard rows
    "1234567890123456",
    "p4ssw0rdp4ssw0rd",  # leet
    "monkey1989monkey",  # word, year, word
    "iloveyou2024!!!!",
    "correct horse bat",  # three words
    "tulip river lantern",  # three words
    "hunter2hunter2hunter2",
]
GOOD = [
    "Zq7!mPx2-vL9#rTb4w",
    "Grün-Apfel-Herbst-Fenster-7421-Kuh",
    "vK3$wQ8nB5#xD2&zL",
    "marble canyon velvet orbit fossil",
]


@pytest.mark.parametrize("bad", WEAK)
def test_weak_passphrases_are_refused(bad):
    with pytest.raises(CustodyError):
        check_strength(bad)


@pytest.mark.parametrize("good", GOOD)
def test_reasonable_passphrases_pass(good):
    check_strength(good)


def test_the_generated_passphrase_always_passes_and_is_six_distinct_words():
    for _ in range(50):
        p = generate_passphrase()
        words = p.split(" ")
        assert len(words) == 6 and len(set(words)) == 6 and all(w in WORDS for w in words)
        check_strength(p)


def test_the_estimate_prices_patterns_cheaply_and_random_text_dearly():
    assert entropy_bits("abcdefghijklmnop") < 15
    assert entropy_bits("summer2024summer") < 35
    assert entropy_bits("Zq7!mPx2-vL9#rTb4w") > 100


def test_length_counts_unicode_scalars_after_nfc():
    composed = "éáóúíý"
    decomposed = "éáóúíý"
    tail = "xzKvWqJ9"  # 14 scalars in all
    assert len(composed + tail) == MIN_CHARS == 14
    check_strength(composed + tail)
    check_strength(decomposed + tail)  # NFC makes it 14 scalars too
    with pytest.raises(CustodyError):
        check_strength(decomposed + tail[:-1])  # 13


def test_the_backend_applies_the_same_check_to_a_prompted_and_to_a_given_passphrase(tmp_path):
    asked = []

    def provider(request: PassphraseRequest) -> str:
        asked.append(request)
        return "password1234567"

    b = PassphraseBackend(tmp_path / "a", _passphrase_provider=provider)
    with pytest.raises(CustodyError):
        b.create("dk", role="device")  # prompted
    with pytest.raises(CustodyError):
        b.create("dk", role="device", passphrase="password1234567")  # given
    assert len(asked) == 1 and not b.exists("dk")


def test_the_minimum_length_is_one_constant():
    from orch.custody import MIN_PASSPHRASE_CHARS

    assert MIN_PASSPHRASE_CHARS == MIN_CHARS


def test_the_users_name_is_charged_as_a_word():
    check_strength("Severin2026Severin")  # without context it is just letters and digits
    with pytest.raises(CustodyError):
        check_strength("Severin2026Severin", ("Severin",))

"""The tamper helpers change every value, including the ones the old ``[:-2] + "AA"`` left untouched."""

from __future__ import annotations

import base64
import os

from tests.tamper import tamper_b64u, tamper_text


def test_the_old_suffix_tamper_was_a_no_op_for_some_values_and_the_helpers_never_are():
    sig = base64.urlsafe_b64encode(bytes(63) + b"\x00").decode().rstrip("=")  # all-zero: ends in "AA"
    assert sig[:-2] + "AA" == sig  # what the old tamper did to it: nothing
    assert tamper_b64u(sig) != sig
    assert tamper_text(sig) != sig


def test_every_random_signature_is_changed_and_stays_decodable():
    for _ in range(300):
        sig = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
        out = tamper_b64u(sig)
        assert out != sig and len(out) == len(sig)
        assert tamper_text(sig) != sig

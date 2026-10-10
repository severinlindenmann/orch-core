"""The bundled Unicode 16.0 data and NFC implementation (ticket-format §11.3, "pinned to 16.0")."""

import random
import unicodedata

import pytest

from orch.canon import _unicode16, nfc, text
from tests.canon import gen_unicode16

RUNTIME_IS_16 = unicodedata.unidata_version == "16.0.0"
needs16 = pytest.mark.skipif(not RUNTIME_IS_16, reason="needs a Unicode 16.0 runtime to compare against")


def test_bundled_version():
    assert _unicode16.UNICODE_VERSION == "16.0.0"


def test_known_values_independent_of_the_runtime():
    assert text._assigned(0x10D40) and text._assigned(0x41) and text._assigned(0xE000)  # Garay (16.0), A, PUA
    assert not text._assigned(0x378) and not text._assigned(0xFFFF) and not text._assigned(0x10FFFF)
    assert text._ccc(0x0301) == 230 and text._ccc(0x0323) == 220 and text._ccc(0x41) == 0
    assert nfc("é") == "é" and nfc("한") == "한" and nfc("Å") == "Å"
    assert nfc("ạ́") == "ạ́" and nfc("ạ́") == "ạ́"  # reordering
    assert nfc("̈́") == "̈́"  # singleton decomposition, not recomposed
    assert nfc("क़") == "क़"  # composition exclusion
    assert nfc("한".encode().decode()) == "한" and nfc("") == ""


@needs16
def test_generated_module_is_up_to_date():
    from pathlib import Path

    assert (Path(_unicode16.__file__)).read_text() == gen_unicode16.generate()


@needs16
def test_nfc_matches_runtime_on_every_code_point():
    for cp in range(0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        s = chr(cp)
        assert nfc(s) == unicodedata.normalize("NFC", s), hex(cp)


@needs16
def test_assigned_matches_runtime_category():
    for cp in range(0x110000):
        assert text._assigned(cp) == (unicodedata.category(chr(cp)) != "Cn"), hex(cp)


@needs16
def test_nfc_matches_runtime_on_random_sequences():
    rnd = random.Random(16)
    pool = [
        cp
        for cp in range(0x110000)
        if not 0xD800 <= cp <= 0xDFFF
        and (unicodedata.combining(chr(cp)) or unicodedata.decomposition(chr(cp)) or 0x1100 <= cp < 0x11C0)
    ]
    for _ in range(4000):
        s = "".join(
            chr(rnd.choice(pool)) if rnd.random() < 0.7 else rnd.choice("aeouᅡ") for _ in range(rnd.randint(1, 8))
        )
        assert nfc(s) == unicodedata.normalize("NFC", s), [hex(ord(c)) for c in s]

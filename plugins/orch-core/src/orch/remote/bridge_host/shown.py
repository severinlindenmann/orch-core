"""The text a person approves on a device, and a pairing label (spec §9.3): the host cleans it, the device shows and
hashes exactly what it received. No normalisation: the hash covers exactly what is shown."""
from __future__ import annotations

import hashlib
import unicodedata

from orch.remote.bridge_host.envelope import canonical_json

# Default_Ignorable_Code_Point (Unicode DerivedCoreProperties.txt), as inclusive ranges
_IGNORABLE = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
              (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
              (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
              (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
_REMOVED = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn"})


def _kept(c: str) -> bool:
    if c == "\n":
        return True
    o = ord(c)
    return unicodedata.category(c) not in _REMOVED and not any(a <= o <= b for a, b in _IGNORABLE)


def clean_shown(s: str) -> str:
    """`s` without controls (line feed kept), format, separator, private-use and unassigned code points and every
    default-ignorable one. ValueError for a string that is not Unicode scalar values (a lone surrogate)."""
    if not isinstance(s, str):
        raise ValueError("not text")
    if any(0xD800 <= ord(c) <= 0xDFFF for c in s):
        raise ValueError("not Unicode scalar values")
    return "".join(c for c in s if _kept(c))


def subject_hash(subject: dict) -> bytes:
    return hashlib.sha256(canonical_json(subject)).digest()

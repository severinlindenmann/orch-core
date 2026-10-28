"""The text rules of a company skill (orch-hub ``contracts/hub/protocol.md`` 7.3, M3 #1430), the host's copy.

Company skills are read by agents, so text a person cannot see is a prompt-injection channel. The rule is an ALLOWLIST,
the same function the hub runs (``orch_hub.contract.org_bundles``): letters, numbers, punctuation, symbols and plain
spaces, plus newline and tab. Everything else is refused, never stripped. ``contracts/hub/vectors_org_skills.json``
(``text_cases``) holds the cases both sides replay, so they refuse identically."""

from __future__ import annotations

import re
import unicodedata

__all__ = ["SKILL_TEXT_MAX", "skill_problem", "text_ok"]

SKILL_NAME = re.compile(r"[a-z][a-z0-9-]{1,39}")
RESERVED_PREFIX = "orch"  # orch's own skills; a company skill never shadows a builtin name
SKILL_TEXT_MAX = 2400
_ALLOWED_CATEGORIES = ("L", "N", "P", "S")
# letters or symbols by category that draw nothing (Hangul fillers, Khmer inherent vowels, braille blank)
_INVISIBLE_FILLERS = frozenset(chr(c) for c in (0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0))


def text_ok(text: str) -> bool:
    return all(
        ch in "\n\t"
        or (
            ch not in _INVISIBLE_FILLERS
            and (unicodedata.category(ch)[0] in _ALLOWED_CATEGORIES or unicodedata.category(ch) == "Zs")
        )
        for ch in text
    )


def skill_problem(name: object, text: object) -> str | None:
    """None when a skill is acceptable, else a short reason code (same codes as the hub)."""
    if type(name) is not str or SKILL_NAME.fullmatch(name) is None:
        return "name"
    if name.startswith(RESERVED_PREFIX):
        return "reserved_name"
    if type(text) is not str or not text.strip():
        return "empty"
    if len(text) > SKILL_TEXT_MAX:
        return "too_long"
    if not text_ok(text):
        return "hidden_characters"
    if text.lstrip().startswith("---"):
        return "front_matter"
    return None

"""Company content a hub hands to a workspace (M3, #1430): the text rules and digests of ``hub.org_skills``.

Company skills are instructions an agent reads, so they are a prompt-injection channel. The rules here are the same on
both sides (the hub refuses to store a bad skill, the host refuses to adopt a bad bundle): a name that cannot be mistaken
for one of orch's own skills, a bounded text of visible characters only (an allowlist) and no front matter. Nothing
here grants anything: a skill the owner accepts is read below orch's rules (the host adds that label)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

from .canon import cj

SKILL_NAME = re.compile(r"[a-z][a-z0-9-]{1,39}")
#: orch's own skills are ``orch-*``; the whole prefix is reserved so a company skill never shadows a builtin name
RESERVED_PREFIX = "orch"
SKILL_TEXT_MAX = 2400
SKILLS_MAX = 20
#: Letters, numbers, punctuation, symbols and plain spaces, plus newline and tab: an ALLOWLIST. Everything else is
#: refused, never stripped, because any category left out (marks, format characters, line separators, private use,
#: unassigned) can hide text from the person who reads it and still reach an agent: variation selectors, the tag block,
#: bidi and zero-width characters. A few fillers are letters or symbols by category but draw nothing, so they are named.
_ALLOWED_CATEGORIES = ("L", "N", "P", "S")
_INVISIBLE_FILLERS = frozenset(chr(c) for c in (0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0))
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def text_ok(text: str) -> bool:
    """True when every character is visible text. The same function, byte for byte, runs at the host (orch-core
    ``orch.hubclient.org_text``); ``vectors_org_skills.json`` holds the cases both replay."""
    return all(
        ch in "\n\t"
        or (
            ch not in _INVISIBLE_FILLERS
            and (unicodedata.category(ch)[0] in _ALLOWED_CATEGORIES or unicodedata.category(ch) == "Zs")
        )
        for ch in text
    )


def normal_text(text: str) -> str:
    """The one harmless rewrite on save: NFC and ``\n`` line ends. Nothing else is changed; a bad character is refused."""
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n"))


def skill_problem(name: Any, text: Any) -> str | None:
    """None when a skill is acceptable, else a short reason code."""
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


def skill_digest(name: str, text: str) -> str:
    return "sha256:" + hashlib.sha256(cj({"name": name, "text": text})).hexdigest()


def valid_org_skills(x: Any) -> bool:
    """The ``skills`` member of ``hub.org_skills``: a sorted-by-name list of ``{name, text, digest}``."""
    if type(x) is not list or len(x) > SKILLS_MAX:
        return False
    names = []
    for s in x:
        if type(s) is not dict or set(s) != {"name", "text", "digest"}:
            return False
        if skill_problem(s["name"], s["text"]) is not None:
            return False
        if type(s["digest"]) is not str or _DIGEST.fullmatch(s["digest"]) is None:
            return False
        if s["digest"] != skill_digest(s["name"], s["text"]):
            return False
        names.append(s["name"])
    return names == sorted(set(names))

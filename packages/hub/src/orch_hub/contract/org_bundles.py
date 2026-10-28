"""Company content a hub hands to a workspace (M3, #1430): the text rules and digests of ``hub.org_skills``.

Company skills are instructions an agent reads, so they are a prompt-injection channel. The rules here are the same on
both sides (the hub refuses to store a bad skill, the host refuses to adopt a bad bundle): a name that cannot be mistaken
for one of orch's own skills, a bounded text with no control or bidirectional characters and no front matter. Nothing
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
#: characters that reorder or hide text: bidi controls, isolates, zero width, the BOM
_HIDDEN = frozenset(
    chr(c) for c in (0x061C, *range(0x200B, 0x2010), *range(0x202A, 0x202F), 0x2060, *range(0x2066, 0x206A), 0xFEFF)
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def clean_text(text: str) -> str:
    """Strip control (except newline and tab) and hidden/bidi characters; normalise line ends."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch for ch in text if ch not in _HIDDEN and (ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf"))
    )


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
    if clean_text(text) != text.replace("\r\n", "\n").replace("\r", "\n") or "\r" in text:
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

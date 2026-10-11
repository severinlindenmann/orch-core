"""The three built-in skills (``orch-tickets``, ``orch-work-on-ticket``, ``orch-refine-ticket``): ``SKILL.md`` in the
portable format and an ``orch.skill.json`` sidecar (land-skills D55). They hold judgment rules only: when to ask, what
a good plan is. Commands are never explained there (``orch help`` and ``orch describe`` do that, from the registry).

The files ship as package data in ``orch/instructions/skills/<name>/``. The sidecar is validated here with plain
Python (the core has no YAML dependency and the sidecar is small): exact keys, ``schema_version`` 1, a semantic
``skill_version``, a ``scope`` of ``builtin``, ``workspace`` or ``org``, and lists of names for ``connections`` and
``env``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "SKILL_MAX_CHARS",
    "SKILL_NAMES",
    "Skill",
    "SkillError",
    "builtin_skills",
    "check_sidecar",
    "parse_frontmatter",
]

SKILL_NAMES = ("orch-tickets", "orch-work-on-ticket", "orch-refine-ticket")
#: The size budget of one skill file: about 600 tokens at four characters a token. A skill is loaded only when the
#: task matches, but when it is, it must stay small.
SKILL_MAX_CHARS = 2400
SIDECAR_NAME = "orch.skill.json"
SKILL_NAME = "SKILL.md"
_DIR = Path(__file__).parent / "skills"
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_CONN_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SCOPES = ("builtin", "workspace", "org")


class SkillError(ValueError):
    """A skill file or its sidecar is not valid."""


@dataclass(frozen=True)
class Skill:
    name: str
    text: str  # SKILL.md
    sidecar: dict[str, Any]

    @property
    def version(self) -> str:
        return self.sidecar["skill_version"]

    def files(self) -> dict[str, str]:
        """``{relative path: content}`` below the skills directory."""
        return {
            f"{self.name}/{SKILL_NAME}": self.text,
            f"{self.name}/{SIDECAR_NAME}": json.dumps(self.sidecar) + "\n",
        }


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """``({name, description}, body)`` of a ``SKILL.md``. Exactly the two keys ``name`` and ``description``, each one
    line of ``key: value`` (the portable subset; no YAML parser is needed or used)."""
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise SkillError("SKILL.md must start with ---")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise SkillError("the frontmatter is not closed") from None
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(": ")
        if not sep or key in meta or not value.strip():
            raise SkillError(f"bad frontmatter line {line!r}")
        meta[key] = value.strip()
    if set(meta) != {"name", "description"}:
        raise SkillError("the frontmatter holds exactly name and description")
    if not _NAME.fullmatch(meta["name"]):
        raise SkillError("the skill name is lower-case words joined by dashes")
    return meta, "\n".join(lines[end + 1 :])


def check_sidecar(doc: object) -> dict[str, Any]:
    """The sidecar if it is valid (D55), else :class:`SkillError`."""
    if not isinstance(doc, dict) or set(doc) != {"schema_version", "skill_version", "scope", "connections", "env"}:
        raise SkillError("the sidecar has exactly schema_version, skill_version, scope, connections, env")
    if doc["schema_version"] != 1 or isinstance(doc["schema_version"], bool):
        raise SkillError("schema_version must be 1")
    if not isinstance(doc["skill_version"], str) or not _SEMVER.fullmatch(doc["skill_version"]):
        raise SkillError("skill_version must be a semantic version (1.2.0)")
    if doc["scope"] not in SCOPES:
        raise SkillError(f"scope must be one of {', '.join(SCOPES)}")
    for key, pattern in (("connections", _CONN_NAME), ("env", _ENV_NAME)):
        names = doc[key]
        if not isinstance(names, list) or not all(isinstance(n, str) and pattern.fullmatch(n) for n in names):
            raise SkillError(f"{key} must be a list of names")
        if len(set(names)) != len(names):
            raise SkillError(f"{key} has a duplicate")
    return doc


def _load(name: str) -> Skill:
    text = (_DIR / name / SKILL_NAME).read_text(encoding="utf-8")
    meta, _body = parse_frontmatter(text)
    if meta["name"] != name:
        raise SkillError(f"{name}: the frontmatter names {meta['name']!r}")
    sidecar = check_sidecar(json.loads((_DIR / name / SIDECAR_NAME).read_text(encoding="utf-8")))
    return Skill(name, text, sidecar)


def builtin_skills() -> list[Skill]:
    return [_load(n) for n in SKILL_NAMES]

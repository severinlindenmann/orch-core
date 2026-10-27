"""The skill of an add-on (design addon-skills D1): one ``SKILL.md`` in the folder the manifest's ``skill`` names.

``check_package`` is the validator every path runs on the bytes of a package (``orch addon pack``, an install, a read of
an installed package): the folder holds exactly ``SKILL.md``; the file is UTF-8, at most 2400 characters, no NUL or
CR; its frontmatter is exactly ``name`` (``orch-addon-<addon>``) and ``description`` (1 to 300 printable characters);
every command it shows resolves (the command lint); it names no ``tickets/`` or ``.orch/`` path and no other skill.
A skill that names a command that does not exist is the worst failure for a weak model, which follows it literally.

``warnings`` are the conventions that are not errors (``Use when`` / ``Not for``, the four sections), shown by ``pack``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

__all__ = ["SkillError", "check_package", "check_skill", "skill_name", "skill_path", "warnings"]

DESCRIPTION_MAX = 300
_FORBIDDEN_PATH = re.compile(r"(tickets|\.orch)/", re.I)  # the ticket files and the store folder, as path starts
_SPAN = re.compile(r"`([^`\n]+)`")
_FIELD_TOKEN = re.compile(r"([a-z][a-z0-9-]*)\.([A-Za-z_][A-Za-z0-9_]*)=")
_OTHER_SKILL = re.compile(r"orch-addon-[a-z0-9-]+", re.I)
_ORCH = re.compile(r"(?<![\w.-])(?:/[\w./-]*/)?orch(?![\w/-])", re.I)  # also /usr/bin/orch and Orch
_HIDDEN = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})  # controls, formats, separators, unassigned
_WORD = re.compile(r"[A-Za-z][\w.-]*")
_SHELL_BREAK = re.compile(r"&&|\|\||[;|&)]")
_SECTIONS = ("Use when", "Do not use when", "Steps", "If orch refuses")


class SkillError(ValueError):
    """The skill breaks a rule; the message says which (it is the author's own text, never a secret)."""


def skill_name(addon: str) -> str:
    return "orch-addon-" + addon


def skill_path(folder: str) -> str:
    return folder + "/SKILL.md"


def _commands() -> list[tuple[str, ...]]:
    import orch.ops as ops

    return sorted((tuple(o.cli.split()) for o in ops.all()), key=len, reverse=True)


def _resolve(tokens: list[str], addon: str, fields: Mapping[str, Any], known: list[tuple[str, ...]]) -> None:
    match = next((c for c in known if tuple(tokens[: len(c)]) == c), None)
    if match is None:
        raise SkillError(f"the command `orch {' '.join(tokens)[:60]}` is not an orch command")
    if match == ("set",):
        for token in tokens:
            m = _FIELD_TOKEN.match(token)
            if m is not None and (m[1] != addon or m[2] not in fields):
                raise SkillError(f"`orch set` may name only this add-on's own fields, not {m[1]}.{m[2]}")


def _tokens_after(segment: str, start: int) -> list[str]:
    """The words of the command that begins after ``orch`` at ``start``: up to the next shell separator or comment,
    quotes cut, leading global flags (``--json``) skipped."""
    part = _SHELL_BREAK.split(segment[start:], 1)[0].split("#", 1)[0]
    tokens = [t for t in (x.strip("\"'") for x in part.split()) if t]
    while tokens and tokens[0].startswith("-"):
        tokens.pop(0)
    return tokens


def _lint_code(segment: str, addon: str, fields: Mapping[str, Any], known: list[tuple[str, ...]]) -> None:
    """Code: every ``orch`` anywhere in it (after ``cd x &&``, ``VAR=1``, ``bash -c "..."``) is a command to resolve.
    Prose is not linted ("orch refuses", "the orch instructions" are English); a first token that is no word
    (``pip install orch==1.2``) names no command."""
    segment = re.sub(r"(^|\s)#.*", "", segment)  # a comment is prose
    for m in _ORCH.finditer(segment):
        tokens = _tokens_after(segment, m.end())
        if tokens and _WORD.fullmatch(tokens[0]):
            _resolve(tokens, addon, fields, known)


def _lint(addon: str, body: str, fields: Mapping[str, Any]) -> None:
    from orch.instructions.skills import SKILL_NAMES

    known = _commands()
    fence: str | None = None
    for line in body.split("\n"):
        stripped = line.strip()
        marker = stripped[:3] if stripped[:3] in ("```", "~~~") else None
        if fence is not None:
            if marker == fence:
                fence = None
            else:
                _lint_code(stripped.removeprefix("$ "), addon, fields, known)
            continue
        if marker is not None:
            fence = marker
            continue
        if line.startswith(("    ", "\t")):  # an indented code block
            _lint_code(stripped.removeprefix("$ "), addon, fields, known)
            continue
        for m in _SPAN.finditer(line):
            _lint_code(m[1], addon, fields, known)
    if _FORBIDDEN_PATH.search(body):
        raise SkillError("the skill must not name the ticket files or the store folder (agents never touch them)")
    own = skill_name(addon)
    for m in _OTHER_SKILL.finditer(body):
        if m[0].lower() != own:
            raise SkillError(f"the skill names another skill ({m[0]})")
    low = body.lower()
    for name in SKILL_NAMES:
        if name in low:
            raise SkillError(f"the skill names a built-in skill ({name})")


def check_skill(
    addon: str, raw: bytes, fields: Mapping[str, Any] | None = None, *, lint: bool = True
) -> tuple[dict[str, str], str]:
    """``(frontmatter, body)`` of the add-on's ``SKILL.md`` bytes, or :class:`SkillError`. ``fields`` are the add-on's
    own manifest fields (the only ones ``orch set`` may name in the skill). ``lint=False`` skips the command lint, which
    depends on this core's operations (a read of an installed package must not fail because core renamed a command)."""
    from orch.instructions.skills import SKILL_MAX_CHARS, parse_frontmatter
    from orch.instructions.skills import SkillError as FrontmatterError

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillError("SKILL.md is not UTF-8") from None
    if "\x00" in text or "\r" in text:
        raise SkillError("SKILL.md holds a NUL or a CR")
    if any(ch not in "\n\t" and unicodedata.category(ch) in _HIDDEN for ch in text):
        raise SkillError("SKILL.md holds a hidden character (control, zero-width, direction mark, line separator)")
    if len(text) > SKILL_MAX_CHARS:
        raise SkillError(f"SKILL.md has {len(text)} characters, the limit is {SKILL_MAX_CHARS}")
    try:
        meta, body = parse_frontmatter(text)
    except FrontmatterError as e:
        raise SkillError(f"SKILL.md: {e}") from None
    if meta["name"] != skill_name(addon):
        raise SkillError(f"the skill name must be {skill_name(addon)}")
    desc = meta["description"]
    if not 1 <= len(desc) <= DESCRIPTION_MAX or not desc.isprintable():
        raise SkillError(f"the description is 1 to {DESCRIPTION_MAX} printable characters")
    if lint:
        _lint(addon, body, fields or {})
    return meta, body


def check_package(manifest: Any, files: Mapping[str, bytes], *, lint: bool = True) -> None:
    """Raise :class:`SkillError` unless the package's skill (if the manifest names one) is valid: the folder holds
    exactly ``SKILL.md`` and the file passes :func:`check_skill`. A package without ``skill`` passes unchanged."""
    folder = manifest.skill
    if folder is None:
        return
    inside = sorted(p for p in files if p.startswith(folder + "/"))
    if inside != [skill_path(folder)]:
        raise SkillError(f"{folder}/ must hold exactly SKILL.md")
    check_skill(manifest.name, files[skill_path(folder)], manifest.fields, lint=lint)


def warnings(manifest: Any, files: Mapping[str, bytes]) -> list[str]:
    """The conventions of the skill template that ``pack`` reports without refusing (non-English authors are not
    blocked): ``description`` starts ``Use when`` and has a ``Not for``, the body has the four sections, and
    ``agents_md`` reads as a trigger. Empty for a valid skill that follows them."""
    out: list[str] = []
    agents = manifest.agents_md
    if agents and not (agents.lower().startswith("use when") and "not for" in agents.lower()):
        out.append('agents_md should read "Use when ... Not for ..." (it is the trigger agents route by)')
    if manifest.skill is None:
        return out
    try:
        meta, body = check_skill(manifest.name, files[skill_path(manifest.skill)], manifest.fields)
    except (SkillError, KeyError):
        return out
    desc = meta["description"]
    if not desc.startswith("Use when"):
        out.append('the skill description should start with "Use when"')
    if "Not for" not in desc:
        out.append('the skill description should say "Not for ..." (the neighbour add-on to use instead)')
    headings = {x.strip().lstrip("#").strip().rstrip(":") for x in body.split("\n")}
    starts = [x.strip().lstrip("#").strip() for x in body.split("\n")]
    for sec in _SECTIONS:
        if sec not in headings and not any(x.startswith(sec + ":") for x in starts):
            out.append(f'the skill should have a "{sec}" section')
    return out

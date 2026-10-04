"""`orch new --body-file` (#24): a body file whose `## Requirements`, `## Acceptance criteria`, `## Out of scope` or
`## Summary` headings (also `###`, any case) are split into those sections; the rest stays the Ask."""
from __future__ import annotations

import re

from orch.core import fences
from orch.core.constants import SECTIONS
from orch.errors import UsageError

# The sections `orch new` fills from a body file or from their own files. Requirements and Acceptance criteria are
# the ones the requirements gate refuses to approve while they are empty (GATED_EMPTY).
SPLIT_SECTIONS = ("Summary", "Requirements", "Acceptance criteria", "Out of scope")
GATED_EMPTY = ("Requirements", "Acceptance criteria")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_CANONICAL = {s.lower(): s for s in SECTIONS}


def _name(raw: str) -> str:
    return " ".join(raw.rstrip(":").split()).lower()


def split_body(text: str) -> tuple[str, dict[str, str]]:
    """(the Ask, {section: text}) of a body file. A part runs from its heading to the next heading of the same or a
    higher level, or to the next part; subheadings inside it stay in it. Headings inside fences are text. A level-2
    heading naming another orch section is refused (it would become that section in the file); any other level-2
    heading left in the Ask is moved one level down, so the Ask stays whole."""
    ask: list[str] = []
    parts: dict[str, list[str]] = {}
    current: tuple[str, int] | None = None  # (section, heading level) of the part being read
    fence: str | None = None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        was = fence
        fence, is_fence = fences.step(fence, line)
        heading = None if (was or is_fence) else _HEADING.match(line)
        if heading:
            level, name = len(heading.group(1)), _CANONICAL.get(_name(heading.group(2)))
            if name in SPLIT_SECTIONS and level in (2, 3):
                if name in parts:
                    raise UsageError(f"the body file has two {name} headings", hint="keep one")
                parts[name], current = [], (name, level)
                continue
            if current and level <= current[1]:
                current = None
            if current is None and level == 2:
                if name is not None:
                    raise UsageError(f"the body file has a `## {name}` heading; it would become that section",
                                     hint=_section_hint(name))
                line = "#" + line
        (parts[current[0]] if current else ask).append(line)
    return "\n".join(ask).strip(), {k: "\n".join(v).strip() for k, v in parts.items() if "\n".join(v).strip()}


def _section_hint(name: str) -> str:
    if name == "Tasks":
        return "add tasks with `orch task add` after creating the ticket"
    if name == "Log":
        return "the Log is written by orch; add lines with `orch log`"
    if name == "Ask":
        return "the body file is the Ask; leave out the heading"
    return f"set it after creating the ticket with `orch section set <id> \"{name}\" --file …`, or rename the heading"


def empty_gate_warnings(ticket) -> list[str]:
    """One warning per gated section that is empty: the requirements gate refuses to approve then."""
    out = []
    for name in GATED_EMPTY:
        if not ticket.section(name).strip():
            quoted = f'"{name}"' if " " in name else name
            out.append(f"{name} empty: the requirements gate will refuse; set it with "
                       f"`orch section set {ticket.id} {quoted} --file …`")
    return out

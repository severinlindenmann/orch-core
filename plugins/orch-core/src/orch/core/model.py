from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from orch.core import fences
from orch.core.constants import FILE_ORDER, FRONTMATTER_ORDER
from orch.errors import TicketParseError


_NO_TIMESTAMPS = {
    key: [r for r in resolvers if r[0] != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class _PyLoader(yaml.SafeLoader):
    """SafeLoader that keeps timestamps as strings (we store them as text on purpose)."""


_PyLoader.yaml_implicit_resolvers = _NO_TIMESTAMPS

if getattr(yaml, "__with_libyaml__", False):
    class _Loader(yaml.CSafeLoader):
        """The same loader on libyaml's parser: about 7x faster on ticket frontmatter, same resolver table, so the
        same values and types. Every page parses tickets; the pure-Python loader stays for errors (`yaml_load`)."""

    _Loader.yaml_implicit_resolvers = _NO_TIMESTAMPS
else:  # PyYAML built without libyaml
    _Loader = _PyLoader

_H2 = re.compile(r"^## (.+)$")  # linear: the closing #s are stripped in h2_title, never by backtracking


def h2_title(line: str) -> str | None:
    """The title of a level-2 heading line (closing #s and spaces dropped), or None. A regex with the closing
    sequence in it (`(.+?)\\s*#*\\s*$`) backtracks cubically on a line of many spaces."""
    m = _H2.match(line)
    if m is None:
        return None
    text = m.group(1)
    title = text.rstrip().rstrip("#").rstrip()
    return title.strip() if title else text.strip()[:1]  # "## ###" is the section "#", as it always was


_H1 = re.compile(r"^# (?!#)")
_FM_END = re.compile(r"^---[ \t]*$", re.M)


def neutral_text(text: str) -> str:
    """Free text bound for a ticket section (an Ask, a reason, a note, an answer...): a line that would open or
    close a fence (``` or ~~~, `fences.could_fence`) or start a heading (# ..., `_H1`/`_H2`) gets a leading backslash, so it
    reads the same but can never forge a section boundary when the file is re-parsed — including an unclosed
    fence, which `_split_body` would otherwise track across every line that follows it, swallowing real headings."""
    out = []
    for line in text.splitlines():  # splitlines also breaks on a bare \r, which the section parser treats as a line end
        indent = len(line) - len(line.lstrip())
        rest = line[indent:]
        if fences.could_fence(line) or rest.startswith("#"):
            out.append(line[:indent] + "\\" + rest)
        else:
            out.append(line)
    return "\n".join(out)


def section_text_problem(text: str) -> str | None:
    """Why `text` cannot be written as one section's body, or None. Read back the way `_split_body` reads a file, a
    line outside a fence that starts a level-2 heading would begin a section of its own (`## Log` would forge the
    Log), and a fence left open would swallow every heading after it, the real sections included."""
    fence: str | None = None
    for n, line in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        fence, is_fence = fences.step(fence, line)
        if not is_fence and fence is None and h2_title(line) is not None:
            return f"line {n} starts a level-2 heading ({line.strip()[:60]}), which would begin a section of its own"
    if fence is not None:
        return f"a {fence} code fence is not closed"
    return None


def yaml_load(text: str):
    try:
        return yaml.load(text, Loader=_Loader)
    except yaml.YAMLError:
        if _Loader is _PyLoader:
            raise
        # The error the pure-Python loader gives (and the rare document only it accepts): its messages are the
        # ones users and tests have always seen.
        return yaml.load(text, Loader=_PyLoader)


def yaml_dump(data) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)


@dataclass
class Ticket:
    meta: dict
    sections: dict[str, str] = field(default_factory=dict)
    preamble: str = ""

    @property
    def id(self) -> str:
        return str(self.meta["id"])

    @property
    def status(self) -> str:
        return str(self.meta.get("status", ""))

    @property
    def title(self) -> str:
        return str(self.meta.get("title", ""))

    def section(self, name: str) -> str:
        return self.sections.get(name, "")

    def set_section(self, name: str, text: str) -> None:
        self.sections[name] = text.strip("\n").rstrip()

    def append_log(self, line: str) -> None:
        current = self.section("Log")
        self.set_section("Log", f"{current}\n{line}" if current else line)


def new_ticket(id: str, title: str, *, type: str, priority: str, size: str, created: str) -> Ticket:
    meta = {
        "id": id, "title": title, "type": type, "priority": priority, "size": size,
        "status": "backlog", "created": created, "updated": created,
        "external": [], "repos": [], "branches": {}, "worktrees": {}, "prs": [],
        "parent": None, "blocked_by": [], "follow_ups": [], "labels": [],
        "gates": {
            "requirements": {"approved": None, "via": None, "hash": None},
            "plan": {"approved": None, "via": None, "hash": None},
            "verify": {"verdict": None, "at": None, "via": None},
        },
        "questions": [],
        "claim": {"session": None, "harness": None, "at": None},
        "sessions": [],
    }
    return Ticket(meta=meta)  # sections appear when someone writes them; an empty one is never written


def parse_ticket(text: str, source: str = "<string>") -> Ticket:
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        raise TicketParseError(f"{source}: missing frontmatter (file must start with ---)")
    end = _FM_END.search(text, 4)
    if end is None:
        raise TicketParseError(f"{source}: unterminated frontmatter")
    try:
        meta = yaml_load(text[4:end.start()]) or {}
    except yaml.YAMLError as e:
        raise TicketParseError(f"{source}: invalid YAML frontmatter ({e})") from e
    if not isinstance(meta, dict) or "id" not in meta:
        raise TicketParseError(f"{source}: frontmatter must be a mapping with an 'id'")
    sections, preamble = _split_body(text[end.end():])
    return Ticket(meta=meta, sections=sections, preamble=preamble)


def parse_body(text: str) -> tuple[dict[str, str], str]:
    """(sections, preamble) of a ticket file without parsing its YAML frontmatter.

    For callers that already hold the file's validated meta (e.g. `store.scan`'s Entry.meta);
    raises TicketParseError when the frontmatter fences are missing.
    """
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    end = _FM_END.search(text, 4) if text.startswith("---\n") else None
    if end is None:
        raise TicketParseError("missing or unterminated frontmatter")
    return _split_body(text[end.end():])


def _split_body(body: str) -> tuple[dict[str, str], str]:
    sections: dict[str, list[str]] = {}
    pre: list[str] = []
    current: list[str] | None = None
    fence: str | None = None
    h1_seen = False
    for line in body.split("\n"):
        fence, is_fence = fences.step(fence, line)
        if not is_fence and fence is None:
            h2 = h2_title(line)
            if h2 is not None:
                current = sections.setdefault(h2, [])
                continue
            if current is None and not h1_seen and _H1.match(line):
                h1_seen = True  # generated title line; re-rendered from meta
                continue
        (current if current is not None else pre).append(line)
    parsed = {name: "\n".join(lines).strip("\n").rstrip() for name, lines in sections.items()}
    return parsed, "\n".join(pre).strip()


def _ordered_meta(meta: dict) -> dict:
    out = {k: meta[k] for k in FRONTMATTER_ORDER if k in meta}
    out.update({k: v for k, v in meta.items() if k not in out})
    return out


def render_ticket(t: Ticket) -> str:
    parts = ["---\n", yaml_dump(_ordered_meta(t.meta)), "---\n\n", f"# {t.id} — {t.title}\n"]
    if t.preamble:
        parts.append(f"\n{t.preamble}\n")
    names = list(FILE_ORDER) + [s for s in t.sections if s not in FILE_ORDER]
    for name in names:
        content = t.sections.get(name, "")
        if content:  # an empty section costs every reader a heading and says nothing
            parts.append(f"\n## {name}\n\n{content}\n")
    return "".join(parts)

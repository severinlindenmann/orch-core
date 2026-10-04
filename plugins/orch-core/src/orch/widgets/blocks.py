"""Finding and checking ```orch blocks (docs/widgets.md, "Parsing rules", "Where widgets may stand")."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from orch.core import fences
from orch.core.constants import LEGACY_SECTIONS, SECTIONS
from orch.core.model import h2_title

MAX_BYTES = 64 * 1024
MAX_BLOCKS = 40
MAX_DEPTH = 8
MAX_STRING = 20_000
MAX_ITEMS = 500  # rows: any array
MAX_INNER = 50  # columns: an array inside an array
LAYERS = ("type", "widget", "html")
# Hashed by a gate (an edit would void approval), own grammar (Tasks) or append-only (Log).
REFUSED = ("Ask", "Summary", "Requirements", "Acceptance criteria", "Out of scope", "Plan", "Tasks", "Log")
ALLOWED = ("Context", "Current state", "Verification", "Findings", *LEGACY_SECTIONS)
ID = re.compile(r"[a-z][a-z0-9-]{0,39}")  # starts with a letter: an id never reads as an index
_FM_END = re.compile(r"^---[ \t]*$")
_ANY_BLOCK = re.compile(r"^ {0,3}(?:`{3,}|~{3,})[ \t]*orch[ \t]*$", re.M)


@dataclass
class Problem:
    code: str  # widget-parse | widget-schema | widget-place | widget-digest
    message: str
    level: str = "error"  # "warning": still drawn, with the warning visible (a changed file)

    def to_dict(self) -> dict:
        return {"code": self.code, "level": self.level, "message": self.message}


@dataclass
class Block:
    section: str
    line: int  # 1-based line of the opening fence: in the ticket file from `ticket_blocks`, else in the text given
    raw: str  # the text between the fences
    data: dict | None = None
    error: str | None = None  # why `raw` is not one strict JSON object
    index: int = 0  # position among the ticket's blocks (the anchor fallback; frames use `digest`)
    problems: list[Problem] = field(default_factory=list)  # filled by `validate`

    @property
    def layer(self) -> str | None:
        """"type", "widget" or "html"; None when the block does not name exactly one."""
        keys = [k for k in LAYERS if isinstance(self.data, dict) and k in self.data]
        return keys[0] if len(keys) == 1 else None

    @property
    def key(self) -> str:
        """The page anchor (`#w-<key>`): the `id`, else the index. Never a frame's address (that is `digest`)."""
        wid = (self.data or {}).get("id")
        return wid if isinstance(wid, str) and ID.fullmatch(wid) else str(self.index)

    @property
    def digest(self) -> str | None:
        """The sha256 of the block's canonical JSON: with its section, the address of its frame
        (`GET /w/<ID>/<section>/<digest>`), so a frame can only ever load the very block the page drew there."""
        if not isinstance(self.data, dict):
            return None
        return hashlib.sha256(json.dumps(self.data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                              .encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {"index": self.index, "id": (self.data or {}).get("id"), "section": self.section, "line": self.line,
                "layer": self.layer, "data": self.data, "problems": [p.to_dict() for p in self.problems]}


def placement(section: str) -> bool:
    """Whether a widget may stand in `section`: the allowed ones and any extra `## Heading` orch does not know (not
    the text before the first heading)."""
    return section in ALLOWED or bool(section) and section not in (*SECTIONS, *LEGACY_SECTIONS)


def duplicate_ids(blocks) -> set[str]:
    """The ids more than one block of the ticket uses: such blocks are refused (drawn as code, never served)."""
    seen: set[str] = set()
    dup: set[str] = set()
    for b in blocks:
        wid = (b.data or {}).get("id") if isinstance(b.data, dict) else None
        if isinstance(wid, str):
            (dup if wid in seen else seen).add(wid)
    return dup


# -- strict JSON -------------------------------------------------------------------------------------------------

def _pairs(items):
    out = {}
    for k, v in items:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _constant(name):
    raise ValueError(f"{name} is not JSON")


def _limits(value, depth: int = 1, inner: bool = False) -> str | None:
    if depth > MAX_DEPTH:
        return f"nested deeper than {MAX_DEPTH} levels"
    if isinstance(value, str):
        return f"a string longer than {MAX_STRING} characters" if len(value) > MAX_STRING else None
    if isinstance(value, list):
        cap = MAX_INNER if inner else MAX_ITEMS
        if len(value) > cap:
            return f"an array of {len(value)} items (at most {cap}{' inside an array' if inner else ''})"
        return next((p for v in value if (p := _limits(v, depth + 1, True))), None)
    if isinstance(value, dict):
        return next((p for v in value.values() if (p := _limits(v, depth + 1))), None)
    return None


def load_strict(raw: str) -> tuple[dict | None, str | None]:
    """(object, None) or (None, why): one JSON object, no duplicate keys, no NaN/Infinity, within the caps."""
    if len(raw.encode("utf-8")) > MAX_BYTES:
        return None, f"larger than {MAX_BYTES // 1024} KiB"
    try:
        data = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except RecursionError:
        return None, f"nested deeper than {MAX_DEPTH} levels"
    except ValueError as e:  # JSONDecodeError is one
        return None, f"not strict JSON ({e})"
    if not isinstance(data, dict):
        return None, "not one JSON object"
    problem = _limits(data)
    return (None, problem) if problem else (data, None)


# -- finding blocks ----------------------------------------------------------------------------------------------

def _scan(lines: list[str], *, section: str, first_line: int, track_sections: bool) -> list[Block]:
    out: list[Block] = []
    fence: str | None = None
    body: list[str] | None = None  # the open orch block's lines
    start = 0
    for n, line in enumerate(lines, first_line):
        was = fence
        fence, is_fence = fences.step(fence, line)
        if was is None and is_fence and fences.info(line) == "orch":
            body, start = [], n
        elif was is not None and fence is None and body is not None:
            out.append(make_block(section, start, "\n".join(body)))
            body = None
        elif body is not None:
            body.append(line)
        elif track_sections and fence is None and not is_fence and (h2 := h2_title(line)) is not None:
            section = h2
    if body is not None:
        out.append(Block(section, start, "\n".join(body), error="the fence is not closed"))
    return out


def make_block(section: str, line: int, raw: str) -> Block:
    data, error = load_strict(raw)
    return Block(section, line, raw, data, error)


def parse_blocks(section_text: str, section: str = "", offset: int = 0) -> list[Block]:
    """The ```orch blocks of one section's text; `line` counts from 1 + `offset`."""
    return _scan((section_text or "").split("\n"), section=section, first_line=1 + offset, track_sections=False)


def has_blocks(text: str) -> bool:
    """Cheap pre-check: could `text` hold an ```orch block at all (checks skip the rest of the tickets)."""
    return bool(_ANY_BLOCK.search(text))


def body_start(lines: list[str]) -> int:
    """Index of the first line after the frontmatter (0 when there is none)."""
    if lines and lines[0].rstrip() == "---":
        return next((i + 1 for i in range(1, len(lines)) if _FM_END.match(lines[i])), 0)
    return 0


def ticket_blocks(ticket, raw: str | None = None) -> list[Block]:
    """Every ```orch block of a ticket, with its section, its line in the file and its index. `raw` is the file as
    read (its lines are what a person opens); without it, the file as orch writes it."""
    if raw is None:
        from orch.core.model import render_ticket
        raw = render_ticket(ticket)
    lines = raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body_at = body_start(lines)
    blocks = _scan(lines[body_at:], section="", first_line=body_at + 1, track_sections=True)
    for i, b in enumerate(blocks):
        b.index = i
    return blocks

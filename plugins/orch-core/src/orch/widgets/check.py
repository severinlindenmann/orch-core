"""Write-time and dry-run checks of the widget blocks in ticket text (policy on top of the strict parser)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .limits import MAX_BLOCKS
from .parse import DEFAULT_LABELS, DEFAULT_ORDER, WIDGET_SECTIONS, WidgetSegment, parse_section, resolve_ticket_widgets

SHOWN_PROBLEMS = 3
_LABELS = {**DEFAULT_LABELS, "findings": "Findings"}
_ORDER = (*DEFAULT_ORDER, "findings")


@dataclass(frozen=True)
class Problem:
    block: int  # 1-based position among the blocks of the written section
    line: int  # 1-based line of the opening fence in that section
    reason: str

    def text(self) -> str:
        return f"widget block {self.block} (line {self.line}): {self.reason}"


def has_fence(text: str) -> bool:
    return "```" in text or "~~~" in text


def count_blocks(text: str, section: str = "context") -> int:
    return sum(isinstance(s, WidgetSegment) for s in parse_section(text, section)) if has_fence(text) else 0


def check_section_text(
    body: Mapping[str, str | None], section: str, text: str, *, templates_ok: bool = False
) -> list[Problem]:
    """Problems with the widget blocks of ``text`` as the new content of ``section``; ``body`` holds the ticket's
    other sections (the written one is replaced by ``text``). No fence, no problem: nothing is parsed."""
    if not has_fence(text):
        return []
    full = {**body, section: text}
    resolved = resolve_ticket_widgets(full, _ORDER, lambda s: _LABELS.get(s, s))
    problems: list[Problem] = []
    n = 0
    for seg in resolved.get(section, []):
        if not isinstance(seg, WidgetSegment):
            continue
        n += 1
        b = seg.block
        reason = b.reason
        if reason is None and b.spec is not None:
            if b.spec["layer"] == "html":
                reason = "agent HTML pages are not available; use a core type"
            elif b.spec["layer"] == "widget" and not templates_ok:
                reason = "templates need the Widgets add-on, which is not on in this workspace"
        if reason is None and section not in WIDGET_SECTIONS:
            reason = f"widgets are not allowed in {_LABELS.get(section, section)}"
        if reason is not None:
            problems.append(Problem(n, b.line, reason))
    return problems


def check_write(
    body: Mapping[str, str | None], section: str, text: str, *, templates_ok: bool = False
) -> list[Problem]:
    """``check_section_text`` plus the ticket-wide cap: a write may not push the ticket's total above the cap."""
    problems = check_section_text(body, section, text, templates_ok=templates_ok)
    if problems:
        return problems
    new = count_blocks(text, section)
    if new == 0:
        return []
    old = count_blocks(body.get(section) or "", section)
    others = sum(count_blocks(v or "", k) for k, v in body.items() if k != section and v)
    if new > old and others + new > MAX_BLOCKS:
        lines = [x.block.line for x in parse_section(text, section) if isinstance(x, WidgetSegment)]
        return [
            Problem(
                new, lines[-1], f"a ticket holds at most {MAX_BLOCKS} widgets; the other sections already hold {others}"
            )
        ]
    return []


def message(problems: list[Problem], section: str) -> str:
    label = _LABELS.get(section, section)
    shown = "; ".join(p.text() for p in problems[:SHOWN_PROBLEMS])
    more = f" (+{len(problems) - SHOWN_PROBLEMS} more)" if len(problems) > SHOWN_PROBLEMS else ""
    return f"{label}: {shown}{more}. The text was not stored; run: orch widgets check FILE"

"""Acceptance criteria and the evidence that proves them (ticket design review §1.3, §3.1).

Criteria are the top-level `- [ ]` / `- [x]` lines of `## Acceptance criteria`, numbered AC1…ACn in order (the same
numbering as task refs `ac:<n>`). A top-level line of `## Verification` that names `AC<n>` is evidence for that
criterion (`- AC2: curl returned 404 once (curl -i …)`; several: `- AC1, AC3: …`); indented lines and fenced blocks
under it belong to it. A criterion is proven only when it has evidence: a tick alone proves nothing, and ticking
one needs evidence first (`Ops.set_section`)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from orch.core import fences

_CRITERION = re.compile(r"^[-*+]\s+\[([ xX])\]\s+(.*)$")
_CITE = re.compile(r"(?<![A-Za-z0-9])AC\s?(\d+)(?![0-9])", re.IGNORECASE)
# "- AC1: …", "- AC1, AC3 — …": the citing prefix, left out of the evidence text
_PREFIX = re.compile(r"^(?:AC\s?\d+\s*(?:,|/|&|\+|and)?\s*)+[:—–-]\s*", re.IGNORECASE)
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
# Evidence must say something: a citing line whose text (with any indented output) has fewer than MIN_EVIDENCE
# letters or digits, or that is only a placeholder ("todo", "TBD: later", "n/a", "?", "…"), proves nothing.
MIN_EVIDENCE = 8
_PLACEHOLDER = re.compile(r"^(?:todo|to do|tbd|tba|wip|n/?a|none|pending|later|soon|fixme|xxx|tk)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Criterion:
    n: int
    text: str
    ticked: bool
    evidence: tuple[str, ...] = ()

    @property
    def proven(self) -> bool:
        return bool(self.evidence)


def _criteria_lines(text: str) -> list[tuple[str, bool]]:
    out = []
    for line in (text or "").split("\n"):
        m = None if line[:1].isspace() else _CRITERION.match(line.strip())
        if m:
            out.append((m.group(2).strip(), m.group(1) != " "))
    return out


def _blocks(text: str) -> list[str]:
    """Top-level lines of `text`, each with the indented lines and fenced blocks that follow it. Text before the
    first top-level line becomes a block of its own that starts indented (it never cites a criterion)."""
    blocks: list[list[str]] = []
    fence = None
    for line in (text or "").split("\n"):
        was = fence
        fence, is_fence = fences.step(fence, line)
        if was is not None:
            blocks[-1].append(line)
            continue
        if not line.strip():
            continue
        if blocks and (is_fence or line[:1].isspace()):
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return ["\n".join(b) for b in blocks]


def _cited(head: str) -> list[int]:
    return [] if head[:1].isspace() else [int(n) for n in _CITE.findall(head)]


def substantial(text: str) -> bool:
    """True when an evidence text says something (see MIN_EVIDENCE); placeholders and near-empty lines do not."""
    plain = text.strip()
    if _PLACEHOLDER.match(plain.lstrip("([*_`").strip()):
        return False
    return sum(ch.isalnum() for ch in plain) >= MIN_EVIDENCE


def _evidence_text(block: str) -> str:
    head, _, rest = block.partition("\n")
    head = _PREFIX.sub("", _BULLET.sub("", head.strip()))
    return head + ("\n" + rest if rest else "")


def criteria(ticket) -> list[Criterion]:
    found = _criteria_lines(ticket.section("Acceptance criteria"))
    proofs: dict[int, list[str]] = {}
    for block in _blocks(ticket.section("Verification")):
        head = block.split("\n", 1)[0]
        text = _evidence_text(block)
        if not substantial(text):
            continue
        for n in dict.fromkeys(_cited(head)):
            if 1 <= n <= len(found):
                proofs.setdefault(n, []).append(text)
    return [Criterion(i, text, ticked, tuple(proofs.get(i, ()))) for i, (text, ticked) in enumerate(found, 1)]


def other_evidence(ticket) -> list[str]:
    """Verification lines that cite no existing criterion (general evidence, shown apart)."""
    total = len(_criteria_lines(ticket.section("Acceptance criteria")))
    out = []
    for block in _blocks(ticket.section("Verification")):
        if not any(1 <= n <= total for n in _cited(block.split("\n", 1)[0])):
            out.append(_evidence_text(block))
    return out


def progress(ticket) -> tuple[int, int]:
    """(proven, total) criteria."""
    cs = criteria(ticket)
    return sum(1 for c in cs if c.proven), len(cs)


def missing(ticket) -> list[int]:
    """Numbers of the criteria without evidence."""
    return [c.n for c in criteria(ticket) if not c.proven]


def ticked_without_evidence(ticket, *, before=None) -> list[int]:
    """Criteria ticked in `ticket` that have no evidence. With `before` (the ticket as it was), only the ticks that
    are new, so an old file whose ticks predate this rule can still be edited."""
    was = {text for text, ticked in _criteria_lines(before.section("Acceptance criteria")) if ticked} if before else set()
    return [c.n for c in criteria(ticket) if c.ticked and not c.proven and c.text not in was]

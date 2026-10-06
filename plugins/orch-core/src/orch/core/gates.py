from __future__ import annotations

import hashlib
import re

from orch.clock import stamp
from orch.core.fsutil import atomic_write_text

GATE_SECTIONS = {
    "requirements": ("Requirements", "Acceptance criteria", "Out of scope"),
    "plan": ("Plan",),
}
# Hash versions. v1: the gate's sections. v2 (new approvals): the requirements gate also binds a non-empty
# `## Summary` (placed first) and the frontmatter `size` and `type`, so an agent cannot shrink a ticket below the
# plan gate after approval. v3: an artifact the gated text shows inline (`![…](artifact:<name>)`) is bound by the
# sha256 its ticket entry records (orch.core.artifacts.binding), so replacing the file invalidates the approval; without
# inline artifacts v3 equals v2. The version is stored with each approval (`hash_v`, absent = 1), so older approvals
# stay valid.
HASH_VERSION = 3
_V2_META = {"requirements": ("size", "type"), "plan": ()}
# A line in gated text that asks the human instead of stating a decision. Anchored to the start of a line (after an
# optional list marker or bold): "Open question: …", "Open question for the human: …", "Question for you? …", or TBD
# as a whole value ("Retention: TBD", "- TBD", "TBD: decide the region"). Such a gate is approved only with the
# human's explicit override; questions belong in `orch ask`. Documented in docs/ticket-schema.md.
_LEAD = r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?"
_HUMAN_QUESTION = re.compile(
    _LEAD + r"(?i:open\s+questions?(?:\s+for\s+(?:the\s+)?(?:human|you|user|owner))?"
    r"|questions?\s+for\s+(?:the\s+)?(?:human|you|user|owner))(?:\*\*|__)?\s*[:?]"
    r"|" + _LEAD + r"(?:TBD\b|[^:\n]{1,80}:\s*(?:\*\*|__)?TBD(?:\*\*|__)?\.?\s*$)")
_CHECKBOX = re.compile(r"^(\s*[-*+]\s+)\[[ xX]\]", re.M)
_BLANKS = re.compile(r"\n{3,}")


def _normalize(text: str) -> str:
    text = _CHECKBOX.sub(r"\1[ ]", text)  # ticking acceptance criteria must not invalidate the gate
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def gate_parts(ticket, gate: str, version: int = HASH_VERSION) -> list[tuple[str, str]]:
    """(section name, normalized text) for every section the gate's hash covers, in hash order. Anything that
    shows a gate for approval renders exactly these, plus `gate_meta`."""
    names = GATE_SECTIONS[gate]
    if version >= 2 and gate == "requirements" and ticket.section("Summary").strip():
        names = ("Summary",) + names
    return [(name, _normalize(ticket.section(name))) for name in names]


def gate_meta(ticket, gate: str, version: int = HASH_VERSION) -> list[tuple[str, str]]:
    """(key, value) of the frontmatter fields the gate's hash covers (v2 requirements: size and type; v3 also
    `artifact <name>` with the recorded `sha256:<hex>`, or `missing`, for each artifact the gated text shows)."""
    if version < 2:
        return []
    meta = [(key, str(ticket.meta.get(key) or "")) for key in _V2_META[gate]]
    if version >= 3:
        from orch.core.artifacts import binding
        meta += binding(ticket, [body for _, body in gate_parts(ticket, gate, version)], None)  # recorded digests only
    return meta


def gate_covers(gate: str, ticket=None, version: int = HASH_VERSION) -> list[str]:
    """The names of what the gate's hash binds: section names, then frontmatter keys (and, v3, `artifact <name>`)."""
    if ticket is None:
        return list(GATE_SECTIONS[gate]) + [k for k in _V2_META[gate] if version >= 2]
    return [n for n, _ in gate_parts(ticket, gate, version)] + [k for k, _ in gate_meta(ticket, gate, version)]


def normalized_text(ticket, gate: str, version: int = HASH_VERSION) -> str:
    text = "\n\n".join(f"## {name}\n\n{body}" for name, body in gate_parts(ticket, gate, version))
    meta = gate_meta(ticket, gate, version)
    if meta:
        text += "\n\n" + "\n".join(f"{key}: {value}" for key, value in meta)
    return text


def gate_hash(ticket, gate: str, version: int = HASH_VERSION) -> str:
    return "sha256:" + hashlib.sha256(normalized_text(ticket, gate, version).encode("utf-8")).hexdigest()


def human_questions_in(ticket, gate: str) -> list[str]:
    """Lines of the gate's text that still ask the human something (see _HUMAN_QUESTION)."""
    return [line.strip() for _, body in gate_parts(ticket, gate) for line in body.split("\n")
            if _HUMAN_QUESTION.search(line)]


def _gate(ticket, gate: str) -> dict:
    return (ticket.meta.get("gates") or {}).get(gate) or {}


def gate_state(ticket, gate: str) -> str:
    g = _gate(ticket, gate)
    if not g.get("approved"):
        return "pending"
    return "approved" if g.get("hash") in _accepted_hashes(ticket, gate, g) else "invalidated"


def invalidated_gates(ticket) -> list[str]:
    """The gates approved for text that changed since: nobody proceeds on them (an agent's work, a verdict) until the
    human approves again."""
    return [g for g in GATE_SECTIONS if gate_state(ticket, g) == "invalidated"]


def _accepted_hashes(ticket, gate: str, g: dict) -> tuple[str, ...]:
    """The current hashes an approval recorded as `g` may match. An approval stores the version it was made with
    (`hash_v`); one without it predates v2 (or comes from a writer that does not record it) and matches v1 or the
    current version, since the current one binds strictly more."""
    v = g.get("hash_v")
    if v in (None, ""):
        return (gate_hash(ticket, gate, 1), gate_hash(ticket, gate))
    try:
        return (gate_hash(ticket, gate, int(v)),)
    except (TypeError, ValueError):
        return ()


def snapshot_path(ws, ticket_id: str, gate: str):
    return ws.state_dir / "gates" / f"{ticket_id}-{gate}.md"


def record_approval(ws, ticket, gate: str, actor, *, snapshot: bool = True, **extra) -> None:
    """Mark the gate approved in the ticket and keep the approved text as a snapshot (`snapshot=False`: a dry run).
    `extra`: what else the approval names (an epic approval: `epic`; an agent's under delegation: `delegation`)."""
    ticket.meta.setdefault("gates", {})[gate] = {"approved": stamp(), "via": actor.via, "hash": gate_hash(ticket, gate),
                                                 "hash_v": HASH_VERSION, **extra}
    if snapshot:
        atomic_write_text(snapshot_path(ws, ticket.id, gate), normalized_text(ticket, gate) + "\n")


def clear_gate(ticket, gate: str) -> None:
    ticket.meta.setdefault("gates", {})[gate] = {"approved": None, "via": None, "hash": None}


def changes_pending(ticket, gate: str) -> bool:
    """True while the human's last "request changes" still matches the gate's current text.

    Once the section is edited, the hash no longer matches and the gate's approval reappears
    in needs_you() (the agent's move is done; the human needs to look again)."""
    cr = _gate(ticket, gate).get("changes_requested")
    return bool(cr) and cr.get("hash") == gate_hash(ticket, gate)


def approved_snapshot(ws, ticket_id: str, gate: str) -> str | None:
    p = snapshot_path(ws, ticket_id, gate)
    return p.read_text(encoding="utf-8") if p.exists() else None


def plan_required(ws, ticket) -> bool:
    if ticket.meta.get("type") == "epic":
        return False  # an epic has no plan of its own; its children do
    return ticket.meta.get("size") not in ws.config["gates"]["plan_skip_sizes"]


def requirements_skipped(size, requirements_skip_sizes, ticket_type=None) -> bool:
    """#172: a ticket of a size in `gates.requirements_skip_sizes` may leave the backlog without an approved
    requirements gate (and without Requirements or Acceptance criteria). Never an epic: its approval is its charter."""
    return ticket_type != "epic" and size is not None and size in tuple(requirements_skip_sizes or ())


def requirements_skip_sizes(ws) -> tuple:
    return tuple(ws.config["gates"].get("requirements_skip_sizes") or ())


def requirements_required(ws, ticket) -> bool:
    """False when the ticket's size skips the requirements gate (`gates.requirements_skip_sizes`, default none)."""
    return not requirements_skipped(ticket.meta.get("size"), requirements_skip_sizes(ws), ticket.meta.get("type"))

"""What the store writes: ``ticket.json``, ``body.md``, ``config.json`` and ``keys.jsonl`` lines, and how it reads
``body.md`` back (ticket-format §2 to §4). Pure functions of values, no filesystem.

Decisions where F1 is silent:

* **Pretty-printing** (§3 "pretty-printed with a fixed key order"): two-space indent, ``ensure_ascii=False``, one
  final LF. The 17 top-level keys are in the §3 order; nested objects of the known shapes (links, acceptance items,
  tasks, questions, options, pull requests) use the order of the §3 example, every other object (``branches``,
  ``addons``, ``config.json`` maps) sorts its keys.
* **body.md** (§4): ``## <heading>`` plus a blank line, the text and a blank line, per section in table order; an
  empty section is ``## <heading>`` plus a blank line only. The file ends with exactly one LF after the last
  non-empty text, and an empty body (no section) is a zero-byte file. Headings are the bare names (``Out of scope``,
  not the table's explanations in brackets). Reading is tolerant of every other layout: a section is what lies
  between its heading and the next one with leading and trailing LF removed.
* **Addon sections** need the manifest's heading, which only C9 has; until then writing one is refused
  (``validation.addon_section``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from orch import canon
from orch.schema import SECTIONS_BY_TYPE

__all__ = [
    "HEADINGS",
    "BodyError",
    "keys_line",
    "parse_body",
    "refs_of",
    "render_body",
    "render_config",
    "render_ticket",
    "section_entry",
    "thaw",
]

HEADINGS = {
    "summary": "Summary",
    "context": "Context",
    "requirements": "Requirements",
    "out_of_scope": "Out of scope",
    "plan": "Plan",
    "decisions": "Decisions",
    "verification": "Verification",
    "findings": "Findings",
    "current_state": "Current state",
}
_ORDER = tuple(HEADINGS)
_BY_HEADING = {h: s for s, h in HEADINGS.items()}
TICKET_KEYS = (
    "schema",
    "uid",
    "key",
    "title",
    "type",
    "priority",
    "size",
    "labels",
    "parent",
    "blocked_by",
    "due",
    "visibility",
    "links",
    "acceptance",
    "tasks",
    "questions",
    "addons",
)
_ITEM_ORDER = {
    "links": ("repos", "branches", "prs", "external"),
    "prs": ("repo", "url"),
    "acceptance": ("id", "text"),
    "tasks": ("id", "text", "verify", "proves", "assignee"),
    "questions": ("id", "to", "text", "why", "options", "recommended", "blocking"),
    "options": ("key", "label", "cost"),
    "policy": ("approvers", "count", "not", "applies", "independent"),
}
_REFS = re.compile(r"\(artifact:([A-Za-z0-9][A-Za-z0-9._-]{0,127})\)")
_FENCE = re.compile(r"(`{3,}|~{3,})")


class BodyError(ValueError):
    """``body.md`` cannot be read as sections (unknown or repeated heading, text before the first heading, an open
    fence at the end, bytes that are not UTF-8)."""


def thaw(o: Any) -> Any:
    """Plain dicts and lists from the model's frozen views (mapping proxies and tuples)."""
    if isinstance(o, Mapping):
        return {k: thaw(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [thaw(v) for v in o]
    return o


def _ordered(name: str | None, o: Any) -> Any:
    """``o`` with dict keys in the fixed order of its shape (sorted when the shape has none)."""
    if isinstance(o, dict):
        order = _ITEM_ORDER.get(name or "")
        keys = [k for k in order if k in o] + sorted(k for k in o if k not in order) if order else sorted(o)
        return {k: _ordered(k if k in _ITEM_ORDER else None, o[k]) for k in keys}
    if isinstance(o, list):
        return [_ordered(name, v) for v in o]
    return o


def _pretty(doc: Any) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False, separators=(",", ": ")) + "\n").encode("utf-8")


def ticket_doc(uid: str, key: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    plain = thaw(fields)
    doc = {"schema": "orch.ticket/2", "uid": uid, "key": key, **plain}
    return {k: _ordered(k, doc[k]) for k in TICKET_KEYS}


def render_ticket(uid: str, key: str, fields: Mapping[str, Any]) -> bytes:
    """``ticket.json`` (§3): all 17 keys in the fixed order, rebuilt from what the events say."""
    return _pretty(ticket_doc(uid, key, fields))


def render_config(view: Any, *, host_id: str, wsk_pub: str, name: str, run_for: list[str]) -> bytes:
    """``config.json`` (§2) from a :class:`~orch.model.WorkspaceView` and the genesis' ``host_id`` and ``wsk_pub``.
    ``name`` and ``agents.run_for`` are not in any event: they come from the existing file (or the creator's
    default)."""
    doc = {
        "schema": "orch.workspace/2",
        "workspace": {"id": view.workspace_id, "prefix": view.prefix, "name": name},
        "host": {"id": host_id, "wsk_pub": wsk_pub},
        "members": [{"person": m.person, "name": m.name, "role": m.role} for m in view.members.values()],
        "gates": {g: _ordered("policy", thaw(view.policies[g])) for g in ("requirements", "plan", "verify", "code")},
        "settings": {
            "grant_hours": view.settings["grant_hours"],
            "claim_ttl_min": view.settings["claim_ttl_min"],
            "lease_ttl_min": view.settings["lease_ttl_min"],
            "repos": {r: {"path": p} for r, p in sorted(view.repos.items())},
        },
        "agents": {"run_for": run_for},
        "addons": {n: {"enabled": bool(a["enabled"])} for n, a in sorted(view.addons.items())},
    }
    return _pretty(doc)


def keys_line(key: str, uid: str, at: str) -> bytes:
    """One ``keys.jsonl`` line (§2): ``cj`` plus LF."""
    return canon.cj_checked({"key": key, "uid": uid, "at": at}) + b"\n"


def refs_of(text: str) -> list[str]:
    """The artifact names a section text references (§5.8): sorted, unique, plain regex over the text."""
    return sorted(set(_REFS.findall(text)))


def section_entry(text: str) -> dict[str, Any]:
    """``{"hash", "refs"}`` of one section text, the value of an edit event's ``sections`` map."""
    return {"hash": canon.section_hash(text), "refs": refs_of(text)}


def render_body(ticket_type: str, sections: Mapping[str, str]) -> str:
    """``body.md`` for the given section texts (empty texts are written as empty sections)."""
    allowed = SECTIONS_BY_TYPE[ticket_type]
    for sid in sections:
        if "." in sid:
            raise BodyError(f"addon section {sid!r} needs its manifest heading (C9)")
        if sid not in allowed:
            raise BodyError(f"a {ticket_type} ticket has no section {sid!r}")
    parts = []
    last_has_text = False
    for sid in _ORDER:
        if sid not in sections:
            continue
        text = sections[sid]
        last_has_text = bool(text)
        parts.append(f"## {HEADINGS[sid]}\n\n" + (f"{text}\n\n" if text else ""))
    out = "".join(parts)
    return out[:-1] if out and last_has_text else out


def parse_body(raw: bytes | str, ticket_type: str) -> dict[str, str]:
    """The section texts of a ``body.md`` (§4), exactly as the host would read them: heading lines are ``## `` at
    column 0 outside a code fence; the text is what follows up to the next heading with leading and trailing LF
    removed. Raises :class:`BodyError` for anything §4 refuses. Line endings and Unicode form are the caller's
    business (normalise first)."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError as e:
        raise BodyError(f"not UTF-8: {e}") from None
    allowed = SECTIONS_BY_TYPE[ticket_type]
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    fence: str | None = None

    def close() -> None:
        if current is not None:
            out[current] = "\n".join(buf).strip("\n")

    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is None:
            if line.startswith("## "):
                close()
                heading = line[3:]
                sid = _BY_HEADING.get(heading)
                if sid is None or sid not in allowed:
                    raise BodyError(f"unknown section heading {line!r}")
                if sid in out:
                    raise BodyError(f"section {heading!r} appears twice")
                current, buf = sid, []
                continue
            if m:
                fence = m.group(1)
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line[m.end() :].strip(" "):
            fence = None
        if current is None:
            if line:
                raise BodyError("text before the first heading")
            continue
        buf.append(line)
    if fence is not None:
        raise BodyError("a code fence is still open at the end of the file")
    close()
    return out

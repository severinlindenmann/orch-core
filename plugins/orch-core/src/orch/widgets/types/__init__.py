"""Core widget types, one module each (registry.core_types finds them). Shared bits for their HTML live here."""
from __future__ import annotations

import re

from orch.widgets.render import esc

ROLES = ("ok", "info", "warn", "err", "neu")
ROLE = {"enum": list(ROLES)}
LABEL = {"type": "string", "minLength": 1, "maxLength": 200}
# A role is never shown by colour alone: a mark the eye sees and a word a screen reader says.
MARK = {"ok": ("✓", "good"), "info": ("i", "note"), "warn": ("!", "warning"), "err": ("✕", "problem"), "neu": ("·", "")}


def mark(role: str | None) -> str:
    if not role or role == "neu":
        return ""
    glyph, word = MARK[role]
    return f'<span class="w-mark" aria-hidden="true">{glyph}</span><span class="w-sr">{word}: </span>'


def role_class(role: str | None) -> str:
    return f" w-r-{esc(role)}" if role else ""


def scope(ctx):
    """The artifact scope widget text is rendered with: the ticket's id and no linked files. A widget's JSON sits in a
    fence, so the artifacts it would show are not in the token stream the gate and verdict hashes bind
    (orch.core.artifacts.binding): an `artifact:` image or link inside widget text stays inert, and a link to the
    ticket's own /a/ files is inert by R24 (canonical_link). Pinned files are shown only by the media types, by digest."""
    from orch.dashboard.markdown import ArtifactScope
    return ArtifactScope(str(getattr(getattr(ctx, "ticket", None), "id", "") or ""), {})


def safe_url(url: str, ctx=None) -> str | None:
    """R24: the href a widget link may have, decided by the same rule as every link in ticket Markdown
    (orch.dashboard.markdown.canonical_link): web and mailto links, in-page anchors, clean dashboard paths and another
    ticket's canonical artifact URL. Anything else (other schemes, this ticket's /a/ files, dot segments, ...) stays
    text."""
    from orch.dashboard.markdown import canonical_link
    kind, href = canonical_link(url, scope(ctx))
    return None if kind == "inert" else href

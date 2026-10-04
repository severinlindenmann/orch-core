"""`health`: on track / at risk / blocked with the reason. Data: {"health": on_track|at_risk|blocked, "why"}."""
from __future__ import annotations

from orch.widgets.render import esc, inline
from orch.widgets.types import role_class

NAME = "health"
MOMENT = "report"
STATES = {"on_track": ("ok", "✓", "On track"), "at_risk": ("warn", "!", "At risk"), "blocked": ("err", "✕", "Blocked")}
SCHEMA = {"properties": {"health": {"enum": list(STATES)}, "why": {"type": "string", "minLength": 1, "maxLength": 2000}},
          "required": ["health", "why"]}
EXAMPLE = {"type": "health", "health": "at_risk", "why": "The vendor API still rate-limits us; the retry work is not in yet."}


def render_html(block, ctx) -> str:
    d = block.data
    role, glyph, word = STATES[d["health"]]
    return (f'<div class="w-health{role_class(role)}"><span class="w-health-word"><span class="w-mark" '
            f'aria-hidden="true">{glyph}</span>{esc(word)}</span><span class="w-health-why">{inline(d["why"], ctx)}</span></div>')


def render_text(block, ctx) -> str:
    return f'{STATES[block.data["health"]][2]}: {block.data["why"]}'

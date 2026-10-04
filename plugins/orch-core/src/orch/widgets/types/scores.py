"""`scores`: 0-100 scores as rings, banded like Lighthouse: 0-49 poor, 50-89 needs work, 90-100 good. Data:
{"items": {name: number 0..100}} (1-12). Each ring shows the number, the band as a mark and a word under it; the
ring's arc is the score (stroke-dasharray on a circle of circumference 100)."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL, mark, role_class
from orch.widgets.types._chartkit import fmt, p

NAME = "scores"
MOMENT = "report"
ALT = "numbers"
SCHEMA = {"properties": {"items": {"type": "object", "minProperties": 1, "maxProperties": 12, "propertyNames": LABEL,
                                   "additionalProperties": {"type": "number", "minimum": 0, "maximum": 100}}},
          "required": ["items"]}
EXAMPLE = {"type": "scores", "title": "Lighthouse", "items": {"performance": 91, "accessibility": 100, "SEO": 72, "PWA": 38}}


def band(score: float) -> tuple[str, str]:
    return ("err", "Poor") if score < 50 else ("warn", "Needs work") if score < 90 else ("ok", "Good")


def render_html(block, ctx) -> str:
    cells = []
    for name, s in block.data["items"].items():
        role, word = band(s)
        cells.append(
            f'<li class="w-score{role_class(role)}"><svg viewBox="0 0 36 36" role="img" aria-label="{esc(name)} {fmt(s)} of 100">'
            f'<circle class="w-ring" cx="18" cy="18" r="15.9155" pathLength="100"/>'
            f'<circle class="w-arc" cx="18" cy="18" r="15.9155" pathLength="100" stroke-dasharray="{p(s)} 100"/>'
            f'<text x="18" y="22.5" text-anchor="middle">{esc(fmt(s))}</text></svg>'
            f'<span class="w-score-n">{esc(name)}</span><span class="w-score-b">{mark(role)}{word}</span></li>')
    return f'<ul class="w-scores">{"".join(cells)}</ul>'


def render_text(block, ctx) -> str:
    return "\n".join(f"{n}: {fmt(s)}/100 ({band(s)[1]})" for n, s in block.data["items"].items())

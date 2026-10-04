"""`stats`: headline numbers with an optional delta (its direction shown by an arrow and a sign, not by colour)."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL, ROLE, mark, role_class

NAME = "stats"
MOMENT = "report"
ALT = "numbers"
_VALUE = {"type": ["string", "number"], "maxLength": 200}
SCHEMA = {"properties": {"items": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
    "type": "object", "additionalProperties": False, "required": ["label", "value"],
    "properties": {"label": LABEL, "value": _VALUE, "delta": _VALUE, "role": ROLE}}}}, "required": ["items"]}
EXAMPLE = {"type": "stats", "title": "Bundle", "source": "npm run size",
           "items": [{"label": "main.js", "value": "286 kB", "delta": -126, "role": "ok"}, {"label": "routes", "value": 14}]}


def _delta(value) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("▲ +" if value > 0 else "▼ " if value < 0 else "") + f"{value:g}"
    return str(value)


def render_html(block, ctx) -> str:
    cells = []
    for it in block.data["items"]:
        delta = f'<dd class="w-stat-d">{esc(_delta(it["delta"]))}</dd>' if "delta" in it else ""
        cells.append(f'<div class="w-stat{role_class(it.get("role"))}"><dt>{esc(it["label"])}</dt>'
                     f'<dd class="w-stat-v">{mark(it.get("role"))}{esc(it["value"])}</dd>{delta}</div>')
    return f'<dl class="w-stats">{"".join(cells)}</dl>'


def render_text(block, ctx) -> str:
    return "\n".join(f'{it["label"]}: {it["value"]}' + (f' ({_delta(it["delta"])})' if "delta" in it else "")
                     for it in block.data["items"])

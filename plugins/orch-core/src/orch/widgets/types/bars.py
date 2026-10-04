"""`bars`: horizontal labelled bars, each as long as its value (the longest fills the row; values are >= 0).
Data: {"data": {label: number} or [[label, number], ..] (1-100 bars), "unit"?, "highlight"?: a label}. The
highlighted bar gets a marker and the word, not just a hue. Every value is printed at its bar."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL
from orch.widgets.types._chartkit import POS, UNIT, p, with_unit

NAME = "bars"
MOMENT = "report"
ALT = "numbers"
SCHEMA = {"properties": {"unit": UNIT, "highlight": LABEL, "data": {"oneOf": [
    {"type": "object", "minProperties": 1, "maxProperties": 100, "propertyNames": LABEL, "additionalProperties": POS},
    {"type": "array", "minItems": 1, "maxItems": 100, "items": {
        "type": "array", "minItems": 2, "maxItems": 2, "prefixItems": [LABEL, POS]}}]}}, "required": ["data"]}
EXAMPLE = {"type": "bars", "title": "Bundle size", "source": "size.txt", "unit": "kB", "highlight": "branch",
           "data": {"main": 412, "branch": 286}}


def pairs(data) -> list[tuple[str, float]]:
    return [(k, v) for k, v in data.items()] if isinstance(data, dict) else [(k, v) for k, v in data]


def render_html(block, ctx) -> str:
    d = block.data
    rows = pairs(d["data"])
    top = max(v for _, v in rows)
    out = []
    for label, v in rows:
        hot = label == d.get("highlight")
        mark = '<span class="w-mark" aria-hidden="true">▸</span><span class="w-sr">highlighted: </span>' if hot else ""
        width = p(v / top * 100) if top else "0"
        out.append(f'<li class="w-bar{" w-hot" if hot else ""}"><span class="w-bar-l">{mark}{esc(label)}</span>'
                   f'<span class="w-bar-t"><i style="width:{width}%"></i></span>'
                   f'<span class="w-bar-v">{esc(with_unit(v, d.get("unit")))}</span></li>')
    return f'<ul class="w-bars">{"".join(out)}</ul>'


def render_text(block, ctx) -> str:
    d = block.data
    return "\n".join(f'{k}: {with_unit(v, d.get("unit"))}' + (" (highlighted)" if k == d.get("highlight") else "")
                     for k, v in pairs(d["data"]))

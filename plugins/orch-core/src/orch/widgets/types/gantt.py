"""`gantt`: lanes of [start, end] intervals on one shared scale. Data: {"lanes": {name: [[start, end], ..]} (1-30
lanes, <= 100 intervals each, start <= end), "unit"?: of the scale}. Ticks are round numbers in the unit, printed
above the lanes; each interval is a bar positioned by its numbers. Overlapping intervals of one lane are packed
onto extra rows. The text alternative lists every interval."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL
from orch.widgets.types._chartkit import NUM, UNIT, fmt, nice_ticks, p, pct, with_unit

NAME = "gantt"
MOMENT = "plan"
ALT = "numbers"
SCHEMA = {"properties": {"unit": UNIT, "lanes": {"type": "object", "minProperties": 1, "maxProperties": 30,
    "propertyNames": LABEL, "additionalProperties": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
        "type": "array", "minItems": 2, "maxItems": 2, "prefixItems": [NUM, NUM]}}}}, "required": ["lanes"]}
EXAMPLE = {"type": "gantt", "title": "Agents, minutes into the run", "unit": "min", "lanes": {
    "build": [[0, 12], [30, 41]], "tests": [[12, 30]], "review": [[28, 45]]}}


def _span(d) -> tuple[float, float]:
    flat = [v for ivs in d["lanes"].values() for iv in ivs for v in iv]
    return min(flat), max(flat)


def _rows(ivs):
    """Greedy packing of intervals onto rows without overlap -> [(row, start, end)]."""
    ends: list[float] = []
    out = []
    for a, b in sorted((min(i), max(i)) for i in ivs):
        row = next((r for r, e in enumerate(ends) if e <= a), None)
        if row is None:
            ends.append(b)
            row = len(ends) - 1
        else:
            ends[row] = b
        out.append((row, a, b))
    return out, len(ends)


def render_html(block, ctx) -> str:
    d = block.data
    lo, hi = _span(d)
    ticks = nice_ticks(lo, hi) if hi > lo else [lo]
    axis = "".join(f'<span style="left:{p(pct(t, lo, hi))}%">{esc(fmt(t))}</span>' for t in ticks)
    grid = "".join(f'<i style="left:{p(pct(t, lo, hi))}%"></i>' for t in ticks)
    lanes = []
    for name, ivs in d["lanes"].items():
        placed, nrows = _rows(ivs)
        bars = "".join(
            f'<b class="w-gb" style="left:{p(pct(a, lo, hi))}%;width:{p(max(pct(b, lo, hi) - pct(a, lo, hi), 0.8))}%;'
            f'top:calc({r} * var(--w-gh))" title="{esc(with_unit(a, d.get("unit")))} – {esc(with_unit(b, d.get("unit")))}">'
            f'<span class="w-gv" aria-hidden="true">{esc(fmt(a))}–{esc(with_unit(b, d.get("unit")))}</span></b>'
            for r, a, b in placed)
        lanes.append(f'<li class="w-lane"><span class="w-lane-n">{esc(name)}</span>'
                     f'<span class="w-lane-t" style="--rows:{nrows}">{grid}{bars}</span></li>')
    unit = f'<p class="w-meta">Scale in {esc(d["unit"])}</p>' if d.get("unit") else ""
    return f'<div class="w-gantt"><div class="w-gaxis" aria-hidden="true">{axis}</div><ul>{"".join(lanes)}</ul>{unit}</div>'


def render_text(block, ctx) -> str:
    d = block.data
    u = d.get("unit")
    return "\n".join(f"{n}: " + ", ".join(f"{fmt(a)}–{with_unit(b, u)}" for a, b in sorted(ivs))
                     for n, ivs in d["lanes"].items())

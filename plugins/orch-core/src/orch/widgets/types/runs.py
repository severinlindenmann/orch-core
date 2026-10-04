"""`runs`: repeated measurements as columns in run order (3-200 values, >= 0), with a median line. Column height is
the value; values are printed on top while there are at most 12 columns (else the smallest, largest and every
marked one). Data: {"values": [..], "unit"?, "marks"?: {"<1-based run>": note}}. A marked run carries a number badge
over its column and its note in the list under the chart (never colour alone)."""
from __future__ import annotations

import statistics

from orch.widgets.render import esc
from orch.widgets.types._chartkit import POS, UNIT, fmt, p, with_unit

NAME = "runs"
MOMENT = "debug"
ALT = "numbers"
SCHEMA = {"properties": {"unit": UNIT, "values": {"type": "array", "minItems": 1, "maxItems": 200, "items": POS},
                         "marks": {"type": "object", "maxProperties": 50, "propertyNames": {"pattern": "^[1-9][0-9]{0,2}$"},
                                   "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 200}}},
          "required": ["values"]}
EXAMPLE = {"type": "runs", "title": "Cold start, ms", "unit": "ms", "values": [412, 405, 398, 880, 401, 409],
           "marks": {"4": "disk cache was cold"}}
MAX_BW = 28
W, H, TOP, BOT = 360, 150, 26, 4 # viewBox units; the text stays readable when the chart scales to 390 px


def _marks(d) -> dict[int, str]:
    return {int(k): v for k, v in (d.get("marks") or {}).items() if 1 <= int(k) <= len(d["values"])}


def render_html(block, ctx) -> str:
    d = block.data
    vals, marks = d["values"], _marks(d)
    n, top = len(vals), max(vals) or 1
    slot = W / n
    bw = min(slot * 0.7, MAX_BW)  # few runs stay slim columns, not slabs
    med = statistics.median(vals)
    shown = set(range(1, n + 1)) if n <= 12 else {vals.index(max(vals)) + 1, vals.index(min(vals)) + 1, *marks}
    cols = []
    for i, v in enumerate(vals, 1):
        h = v / top * (H - TOP - BOT)
        x, y = (i - 1) * slot + (slot - bw) / 2, H - BOT - h
        cols.append(f'<rect class="w-col{" w-marked" if i in marks else ""}" x="{p(x)}" y="{p(y)}" width="{p(bw)}" '
                    f'height="{p(h)}"/>')
        if i in shown:
            cols.append(f'<text class="w-val" x="{p(x + bw / 2)}" y="{p(y - 3)}" text-anchor="middle">{esc(fmt(v))}</text>')
        if i in marks:
            cols.append(f'<text class="w-badge" x="{p(x + bw / 2)}" y="{p(y - 14)}" text-anchor="middle">▼{i}</text>')
    my = H - BOT - med / top * (H - TOP - BOT)
    cols.append(f'<line class="w-med" x1="0" x2="{W}" y1="{p(my)}" y2="{p(my)}"/>')
    label = f'median {with_unit(med, d.get("unit"))}'
    aria = esc(f"{n} runs, {label}, from {fmt(min(vals))} to {fmt(max(vals))}")  # nested f-strings need 3.12
    svg = f'<svg class="w-chart" viewBox="0 0 {W} {H}" role="img" aria-label="{aria}">{"".join(cols)}</svg>'
    notes = "".join(f'<li><b>Run {i}</b> ({esc(with_unit(vals[i - 1], d.get("unit")))}): {esc(t)}</li>'
                    for i, t in sorted(marks.items()))
    return (svg + f'<p class="w-meta">{n} runs · {esc(label)}</p>' + (f'<ul class="w-marks">{notes}</ul>' if notes else ""))


def render_text(block, ctx) -> str:
    d = block.data
    u = d.get("unit")
    lines = [f'{len(d["values"])} runs, median {with_unit(statistics.median(d["values"]), u)}',
             "Runs: " + ", ".join(fmt(v) for v in d["values"]) + (f" ({u})" if u else "")]
    lines += [f"Run {i}: {t}" for i, t in sorted(_marks(d).items())]
    return "\n".join(lines)

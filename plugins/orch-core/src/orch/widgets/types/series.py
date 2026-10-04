"""`series`: a line over an ordered x with event markers. Data: {"points": [[x, y], ..] (2-500, x and y numbers; drawn
in x order), "unit"?: of y, "markers"?: [{"x": number, "label"}] (<= 20)}. No axis to misread: the first, last,
lowest and highest points carry their value (a label too close to a shown one is left out; render_text has all), the first and last x sit under the line, and each marker is a dashed
vertical rule with its label on a row above the plot (three rows, so neighbours do not overlap)."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL
from orch.widgets.types._chartkit import NUM, UNIT, fmt, p, with_unit

NAME = "series"
MOMENT = "debug"
ALT = "numbers"
SCHEMA = {"properties": {"unit": UNIT, "points": {"type": "array", "minItems": 2, "maxItems": 500, "items": {
    "type": "array", "minItems": 2, "maxItems": 2, "prefixItems": [NUM, NUM]}},
    "markers": {"type": "array", "maxItems": 20, "items": {
        "type": "object", "additionalProperties": False, "required": ["x", "label"],
        "properties": {"x": NUM, "label": LABEL}}}}, "required": ["points"]}
EXAMPLE = {"type": "series", "title": "p95 latency", "unit": "ms", "points": [[1, 120], [2, 118], [3, 131], [4, 210],
           [5, 205], [6, 126], [7, 119]], "markers": [{"x": 4, "label": "cache change"}, {"x": 6, "label": "revert"}]}
W, H, LEFT, ROWS, ROW_H = 360, 190, 14, 3, 12
CLIP = 22


def _anchor(x: float) -> str:
    return "start" if x < 50 else "end" if x > W - 50 else "middle"


def _clip(s: str) -> str:
    return s if len(s) <= CLIP else s[:CLIP - 1] + "…"


def render_html(block, ctx) -> str:
    d = block.data
    pts = sorted(d["points"])
    marks = sorted(d.get("markers") or [], key=lambda m: m["x"])
    xs = [x for x, _ in pts] + [m["x"] for m in marks]
    x0, x1 = min(xs), max(xs)
    ys = [y for _, y in pts]
    y0, y1 = min(ys), max(ys)
    top = ROWS * ROW_H + 26 if marks else 16  # room for a high label under the marker rows
    bot = H - 18

    def X(v): return LEFT + ((v - x0) / (x1 - x0) * (W - 2 * LEFT) if x1 > x0 else (W - 2 * LEFT) / 2)
    def Y(v): return (top + bot) / 2 if y1 == y0 else bot - (v - y0) / (y1 - y0) * (bot - top)

    out = [f'<polyline class="w-line" points="{" ".join(f"{p(X(x))},{p(Y(y))}" for x, y in pts)}"/>']
    for i, m in enumerate(marks):
        x, row = X(m["x"]), i % ROWS
        ty = 10 + row * ROW_H
        out.append(f'<line class="w-rule" x1="{p(x)}" x2="{p(x)}" y1="{ty + 2}" y2="{p(bot)}"/>'
                   f'<text class="w-mk" x="{p(x)}" y="{ty}" text-anchor="{_anchor(x)}"><title>{esc(m["label"])}</title>'
                   f'{esc(_clip(m["label"]))}</text>')
    shown: list[int] = []
    def span(i):  # where the label's text runs along x, in viewBox units (about 7 per character)
        x, w = X(pts[i][0]), 7 * len(with_unit(pts[i][1], d.get("unit")))
        a = _anchor(x)
        return (x, x + w) if a == "start" else (x - w, x) if a == "end" else (x - w / 2, x + w / 2)

    for i in (ys.index(y1), ys.index(y0), 0, len(pts) - 1):  # high and low win; first and last skip when they would touch one
        a0, a1 = span(i)
        if all(a1 + 6 < b0 or b1 + 6 < a0 for b0, b1 in map(span, shown)):
            shown.append(i)
    for i in sorted(shown):
        x, y = pts[i]
        above = Y(y) - top > 11 or i == ys.index(y1)
        out.append(f'<circle class="w-pt" cx="{p(X(x))}" cy="{p(Y(y))}" r="3"/>'
                   f'<text class="w-val" x="{p(X(x))}" y="{p(Y(y) + (-6 if above else 13))}" text-anchor="{_anchor(X(x))}">'
                   f'{esc(with_unit(y, d.get("unit")))}</text>')
    for v in (pts[0][0], pts[-1][0]):
        out.append(f'<text class="w-xl" x="{p(X(v))}" y="{H - 4}" text-anchor="{"start" if v == pts[0][0] else "end"}">'
                   f'{esc(fmt(v))}</text>')
    label = (f'{len(pts)} points from {with_unit(pts[0][1], d.get("unit"))} to {with_unit(pts[-1][1], d.get("unit"))}, '
             f'low {with_unit(y0, d.get("unit"))}, high {with_unit(y1, d.get("unit"))}'
             + (f', {len(marks)} markers' if marks else ""))
    return f'<svg class="w-chart" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(label)}">{"".join(out)}</svg>'


def render_text(block, ctx) -> str:
    d = block.data
    u = d.get("unit")
    lines = [f"{len(d['points'])} points" + (f" ({u})" if u else ""),
             "; ".join(f"{fmt(x)}: {fmt(y)}" for x, y in sorted(d["points"]))]
    lines += [f'Marker at {fmt(m["x"])}: {m["label"]}' for m in sorted(d.get("markers") or [], key=lambda m: m["x"])]
    return "\n".join(lines)

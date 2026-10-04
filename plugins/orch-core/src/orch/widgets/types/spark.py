"""`spark`: a word-sized sparkline inside a sentence. Data: {"values": [2-200 numbers], "text": "CI {spark} now 6m"}
where `{spark}` (exactly once) is replaced by the line, drawn at text height with the last point dotted. Min and
max are in its aria-label; the text alternative lists every value."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types._chartkit import NUM, fmt, p

NAME = "spark"
MOMENT = "report"
ALT = "numbers"
SCHEMA = {"properties": {"values": {"type": "array", "minItems": 2, "maxItems": 200, "items": NUM},
                         "text": {"type": "string", "minLength": 8, "maxLength": 500,
                                  "pattern": r"^(?:(?!\{spark\})[\s\S])*\{spark\}(?:(?!\{spark\})[\s\S])*$"}},
          "required": ["values", "text"]}
EXAMPLE = {"type": "spark", "values": [14, 13, 11, 9, 9, 6], "text": "CI time {spark} now 6 min, was 14"}
W, H, PAD = 80, 16, 2


def _svg(vals) -> str:
    lo, hi = min(vals), max(vals)
    pts = [(PAD + i * (W - 2 * PAD) / (len(vals) - 1),
            H / 2 if hi == lo else H - PAD - (v - lo) / (hi - lo) * (H - 2 * PAD)) for i, v in enumerate(vals)]
    line = " ".join(f"{p(x)},{p(y)}" for x, y in pts)
    label = f"trend of {len(vals)} values from {fmt(vals[0])} to {fmt(vals[-1])}, min {fmt(lo)}, max {fmt(hi)}"
    return (f'<svg class="w-spark" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(label)}">'
            f'<polyline points="{line}"/><circle cx="{p(pts[-1][0])}" cy="{p(pts[-1][1])}" r="2"/></svg>')


def render_html(block, ctx) -> str:
    before, after = block.data["text"].split("{spark}")
    return f'<p class="w-p">{esc(before)}{_svg(block.data["values"])}{esc(after)}</p>'


def render_text(block, ctx) -> str:
    d = block.data
    vals = d["values"]
    return d["text"].replace("{spark}", f"[{', '.join(fmt(v) for v in vals)}]") + \
        f"\nmin {fmt(min(vals))}, max {fmt(max(vals))}, last {fmt(vals[-1])}"

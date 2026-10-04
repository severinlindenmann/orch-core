"""`bullet`: one value against a target on a banded scale. Data: {"value", "target", "bands": [good, ok, bad],
"unit"?, "lower_is_better"?: bool (default false)}. `bands` are the limits of the zones, best first, and `bad` is
the far end of the scale: lower_is_better -> good <= `good` < ok <= `ok` < bad zone up to `bad` (e.g. [250, 300, 400]);
otherwise good >= `good`, ok >= `ok`, bad from `bad` up to there (e.g. [90, 70, 0]). The scale spans every number
given. The verdict is a word with a mark under the bar ("Good", "OK", "Bad"), and the value and target are printed."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import mark, role_class
from orch.widgets.types._chartkit import NUM, UNIT, fmt, p, pct, with_unit

NAME = "bullet"
MOMENT = "decide"
ALT = "numbers"
SCHEMA = {"properties": {"value": NUM, "target": NUM, "unit": UNIT, "lower_is_better": {"type": "boolean"},
                         "bands": {"type": "array", "minItems": 3, "maxItems": 3, "items": NUM}},
          "required": ["value", "target", "bands"]}
EXAMPLE = {"type": "bullet", "title": "Bundle size", "unit": "kB", "value": 286, "target": 300, "lower_is_better": True,
           "bands": [300, 350, 450]}
WORDS = {"ok": "Good", "warn": "OK", "err": "Bad"}  # the roles: good / ok / bad


def zones(d) -> tuple[float, float, list[tuple[str, float, float]], str]:
    """(lo, hi, [(role, from, to)] left to right, verdict role of the value)."""
    good, ok, bad = d["bands"]
    nums = [d["value"], d["target"], good, ok, bad]
    lo, hi = min(nums), max(nums)
    room = (hi - lo) * 0.15  # the good zone is open-ended: leave room for it on its side
    if d.get("lower_is_better"):
        lo -= room
        z = [("ok", lo, good), ("warn", good, ok), ("err", ok, hi)]
    else:
        hi += room
        z = [("err", lo, ok), ("warn", ok, good), ("ok", good, hi)]
    v = d["value"]
    if d.get("lower_is_better"):
        role = "ok" if v <= good else "warn" if v <= ok else "err"
    else:
        role = "ok" if v >= good else "warn" if v >= ok else "err"
    return lo, hi, z, role


def render_html(block, ctx) -> str:
    d = block.data
    lo, hi, z, role = zones(d)
    u = d.get("unit")
    segs = "".join(f'<i class="w-zone w-r-{r}" style="left:{p(pct(a, lo, hi))}%;width:{p(pct(b, lo, hi) - pct(a, lo, hi))}%"></i>'
                   for r, a, b in z)
    return (f'<div class="w-bullet{role_class(role)}"><div class="w-bl-track">{segs}'
            f'<i class="w-bl-val" style="width:{p(pct(d["value"], lo, hi))}%"></i>'
            f'<i class="w-bl-tgt" style="left:{p(pct(d["target"], lo, hi))}%"></i></div>'
            f'<p class="w-bl-read"><b class="w-bl-v">{mark(role)}{esc(with_unit(d["value"], u))}</b> '
            f'<span>{WORDS[role]}</span> · target {esc(with_unit(d["target"], u))}'
            f'{" (lower is better)" if d.get("lower_is_better") else ""}</p></div>')


def render_text(block, ctx) -> str:
    d = block.data
    lo, hi, z, role = zones(d)
    u = d.get("unit")
    names = {"ok": "good", "warn": "ok", "err": "bad"}
    zt = ", ".join(f"{names[r]} {fmt(a)}–{fmt(b)}" for r, a, b in z)
    return (f'{with_unit(d["value"], u)} against a target of {with_unit(d["target"], u)}'
            f'{" (lower is better)" if d.get("lower_is_better") else ""}: {WORDS[role]}\nScale {zt}')


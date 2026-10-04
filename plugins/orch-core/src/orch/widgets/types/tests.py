"""`tests`: what a change did to the test suite, as a delta and never just totals. Data: {"added", "fixed", "broke":
integers >= 0, "flaky"?: integer, "total"?: integer (the suite size after), "names"?: {"added"|"fixed"|"broke"|
"flaky": [test names]} (<= 50 each)}. Four tiles, each a glyph, a count and a word; zero counts are muted so a
break is the first thing the eye finds. Names listed under the tiles are the evidence."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import mark

NAME = "tests"
MOMENT = "verify"
ALT = "numbers"
_N = {"type": "integer", "minimum": 0, "maximum": 1000000}
_NAMES = {"type": "array", "maxItems": 50, "items": {"type": "string", "minLength": 1, "maxLength": 200}}
KINDS = (("broke", "err", "broke"), ("fixed", "ok", "fixed"), ("added", "info", "added"), ("flaky", "warn", "flaky"))
SCHEMA = {"properties": {"added": _N, "fixed": _N, "broke": _N, "flaky": _N, "total": _N, "names": {
    "type": "object", "additionalProperties": False, "properties": {k: _NAMES for k, _, _ in KINDS}}},
    "required": ["added", "fixed", "broke"]}
EXAMPLE = {"type": "tests", "title": "Suite delta", "source": "pytest -q", "added": 6, "fixed": 2, "broke": 1, "flaky": 0,
           "total": 1284, "names": {"broke": ["tests/test_cli.py::test_move_refused"], "fixed": ["tests/test_x.py::test_a"]}}


def render_html(block, ctx) -> str:
    d = block.data
    tiles, lists = [], []
    for key, role, word in KINDS:
        if key not in d:
            continue
        n = d[key]
        live = f" w-r-{role}" if n else " w-zero"
        tiles.append(f'<div class="w-tt{live}"><dt>{word}</dt><dd class="w-tt-n">{mark(role) if n else ""}{n}</dd></div>')
        if names := (d.get("names") or {}).get(key):
            lists.append(f'<div class="w-r-{role} w-tn"><b>{word}</b><ul>'
                         + "".join(f"<li><code>{esc(x)}</code></li>" for x in names) + "</ul></div>")
    total = f'<p class="w-meta">{d["total"]} tests in the suite after the change</p>' if "total" in d else ""
    return f'<dl class="w-tests">{"".join(tiles)}</dl>{"".join(lists)}{total}'


def render_text(block, ctx) -> str:
    d = block.data
    head = ", ".join(f"{d[k]} {w}" for k, _, w in KINDS if k in d) + (f" (of {d['total']} tests)" if "total" in d else "")
    lines = [head]
    for k, _, w in KINDS:
        lines += [f"{w}: {x}" for x in (d.get("names") or {}).get(k, [])]
    return "\n".join(lines)

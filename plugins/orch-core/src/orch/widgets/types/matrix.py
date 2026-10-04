"""`matrix`: options (rows) x criteria (columns). A cell is a score 0-5 or a tick "✓" / "✗". The total of a row is
the sum of cell x weight (a score counts as itself, ✓ as 1, ✗ as 0; weights default to 1), computed here, never
supplied. `pick` names a row and is marked "Recommended". A row shorter than the criteria shows "–" for the gap."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL

NAME = "matrix"
MOMENT = "decide"
ALT = "numbers"
_CELL = {"anyOf": [{"type": "integer", "minimum": 0, "maximum": 5}, {"enum": ["✓", "✗"]}]}
SCHEMA = {"properties": {
    "criteria": {"type": "array", "minItems": 1, "maxItems": 12, "items": LABEL},
    "rows": {"type": "object", "minProperties": 1, "maxProperties": 12, "propertyNames": {"maxLength": 200},
             "additionalProperties": {"type": "array", "maxItems": 12, "items": _CELL}},
    "weights": {"type": "array", "maxItems": 12, "items": {"type": "number", "minimum": 0, "maximum": 100}},
    "pick": {"type": "string", "maxLength": 200}}, "required": ["criteria", "rows"]}
EXAMPLE = {"type": "matrix", "criteria": ["Effort", "Risk", "Reversible"], "weights": [1, 2, 1], "pick": "Queue",
           "rows": {"Patch": [4, 2, "✓"], "Queue": [2, 4, "✓"], "Rewrite": [0, 3, "✗"]}}


def _num(v) -> int:
    return {"✓": 1, "✗": 0}.get(v, v) if isinstance(v, str) else v


def totals(d: dict) -> dict[str, float]:
    w = d.get("weights") or []
    return {name: sum(_num(v) * (w[i] if i < len(w) else 1) for i, v in enumerate(cells[:len(d["criteria"])]))
            for name, cells in d["rows"].items()}


def _cell(v) -> str:
    if v == "✓":
        return '<td class="w-mx-tick"><span aria-hidden="true">✓</span><span class="w-sr">yes</span></td>'
    if v == "✗":
        return '<td class="w-mx-tick"><span aria-hidden="true">✗</span><span class="w-sr">no</span></td>'
    if v is None:
        return '<td class="w-num">–</td>'
    return f'<td class="w-num"><span class="w-mx-bar" style="--v:{v}" aria-hidden="true"></span>{v}<span class="w-sr"> of 5</span></td>'


def render_html(block, ctx) -> str:
    d, tot = block.data, totals(block.data)
    n = len(d["criteria"])
    head = "".join(f'<th scope="col">{esc(c)}</th>' for c in d["criteria"])
    rows = []
    for name, cells in d["rows"].items():
        pick = name == d.get("pick")
        flag = ' <span class="w-rec"><span class="w-mark" aria-hidden="true">★</span>Recommended</span>' if pick else ""
        body = "".join(_cell(cells[i] if i < len(cells) else None) for i in range(n))
        rows.append(f'<tr{" class=w-mx-pick" if pick else ""}><th scope="row">{esc(name)}{flag}</th>{body}'
                    f'<td class="w-num w-mx-total">{tot[name]:g}</td></tr>')
    return (f'<div class="w-scroll"><table class="w-table w-matrix"><thead><tr><th scope="col">Option</th>{head}'
            f'<th scope="col">Total</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render_text(block, ctx) -> str:
    d, tot = block.data, totals(block.data)
    lines = ["Criteria: " + ", ".join(d["criteria"]) + (f' (weights: {", ".join(f"{x:g}" for x in d["weights"])})' if d.get("weights") else "")]
    lines += [f'{name}: {" ".join(str(c) for c in cells)} = {tot[name]:g}' + (" [recommended]" if name == d.get("pick") else "")
              for name, cells in d["rows"].items()]
    return "\n".join(lines)

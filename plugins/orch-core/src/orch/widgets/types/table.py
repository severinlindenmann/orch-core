"""`table`: columns and rows; scrolls sideways inside itself on a phone, never the page."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL

NAME = "table"
MOMENT = "understand"
ALT = "numbers"
_CELL = {"type": ["string", "number", "boolean", "null"], "maxLength": 2000}
SCHEMA = {"properties": {
    "columns": {"type": "array", "minItems": 1, "maxItems": 50, "items": LABEL},
    "rows": {"type": "array", "maxItems": 500, "items": {"type": "array", "maxItems": 50, "items": _CELL}}},
    "required": ["columns", "rows"]}
EXAMPLE = {"type": "table", "title": "Endpoints", "columns": ["Route", "p95 ms"], "rows": [["/t/{id}", 41], ["/", 18]]}


def _cell(v) -> str:
    return "" if v is None else ("yes" if v is True else "no" if v is False else str(v))


def _numeric(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def render_html(block, ctx) -> str:
    d = block.data
    head = "".join(f'<th scope="col">{esc(c)}</th>' for c in d["columns"])
    rows = "".join("<tr>" + "".join(f'<td{" class=w-num" if _numeric(v) else ""}>{esc(_cell(v))}</td>' for v in r)
                   + "</tr>" for r in d["rows"])
    return (f'<div class="w-scroll"><table class="w-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')


def render_text(block, ctx) -> str:
    d = block.data
    return "\n".join("\t".join(_cell(v) for v in r) for r in [d["columns"], *d["rows"]])

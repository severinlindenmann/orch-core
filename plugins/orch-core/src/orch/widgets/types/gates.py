"""`gates`: build/test/lint gates in order, each with a status and how long it took. Data: {"items": [{"name",
"status": pass|fail|skip|running, "seconds"?: number >= 0}]} (1-50). The status is a glyph and a word; the duration
bar is proportional to seconds (the longest gate fills the track) and the time is printed as well."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL
from orch.widgets.types._chartkit import POS, duration, fmt, p

NAME = "gates"
MOMENT = "verify"
ALT = "numbers"
STATUS = {"pass": ("ok", "✓", "Pass"), "fail": ("err", "✕", "Fail"), "skip": ("neu", "–", "Skipped"),
          "running": ("info", "…", "Running")}
SCHEMA = {"properties": {"items": {"type": "array", "minItems": 1, "maxItems": 50, "items": {
    "type": "object", "additionalProperties": False, "required": ["name", "status"],
    "properties": {"name": LABEL, "status": {"enum": list(STATUS)}, "seconds": POS}}}}, "required": ["items"]}
EXAMPLE = {"type": "gates", "title": "CI", "items": [{"name": "build", "status": "pass", "seconds": 94},
           {"name": "pytest", "status": "fail", "seconds": 212}, {"name": "lint", "status": "skip"}]}


def render_html(block, ctx) -> str:
    items = block.data["items"]
    top = max((i.get("seconds", 0) for i in items), default=0)
    rows = []
    for it in items:
        role, glyph, word = STATUS[it["status"]]
        s = it.get("seconds")
        bar = f'<i style="width:{p(s / top * 100) if top else 0}%"></i>' if s is not None else ""
        rows.append(f'<li class="w-gate w-r-{role}"><span class="w-gate-s"><span class="w-mark" aria-hidden="true">{glyph}</span>{word}</span>'
                    f'<span class="w-gate-n">{esc(it["name"])}</span><span class="w-gate-t">{bar}</span>'
                    f'<span class="w-gate-d">{esc(duration(s)) if s is not None else ""}</span></li>')
    return f'<ul class="w-gates">{"".join(rows)}</ul>'


def render_text(block, ctx) -> str:
    return "\n".join(f'{i["name"]}: {STATUS[i["status"]][2]}' + (f' in {duration(i["seconds"])} ({fmt(i["seconds"])}s)'
                     if "seconds" in i else "") for i in block.data["items"])

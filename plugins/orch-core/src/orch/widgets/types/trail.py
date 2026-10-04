"""`trail`: the breadcrumb events before a failure, oldest first. Data: {"items": [{"t", "kind", "text"}]}; `t` is
the time as written, `kind` is a short word (nav, click, request, log, error, ...) with its own icon; an error
kind is drawn as a problem (icon, word and colour)."""
from __future__ import annotations

from orch.widgets.render import esc

NAME = "trail"
MOMENT = "debug"
ICONS = {"nav": "→", "click": "◉", "tap": "◉", "input": "✎", "request": "⇄", "response": "⇠", "log": "≡", "warn": "!",
         "error": "✕"}
SCHEMA = {"properties": {"items": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
    "type": "object", "additionalProperties": False, "required": ["t", "kind", "text"],
    "properties": {"t": {"type": "string", "minLength": 1, "maxLength": 40},
                   "kind": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,19}$"},
                   "text": {"type": "string", "minLength": 1, "maxLength": 500}}}}}, "required": ["items"]}
EXAMPLE = {"type": "trail", "items": [{"t": "12:00:01", "kind": "nav", "text": "/board"},
                                       {"t": "12:00:03", "kind": "click", "text": "Approve plan"},
                                       {"t": "12:00:03", "kind": "error", "text": "409 plan changed"}]}


def render_html(block, ctx) -> str:
    out = []
    for it in block.data["items"]:
        role = {"error": "err", "warn": "warn"}.get(it["kind"], "")
        out.append(f'<li class="w-trail-i{" w-r-" + role if role else ""}"><time class="w-trail-t">{esc(it["t"])}</time>'
                   f'<span class="w-trail-k"><span class="w-mark" aria-hidden="true">{ICONS.get(it["kind"], "•")}</span>'
                   f'{esc(it["kind"])}</span><span class="w-trail-x">{esc(it["text"])}</span></li>')
    return f'<ol class="w-trail">{"".join(out)}</ol>'


def render_text(block, ctx) -> str:
    return "\n".join(f'{it["t"]} {it["kind"]}: {it["text"]}' for it in block.data["items"])

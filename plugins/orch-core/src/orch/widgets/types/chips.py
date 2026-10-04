"""`chips`: tags or scope (files, routes, areas)."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL, ROLE, mark, role_class

NAME = "chips"
MOMENT = "understand"
SCHEMA = {"properties": {"items": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"oneOf": [
    LABEL, {"type": "object", "additionalProperties": False, "required": ["text"],
            "properties": {"text": LABEL, "role": ROLE}}]}}}, "required": ["items"]}
EXAMPLE = {"type": "chips", "title": "Touches", "items": ["src/auth.py", {"text": "sessions table", "role": "warn"}]}


def _items(block):
    return [{"text": it} if isinstance(it, str) else it for it in block.data["items"]]


def render_html(block, ctx) -> str:
    chips = "".join(f'<li class="w-chip-item{role_class(it.get("role"))}">{mark(it.get("role"))}{esc(it["text"])}</li>'
                    for it in _items(block))
    return f'<ul class="w-chips">{chips}</ul>'


def render_text(block, ctx) -> str:
    return ", ".join(it["text"] + (f' ({it["role"]})' if it.get("role") else "") for it in _items(block))

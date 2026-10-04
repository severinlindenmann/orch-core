"""`callout`: one sentence the reader must not miss; the role is said in a word, not only in colour."""
from __future__ import annotations

from orch.widgets.render import inline
from orch.widgets.types import ROLE, mark, role_class

NAME = "callout"
MOMENT = "understand"
SCHEMA = {"properties": {"role": ROLE, "text": {"type": "string", "minLength": 1, "maxLength": 2000}},
          "required": ["role", "text"]}
EXAMPLE = {"type": "callout", "role": "warn", "text": "The migration locks the orders table for about 40 s."}


def render_html(block, ctx) -> str:
    d = block.data
    return f'<p class="w-callout{role_class(d["role"])}">{mark(d["role"])}{inline(d["text"], ctx)}</p>'


def render_text(block, ctx) -> str:
    d = block.data
    return f'{d["role"]}: {d["text"]}'

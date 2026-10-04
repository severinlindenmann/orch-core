"""`text`: a paragraph, Markdown inline."""
from __future__ import annotations

from orch.widgets.render import inline

NAME = "text"
MOMENT = "understand"
SCHEMA = {"properties": {"text": {"type": "string", "minLength": 1, "maxLength": 20000}}, "required": ["text"]}
EXAMPLE = {"type": "text", "text": "The cache is **cold** after every deploy; see the runs below."}


def render_html(block, ctx) -> str:
    return f'<p class="w-p">{inline(block.data["text"], ctx)}</p>'


def render_text(block, ctx) -> str:
    return block.data["text"]

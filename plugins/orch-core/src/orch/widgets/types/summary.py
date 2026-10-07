"""`summary`: the handoff as structured fields instead of a paragraph, short on purpose. The verdict drawer pins the
first one on top. Data: {"delivered", "check_first", "open"?: [str], "not_done"?}. Text is Markdown inline, with a hard
length limit."""
from __future__ import annotations

from orch.widgets.render import esc, inline

NAME = "summary"
MOMENT = "understand"
ALT = "text"
_TEXT = {"type": "string", "minLength": 1, "maxLength": 400}
SCHEMA = {"properties": {"delivered": _TEXT, "check_first": _TEXT, "not_done": _TEXT,
                         "open": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 160}}},
          "required": ["delivered", "check_first"]}
EXAMPLE = {"type": "summary", "delivered": "10 post drafts, a preview page and the storyline",
           "check_first": "Open linkedin/index.html and mark the posts to keep",
           "open": ["Landing-page URL is a placeholder", "Sign-off needed before post 01"], "not_done": "Scheduling (out of scope)"}
ROWS = (("delivered", "Delivered"), ("check_first", "Check first"), ("open", "Open points"), ("not_done", "Not done"))


def fields(d: dict) -> list[tuple[str, object]]:
    return [(label, d[key]) for key, label in ROWS if d.get(key)]


def render_html(block, ctx) -> str:
    rows = []
    for label, value in fields(block.data):
        if isinstance(value, list):
            cells = " ".join(f'<span class="w-chip w-r-warn">{esc(v)}</span>' for v in value)
        else:
            cells = str(inline(value, ctx))
        rows.append(f"<dt>{esc(label)}</dt><dd>{cells}</dd>")
    return f'<dl class="w-summary">{"".join(rows)}</dl>'


def render_text(block, ctx) -> str:
    return "\n".join(f'{label}: ' + ("; ".join(v) if isinstance(v, list) else v) for label, v in fields(block.data))

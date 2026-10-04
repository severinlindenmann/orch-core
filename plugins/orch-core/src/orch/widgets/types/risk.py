"""`risk`: flags (auth, data, money, ...). Data: {"items": {flag: true | false | "note"}}. true or a note means
flagged (mark and word "Flagged", the note after it); false means clear."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import role_class

NAME = "risk"
MOMENT = "decide"
SCHEMA = {"properties": {"items": {"type": "object", "minProperties": 1, "maxProperties": 30,
                                   "propertyNames": {"minLength": 1, "maxLength": 100},
                                   "additionalProperties": {"type": ["boolean", "string"], "maxLength": 500}}},
          "required": ["items"]}
EXAMPLE = {"type": "risk", "items": {"auth": False, "data": "adds a column to orders", "money": False, "migration": True}}


def _flagged(v) -> bool:
    return v is True or (isinstance(v, str) and v != "")


def _note(v) -> str:
    return v if isinstance(v, str) else ""


def render_html(block, ctx) -> str:
    out = []
    for flag, v in block.data["items"].items():
        on = _flagged(v)
        word = '<span class="w-mark" aria-hidden="true">!</span>Flagged' if on else '<span class="w-mark" aria-hidden="true">✓</span>Clear'
        note = f'<span class="w-risk-n">{esc(_note(v))}</span>' if _note(v) else ""
        out.append(f'<li class="w-risk{role_class("warn" if on else "ok")}"><span class="w-risk-f">{esc(flag)}</span>'
                   f'<span class="w-risk-w">{word}</span>{note}</li>')
    return f'<ul class="w-risks">{"".join(out)}</ul>'


def render_text(block, ctx) -> str:
    return "\n".join(f'{flag}: {"flagged" if _flagged(v) else "clear"}' + (f' — {_note(v)}' if _note(v) else "")
                     for flag, v in block.data["items"].items())

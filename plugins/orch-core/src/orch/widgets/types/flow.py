"""`flow`: a linear flow, one step highlighted ("You are here" in words), optional notes keyed by step text.
Data: {"steps": [str], "highlight"?: a step text, "notes"?: {step text: note}}. Wraps on a phone."""
from __future__ import annotations

from orch.widgets.render import esc

NAME = "flow"
MOMENT = "understand"
_S = {"type": "string", "minLength": 1, "maxLength": 200}
SCHEMA = {"properties": {"steps": {"type": "array", "minItems": 2, "maxItems": 20, "uniqueItems": True, "items": _S},
                         "highlight": _S, "notes": {"type": "object", "maxProperties": 20, "propertyNames": {"maxLength": 200},
                                                    "additionalProperties": {"type": "string", "maxLength": 500}}},
          "required": ["steps"]}
EXAMPLE = {"type": "flow", "steps": ["Sign in", "Pick a plan", "Pay", "Receipt"], "highlight": "Pay",
           "notes": {"Pay": "the card step fails on a 3-D Secure redirect"}}


def render_html(block, ctx) -> str:
    d, notes, out = block.data, block.data.get("notes") or {}, []
    for i, s in enumerate(d["steps"], 1):
        here = s == d.get("highlight")
        flag = '<span class="w-flow-here"><span class="w-mark" aria-hidden="true">▶</span>You are here</span>' if here else ""
        n = f'<span class="w-flow-n">{esc(notes[s])}</span>' if s in notes else ""
        out.append(f'<li class="w-flow-s{" w-flow-on" if here else ""}"{" aria-current=step" if here else ""}>'
                   f'<span class="w-flow-i">{i}</span><span class="w-flow-t">{esc(s)}</span>{flag}{n}</li>')
    return f'<ol class="w-flow">{"".join(out)}</ol>'


def render_text(block, ctx) -> str:
    notes = block.data.get("notes") or {}
    return "\n".join(f'{i}. {s}' + (" [you are here]" if s == block.data.get("highlight") else "")
                     + (f' — {notes[s]}' if s in notes else "") for i, s in enumerate(block.data["steps"], 1))

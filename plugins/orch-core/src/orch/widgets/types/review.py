"""`review`: a verdict per acceptance criterion for the person reading, on the ticket's own criteria. Each row has
✓ ok, ✕ not met and ? open; marking one is only a note on this page (nothing is stored; it is never a verdict). "Send back flagged" opens Send back with the ✕ criteria already chosen. Data: {"acs"?: [n]},
the criteria to list (default all)."""
from __future__ import annotations

from orch.widgets.render import esc

NAME = "review"
MOMENT = "verify"
ALT = "text"
SCHEMA = {"properties": {"acs": {"type": "array", "minItems": 1, "maxItems": 100, "uniqueItems": True,
                                  "items": {"type": "integer", "minimum": 1, "maximum": 999}}}}
EXAMPLE = {"type": "review", "acs": [1, 2, 3]}


def _rows(block, ctx) -> list[tuple[int, str]]:
    from orch.core import evidence
    if ctx.ticket is None or not hasattr(ctx.ticket, "section"):
        return []
    found = [(c.n, c.text) for c in evidence.criteria(ctx.ticket)]
    wanted = block.data.get("acs")
    return [(n, t) for n, t in found if not wanted or n in wanted]


def render_html(block, ctx) -> str:
    rows = _rows(block, ctx)
    if not rows:
        return '<p class="w-note w-note-neu" role="note">This ticket has no acceptance criteria to review.</p>'
    items = []
    for n, text in rows:
        radios = "".join(
            f'<label class="w-rv-opt w-rv-{key}"><input type="radio" name="rv-{n}" value="{key}"{" checked" if key == "open" else ""}>'
            f'<span><span aria-hidden="true">{glyph}</span> {word}</span></label>'
            for key, glyph, word in (("ok", "✓", "ok"), ("no", "✕", "not met"), ("open", "?", "open")))
        items.append(f'<li data-ac="{n}"><span class="w-rv-t"><b>AC{n}</b> {esc(text)}</span>'
                     f'<span class="w-rv-tri" role="radiogroup" aria-label="AC{n}">{radios}</span></li>')
    return (f'<div class="w-review" data-review><ul class="w-rv">{"".join(items)}</ul>'
            '<div class="w-rv-sum"><span class="w-rv-count" aria-live="polite"></span>'
            '<button type="button" class="btn btn-sm" data-review-send hidden>Send back flagged…</button></div></div>')


def render_text(block, ctx) -> str:
    return "\n".join(f"AC{n}: {text}" for n, text in _rows(block, ctx)) or "no acceptance criteria"

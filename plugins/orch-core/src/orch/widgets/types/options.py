"""`options`: the choices as cards with cost and risk; `pick` (an item id) is marked "Recommended" with a star and
the word. Data: {"pick"?: id, "items": [{"id", "title", "cost"?, "risk"?, "notes"?}]}; ids are unique."""
from __future__ import annotations

from orch.widgets.render import esc, inline

NAME = "options"
MOMENT = "decide"
_ID = {"type": "string", "minLength": 1, "maxLength": 40}
SCHEMA = {"properties": {"pick": _ID, "items": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True, "items": {
    "type": "object", "additionalProperties": False, "required": ["id", "title"],
    "properties": {"id": _ID, "title": {"type": "string", "minLength": 1, "maxLength": 200},
                   "cost": {"type": "string", "maxLength": 200}, "risk": {"type": "string", "maxLength": 200},
                   "notes": {"type": "string", "maxLength": 1000}}}}}, "required": ["items"]}
EXAMPLE = {"type": "options", "pick": "b", "items": [
    {"id": "a", "title": "Patch the cache", "cost": "1 day", "risk": "keeps the race"},
    {"id": "b", "title": "Queue the writes", "cost": "3 days", "risk": "new moving part", "notes": "Removes the race."}]}


def render_html(block, ctx) -> str:
    d, cards = block.data, []
    for it in d["items"]:
        rec = it["id"] == d.get("pick")
        flag = '<span class="w-rec"><span class="w-mark" aria-hidden="true">★</span>Recommended</span>' if rec else ""
        facts = "".join(f"<div><dt>{k}</dt><dd>{esc(it[key])}</dd></div>"
                        for k, key in (("Cost", "cost"), ("Risk", "risk")) if it.get(key))
        notes = f'<p class="w-opt-n">{inline(it["notes"], ctx)}</p>' if it.get("notes") else ""
        cards.append(f'<li class="w-opt{" w-opt-pick" if rec else ""}">{flag}<p class="w-opt-t">'
                     f'<code>{esc(it["id"])}</code> {esc(it["title"])}</p>'
                     f'{"<dl class=w-opt-f>" + facts + "</dl>" if facts else ""}{notes}</li>')
    return f'<ul class="w-opts">{"".join(cards)}</ul>'


def render_text(block, ctx) -> str:
    d = block.data
    return "\n".join(f'{it["id"]}: {it["title"]}' + "".join(f' · {k}: {it[key]}' for k, key in (("cost", "cost"), ("risk", "risk")) if it.get(key))
                     + (f' — {it["notes"]}' if it.get("notes") else "") + (" [recommended]" if it["id"] == d.get("pick") else "")
                     for it in d["items"])

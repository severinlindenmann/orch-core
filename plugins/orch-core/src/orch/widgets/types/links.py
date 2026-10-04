"""`links`: "look here" links, each with what the reader should expect to find."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL, safe_url

NAME = "links"
MOMENT = "review"
SCHEMA = {"properties": {"items": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
    "type": "object", "additionalProperties": False, "required": ["label", "url"],
    "properties": {"label": LABEL, "url": {"type": "string", "minLength": 1, "maxLength": 2000},
                   "as": {"type": "string", "maxLength": 40}, "expect": {"type": "string", "maxLength": 500}}}}},
    "required": ["items"]}
EXAMPLE = {"type": "links", "items": [{"label": "Preview", "url": "https://example.test/pr/12", "as": "page",
                                       "expect": "the new empty state on the board"}]}


def _link(it, ctx=None) -> str:
    url = safe_url(it["url"], ctx)
    if not url:  # javascript:, data: and friends stay text
        return f'{esc(it["label"])} <code>{esc(it["url"])}</code>'
    if url.lower().startswith("http"):
        return f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(it["label"])} ↗</a>'
    return f'<a href="{esc(url)}">{esc(it["label"])}</a>'


def render_html(block, ctx) -> str:
    rows = []
    for it in block.data["items"]:
        kind = f' <span class="w-meta">{esc(it["as"])}</span>' if it.get("as") else ""
        expect = f'<span class="w-expect">Expect: {esc(it["expect"])}</span>' if it.get("expect") else ""
        rows.append(f"<li>{_link(it, ctx)}{kind}{expect}</li>")
    return f'<ul class="w-links">{"".join(rows)}</ul>'


def render_text(block, ctx) -> str:
    return "\n".join(f'{it["label"]}: {it["url"]}' + (f' (expect: {it["expect"]})' if it.get("expect") else "")
                     for it in block.data["items"])

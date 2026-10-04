"""`screens`: a grid of screenshots or mockups, each pinned by digest. Data: {"items": [{"label", "path", "sha256"}],
"columns"?: 1-6 (default 3; one column on a phone)}. Label screenshots by viewport and theme ("390 px dark")."""
from __future__ import annotations

from orch.widgets.types import LABEL, _media
from orch.widgets.types.compare import shot

NAME = "screens"
MOMENT = "review"
ALT = "numbers"
_ITEM = _media.file_schema({"label": LABEL})
_ITEM["required"] = ["label", *_media.REQUIRED]
SCHEMA = {"properties": {"items": {"type": "array", "minItems": 1, "maxItems": 24, "items": _ITEM},
                         "columns": {"type": "integer", "minimum": 1, "maximum": 6}}, "required": ["items"]}
EXAMPLE = {"type": "screens", "columns": 2, "items": [
    {"label": "1100 px light", "path": "artifacts/B-0001/wide-light.png", "sha256": "0" * 64},
    {"label": "390 px dark", "path": "artifacts/B-0001/phone-dark.png", "sha256": "0" * 64}]}


def render_html(block, ctx) -> str:
    d = block.data
    return (f'<div class="w-screens" style="--cols:{int(d.get("columns", 3))}">'
            + "".join(shot(ctx, it, it["label"]) for it in d["items"]) + "</div>")


def render_text(block, ctx) -> str:
    return "\n".join(f'{it["label"]}: {it["path"]} (sha256 {it["sha256"][:12]})' for it in block.data["items"])

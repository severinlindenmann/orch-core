"""`compare`: two images side by side (static, no slider). Data: {"before": {"path", "sha256"}, "after": {...},
"labels"?: [before label, after label]}. Files are ticket artifacts pinned by digest (a changed file draws with the
chrome's warning, a missing one is an error). Also holds the .w-shot styles screens and video share."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import LABEL, _media

NAME = "compare"
MOMENT = "review"
ALT = "numbers"
SCHEMA = {"properties": {"before": _media.file_schema(), "after": _media.file_schema(),
                         "labels": {"type": "array", "minItems": 2, "maxItems": 2, "items": LABEL}},
          "required": ["before", "after"]}
EXAMPLE = {"type": "compare", "before": {"path": "artifacts/B-0001/before.png", "sha256": "0" * 64},
           "after": {"path": "artifacts/B-0001/after.png", "sha256": "0" * 64}, "labels": ["Before", "After"]}


def labels(d: dict) -> list[str]:
    return d.get("labels") or ["Before", "After"]


def shot(ctx, file: dict, label: str) -> str:
    src = _media.uri(ctx, file)
    img = f'<img src="{esc(src)}" alt="{esc(label)}" loading="lazy">' if src else _media.unavailable(esc(label))
    return f'<figure class="w-shot">{img}<figcaption>{esc(label)}</figcaption></figure>'


def render_html(block, ctx) -> str:
    d = block.data
    return f'<div class="w-compare">{shot(ctx, d["before"], labels(d)[0])}{shot(ctx, d["after"], labels(d)[1])}</div>'


def render_text(block, ctx) -> str:
    d = block.data
    return "\n".join(f'{lab}: {d[k]["path"]} (sha256 {d[k]["sha256"][:12]})' for k, lab in zip(("before", "after"), labels(d)))

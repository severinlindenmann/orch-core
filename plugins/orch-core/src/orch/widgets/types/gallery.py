"""`gallery`: many outputs of one criterion as a grid of thumbnails, each pinned by digest. An image opens in a
lightbox (← → move, Esc closes); an HTML, PDF or other file is a labelled tile that links to the file, with an optional
pinned `poster` image as its thumbnail (the dashboard takes no snapshots itself). Data: {"items": [{"label", "path",
"sha256", "poster"?: {"path", "sha256"}}], "columns"?: 2-6 (default 4)}."""
from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

from orch.widgets import artifacts
from orch.widgets.render import esc
from orch.widgets.types import LABEL, _media

NAME = "gallery"
MOMENT = "review"
ALT = "numbers"
_ITEM = _media.file_schema({"label": LABEL, "poster": _media.file_schema()})
_ITEM["required"] = ["label", *_media.REQUIRED]
SCHEMA = {"properties": {"items": {"type": "array", "minItems": 1, "maxItems": 60, "items": _ITEM},
                         "columns": {"type": "integer", "minimum": 2, "maximum": 6}}, "required": ["items"]}
EXAMPLE = {"type": "gallery", "columns": 4, "items": [
    {"label": "Post 01 · Hook", "path": "artifacts/B-0001/post-01.png", "sha256": "0" * 64},
    {"label": "Landing A", "path": "artifacts/B-0001/landing-a.html", "sha256": "0" * 64,
     "poster": {"path": "artifacts/B-0001/landing-a.png", "sha256": "0" * 64}}]}


def _ext(item: dict) -> str:
    return PurePosixPath(str(item["path"])).suffix.lstrip(".").upper() or "FILE"


def _is_image(item: dict) -> bool:
    return mimetypes.guess_type(str(item["path"]))[0] in artifacts.IMAGE_TYPES


def _tile(ctx, item: dict) -> str:
    label = esc(item["label"])
    if _is_image(item):
        src = _media.uri(ctx, item)
        if not src:
            return f'<li class="w-gal-item">{_media.unavailable(label)}</li>'
        return (f'<li class="w-gal-item"><a class="w-gal-thumb" href="{esc(src)}" data-lightbox="{label}">'
                f'<img src="{esc(src)}" alt="{label}" loading="lazy" decoding="async"></a>'
                f'<span class="w-gal-cap">{label}</span></li>')
    # not an image: a poster stands in for it, the file itself opens on its own
    href = _file_href(ctx, item)
    poster = _media.uri(ctx, item["poster"]) if item.get("poster") else None
    thumb = (f'<img src="{esc(poster)}" alt="" loading="lazy" decoding="async">' if poster
             else f'<span class="w-gal-type">{esc(_ext(item))}</span>')
    kind = f'<span class="w-gal-kind">{esc(_ext(item))}</span>' if poster else ""  # the thumbnail already says it
    if not href:
        return f'<li class="w-gal-item">{_media.unavailable(label)}</li>'
    return (f'<li class="w-gal-item"><a class="w-gal-thumb" href="{esc(href)}" target="_blank" rel="noopener">{thumb}{kind}</a>'
            f'<span class="w-gal-cap">{label} ↗</span></li>')


def _file_href(ctx, item: dict) -> str | None:
    """The link of a pinned file that is not an image: only on a page (a standalone document embeds images only), and
    only while the bytes still match the digest the block pins."""
    from urllib.parse import quote
    tid = getattr(ctx.ticket, "id", None)
    if ctx.standalone or not isinstance(tid, str):
        return None
    path = artifacts.resolve(ctx.ws, tid, item["path"])
    digest = item.get("sha256")
    if path is None or not digest or artifacts.sha256(path) != digest:
        return None
    return f"/a/{quote(tid)}/{quote(artifacts.name_of(tid, item['path']))}?v={digest}"


def render_html(block, ctx) -> str:
    d = block.data
    return (f'<ul class="w-gallery" style="--cols:{int(d.get("columns", 4))}">'
            + "".join(_tile(ctx, it) for it in d["items"]) + "</ul>")


def render_text(block, ctx) -> str:
    return "\n".join(f'{it["label"]}: {it["path"]} (sha256 {it["sha256"][:12]})' for it in block.data["items"])

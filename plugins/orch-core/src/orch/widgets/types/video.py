"""`video`: a recording in a native <video controls preload="metadata">. Data: {"path", "sha256", "poster"?: {"path",
"sha256"}}; both digest-pinned ticket artifacts. On the dashboard the file is the /a/ route (page CSP: media falls back
to default-src 'self', so it plays); a standalone document embeds it as a data: URI up to 5 MB and says so otherwise."""
from __future__ import annotations

from orch.widgets.render import esc
from orch.widgets.types import _media

NAME = "video"
MOMENT = "review"
ALT = "numbers"
SCHEMA = {"properties": {**_media.file_schema()["properties"], "poster": _media.file_schema()}, "required": list(_media.REQUIRED)}
EXAMPLE = {"type": "video", "path": "artifacts/B-0001/run.mp4", "sha256": "0" * 64,
           "poster": {"path": "artifacts/B-0001/run.png", "sha256": "0" * 64}}


def render_html(block, ctx) -> str:
    d = block.data
    src = _media.uri(ctx, d, _media.VIDEO_TYPES)
    if not src:
        return _media.unavailable("the recording" + (" (over 5 MB, too big to embed in a document)" if ctx.standalone else ""))
    poster = _media.uri(ctx, d["poster"]) if d.get("poster") else None
    attr = f' poster="{esc(poster)}"' if poster else ""
    return (f'<video class="w-video" controls preload="metadata" src="{esc(src)}"{attr}>'
            f'The recording is {esc(d["path"])}.</video>')


def render_text(block, ctx) -> str:
    d = block.data
    return f'Recording: {d["path"]} (sha256 {d["sha256"][:12]})'

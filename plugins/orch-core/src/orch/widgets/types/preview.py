"""`preview`: a local HTML artifact inside the ticket page, in a frame with the artifact route's own sandbox (no
scripts, no network, an opaque origin), with viewport toggles. A page whose scripts matter opens in its own tab through
the link. Data: {"path", "sha256"} (a ticket artifact pinned by digest) or {"url"} (a web link: shown as a link, never
framed), "viewports"?: [px, ...] (default [390, 1280]), "height"?: 120-1200 (default 480)."""
from __future__ import annotations

from urllib.parse import quote

from orch.widgets import artifacts
from orch.widgets.render import esc
from orch.widgets.types import LABEL, _media, safe_url

NAME = "preview"
MOMENT = "review"
ALT = "text"
SCHEMA = {"properties": {
    "path": _media.file_schema()["properties"]["path"], "sha256": _media.SHA,
    "url": {"type": "string", "minLength": 1, "maxLength": 2000}, "label": LABEL,
    "viewports": {"type": "array", "minItems": 1, "maxItems": 4, "uniqueItems": True,
                  "items": {"type": "integer", "minimum": 240, "maximum": 2400}},
    "height": {"type": "integer", "minimum": 120, "maximum": 1200}}}
EXAMPLE = {"type": "preview", "path": "artifacts/B-0001/landing-b/index.html", "sha256": "0" * 64,
           "viewports": [390, 1280], "height": 480}


def _check(d: dict) -> str | None:
    if ("url" in d) == ("path" in d):
        return "name either a pinned file (path and sha256) or a url"
    if "path" in d and "sha256" not in d:
        return "a file needs its sha256"
    return None


def _src(ctx, d: dict) -> str | None:
    tid = getattr(ctx.ticket, "id", None)
    if ctx.standalone or not isinstance(tid, str) or not str(d["path"]).lower().endswith((".html", ".htm")):
        return None
    path = artifacts.resolve(ctx.ws, tid, d["path"])
    if path is None or not d.get("sha256") or artifacts.sha256(path) != d["sha256"]:
        return None
    return f"/a/{quote(tid)}/{quote(artifacts.name_of(tid, d['path']))}?v={d['sha256']}"


def render_html(block, ctx) -> str:
    d = block.data
    problem = _check(d)
    if problem:
        return _media.unavailable(esc(problem))
    if "url" in d:
        link = safe_url(d["url"], ctx)
        shown = f'<a href="{esc(link)}" target="_blank" rel="noopener">{esc(d.get("label") or d["url"])} ↗</a>' if link \
            else f'<code>{esc(d["url"])}</code>'
        return f'<p class="w-note w-note-neu" role="note">An external page is not framed here: {shown}</p>'
    src = _src(ctx, d)
    if src is None:
        return _media.unavailable(esc(d.get("label") or d["path"]))
    sizes = d.get("viewports") or [390, 1280]
    buttons = "".join(f'<button type="button" class="w-pv-size" data-w="{int(w)}" aria-pressed="{"true" if i == len(sizes) - 1 else "false"}">{int(w)}</button>'
                      for i, w in enumerate(sizes))
    first = sizes[-1]
    return (f'<div class="w-preview" data-preview><div class="w-pv-bar"><span class="w-pv-url">{esc(d["path"])}</span>'
            f'<span class="w-pv-sizes" role="group" aria-label="Viewport width in pixels">{buttons}</span>'
            f'<a class="w-pv-open" href="{esc(src)}" target="_blank" rel="noopener">Open ↗</a></div>'
            f'<div class="w-pv-stage"><iframe class="w-pv-frame" sandbox="" referrerpolicy="no-referrer" loading="lazy" '
            f'title="{esc(d.get("label") or d["path"])}" src="{esc(src)}" style="width:{int(first)}px;height:{int(d.get("height", 480))}px"></iframe></div></div>')


def render_text(block, ctx) -> str:
    d = block.data
    return f'{d.get("path") or d.get("url")}' + (f' (sha256 {d["sha256"][:12]})' if d.get("sha256") else "")

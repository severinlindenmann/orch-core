"""GET /w/<ID>/<section>/<digest>: one widget as its own document, for the agent-HTML frame (`<iframe sandbox="allow-scripts"
src=…>`). A srcdoc frame would inherit the page's CSP (script-src 'self'), so a frame gets a URL of its own with the
frame policy as a header: `sandbox` gives it an opaque origin, frame-ancestors keeps it inside the dashboard.
GET /w/preview/<name>@<v>: a template with its example data, the same way. GET /widgets: the library."""
from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from orch.core import store
from orch.errors import OrchError
from orch.widgets import Ctx, registry, render_document, render_html, ticket_blocks
from orch.widgets.assemble import NONCE
from orch.widgets.blocks import MAX_BLOCKS, duplicate_ids
from orch.widgets.render import FRAME_CSP_HEADER

router = APIRouter()
HEADERS = {"Content-Security-Policy": FRAME_CSP_HEADER, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
UNUSED_DAYS = 30


def _frame_ctx(request: Request, ticket, theme: str, n: str) -> Ctx | PlainTextResponse:
    """The frame's Ctx, or a 400 when the host's nonce (?n=) is not 8-64 of [A-Za-z0-9_-]."""
    if n and not NONCE.match(n):
        return PlainTextResponse("bad nonce", status_code=400, headers=HEADERS)
    ctx = Ctx.of(request.app.state.ws, ticket, theme=theme if theme in ("light", "dark", "system") else None)
    ctx.nonce = n
    return ctx


@router.get("/w/preview/{ref}")
def template_preview(request: Request, ref: str, theme: str = "", n: str = ""):
    """A template (`name@v`) filled with its example data, as the frame a ticket would load."""
    from orch.widgets import frames, render
    ctx = _frame_ctx(request, None, theme, n)
    if not isinstance(ctx, Ctx):
        return ctx
    spec, version = registry.template_version(ctx.home, ref)
    if version is None:
        return PlainTextResponse("not found", status_code=404, headers=HEADERS)
    name, _, v = ref.partition("@")
    if not ctx.html:
        return HTMLResponse(render.shell(render.note("neu", "Note", "Agent HTML is off in this workspace"), ctx),
                            headers=HEADERS)
    try:
        folder = Path(spec["folder"])
        example = json.loads((folder / "example.json").read_text(encoding="utf-8")).get(v, {})
        body = (folder / f"v{v}.html").read_text(encoding="utf-8")
        doc = frames.build(body, example, libs=spec.get("libs", []), ctx=ctx, title=spec.get("title") or name)
    except (LookupError, OSError, ValueError) as e:
        doc = render.shell(render.note("err", "Error", f"preview not drawn: {e}"), ctx)
    return HTMLResponse(doc, headers=HEADERS)


@router.get("/wp/{addon}/{digest}")
def page_widget_document(request: Request, addon: str, digest: str, page: str = "", theme: str = "", n: str = ""):
    """The block of the page `page` of `addon` whose canonical JSON has sha256 `digest`: the frame of a widget on a
    wiki page. The addon says what the page's text is (its `page_source(page_id)` -> (text, folder), the same cache the
    page was drawn from); nothing else is served: no id or index lookup, and a block whose id the page uses twice is
    never served."""
    from orch.addons.loader import valid_name
    from orch.widgets.pages import blocks_of, page_ctx
    ws = request.app.state.ws
    la = request.app.state.addons.registry.get(addon) if valid_name(addon) else None
    source = getattr(la.obj, "page_source", None) if la is not None else None
    try:
        found = source(page) if callable(source) and re.fullmatch(r"[0-9a-f]{64}", digest) else None
    except Exception:
        found = None
    if not (isinstance(found, tuple) and len(found) == 2 and all(isinstance(x, str) for x in found)):
        return PlainTextResponse("not found", status_code=404, headers=HEADERS)
    text, folder = found
    base = _frame_ctx(request, None, theme, n)  # the nonce check (400) and the theme
    if not isinstance(base, Ctx):
        return base
    ctx = page_ctx(ws, addon, page, folder)
    ctx.nonce, ctx.theme = base.nonce, base.theme
    blocks = blocks_of(text, ctx.ticket)
    block = next((b for b in blocks if b.digest == digest), None)
    if block is None or block.index >= MAX_BLOCKS:
        return PlainTextResponse("not found", status_code=404, headers=HEADERS)
    if (block.data or {}).get("id") in duplicate_ids(blocks):
        return PlainTextResponse("this widget's id is used twice on the page; it is not served", status_code=409,
                                 headers=HEADERS)
    return HTMLResponse(render_document(block, ctx), headers=HEADERS)


@router.get("/wpf/{addon}/{digest}")
def page_file_document(request: Request, addon: str, digest: str, page: str = ""):
    """A file a widget on a wiki page pins, by its sha256: served only when a valid block of that page names a file of
    its `_files/` folder with this digest, and only the bytes that hash to it (read once, hashed as read). Images and
    video only; nosniff, no-store and `sandbox`, so an SVG opened on its own cannot run."""
    import mimetypes

    from fastapi.responses import Response

    from orch.addons.loader import valid_name
    from orch.core.artifacts import read_pinned
    from orch.widgets import artifacts
    from orch.widgets.pages import blocks_of, page_ctx
    from orch.widgets.types._media import VIDEO_TYPES
    la = request.app.state.addons.registry.get(addon) if valid_name(addon) else None
    source = getattr(la.obj, "page_source", None) if la is not None else None
    try:
        found = source(page) if callable(source) and re.fullmatch(r"[0-9a-f]{64}", digest) else None
    except Exception:
        found = None
    if not (isinstance(found, tuple) and len(found) == 2 and all(isinstance(x, str) for x in found)):
        return PlainTextResponse("not found", status_code=404, headers=HEADERS)
    text, folder = found
    ctx = page_ctx(request.app.state.ws, addon, page, folder)
    blocks = [b for b in blocks_of(text, ctx.ticket) if not b.error and b.index < MAX_BLOCKS]
    refs = {r["ref"] for b in blocks for r in artifacts.refs(b.data) if r["sha256"] == digest}
    path = next((p for ref in sorted(refs) if (p := artifacts.resolve(ctx.ws, ctx.ticket.id, ref)) is not None), None)
    kind = mimetypes.guess_type(path.name)[0] if path else None
    if kind not in artifacts.IMAGE_TYPES | VIDEO_TYPES:
        return PlainTextResponse("not found", status_code=404, headers=HEADERS)
    data = read_pinned(path, digest)
    if data is None:
        return PlainTextResponse("not found", status_code=404, headers=HEADERS)
    return Response(data, media_type=kind, headers={"Content-Security-Policy": "sandbox",
                                                     "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.get("/w/{ref}/{section}/{digest}")
def widget_document(request: Request, ref: str, section: str, digest: str, theme: str = "", n: str = ""):
    """The block of `section` whose canonical JSON has sha256 `digest` (Block.digest, render.frame_path); `n` the
    host's nonce for this frame. Anything else is refused: no other section, no id or index lookup, and a block whose
    id another block of the ticket also uses is never served."""
    ws = request.app.state.ws
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return PlainTextResponse("not found", status_code=404, headers=HEADERS)
    try:
        path = store.resolve(ws, ref).path
        ticket = store.read_ticket(path)
    except (OrchError, OSError, UnicodeDecodeError):
        return PlainTextResponse("not found", status_code=404, headers=HEADERS)
    blocks = ticket_blocks(ticket, path.read_text(encoding="utf-8"))
    block = next((b for b in blocks if b.section == section and b.digest == digest), None)
    if block is None or block.index >= MAX_BLOCKS:
        return PlainTextResponse("not found", status_code=404, headers=HEADERS)
    if (block.data or {}).get("id") in duplicate_ids(blocks):
        return PlainTextResponse("this widget's id is used twice in the ticket; it is not served", status_code=409,
                                 headers=HEADERS)
    ctx = _frame_ctx(request, ticket, theme, n)
    if not isinstance(ctx, Ctx):
        return ctx
    return HTMLResponse(render_document(block, ctx), headers=HEADERS)


# -- the library -------------------------------------------------------------------------------------------------

def _type_word(s: dict) -> str:
    if "const" in s:
        return json.dumps(s["const"])
    if "enum" in s:
        return "one of"
    kinds = s.get("type") or [_type_word(x) for x in s.get("oneOf", s.get("anyOf", []))] or "any"
    kind = " or ".join(kinds) if isinstance(kinds, list) else kinds
    if kind == "array" and isinstance(s.get("items"), dict):
        return f"array of {_type_word(s['items'])}"
    return kind


def _notes(s: dict) -> str:
    out = []
    if "enum" in s:
        out.append(", ".join(map(str, s["enum"])))
    for key, word in (("minimum", "≥"), ("maximum", "≤"), ("minLength", "length ≥"), ("maxLength", "length ≤"),
                      ("maxItems", "items ≤")):
        if key in s:
            out.append(f"{word} {s[key]}")
    return "; ".join(out)


def schema_fields(schema: dict, prefix: str = "", depth: int = 0) -> list[dict]:
    """A schema's properties as table rows (name, type, required, notes), nested objects as dotted names."""
    rows, required = [], set(schema.get("required", []))
    for name, s in (schema.get("properties") or {}).items():
        if not isinstance(s, dict):
            continue
        rows.append({"name": prefix + name, "type": _type_word(s), "required": name in required, "notes": _notes(s)})
        inner = s.get("items") if s.get("type") == "array" else s
        if depth < 2 and isinstance(inner, dict) and inner.get("properties"):
            rows += schema_fields(inner, f"{prefix}{name}{'[]' if inner is not s else ''}.", depth + 1)
    return rows


def _versions(spec: dict) -> list[str]:
    return sorted(spec.get("versions", {}), key=lambda v: int(v) if v.isdigit() else 0)


def library(ws, theme: str = "system") -> list[dict]:
    """Core types and templates grouped by moment: [{"moment", "items": [entry, …]}]."""
    from orch.clock import now, parse_stamp
    from orch.widgets.blocks import make_block
    from orch.widgets.validate import usage
    used = usage(ws)
    cutoff = now() - timedelta(days=UNUSED_DAYS)
    html_on = Ctx.of(ws).html

    def unused(row: dict) -> bool:
        try:
            return not row.get("last") or parse_stamp(row["last"]) < cutoff
        except ValueError:
            return True

    items = []
    for name, mod in registry.core_types().items():
        row = used["type"].get(name, {})
        block = make_block("Context", 0, json.dumps(mod.EXAMPLE))
        block.index = len(items)
        desc = re.sub(rf"^`{re.escape(name)}`:\s*", "", (mod.__doc__ or "").strip().split("\n\n")[0])
        items.append({"name": name, "layer": "core", "title": name, "description": desc[:1].upper() + desc[1:],
                      "moment": mod.MOMENT, "uses": row.get("uses", 0), "tickets": row.get("tickets", []),
                      "unused": unused(row), "versions": [], "fields": schema_fields(mod.SCHEMA),
                      "preview": render_html(block, Ctx(ws=ws, theme=theme))})
    for name, spec in sorted(registry.templates(ws.home).items()):
        row = used["widget"].get(name, {})
        versions = _versions(spec)
        latest = versions[-1] if versions else "1"
        nonce = secrets.token_urlsafe(12)
        items.append({"name": name, "layer": "widget", "title": spec.get("title") or name,
                      "description": spec.get("description", ""), "moment": spec.get("moment"),
                      "origin": spec["origin"], "libs": spec.get("libs", []), "uses": row.get("uses", 0),
                      "tickets": row.get("tickets", []), "unused": unused(row),
                      "versions": [{"v": v, "notes": spec["versions"][v].get("notes", ""),
                                    "uses": row.get("versions", {}).get(v, 0)} for v in versions],
                      "fields": schema_fields(spec["versions"].get(latest, {}).get("schema") or {}),
                      "preview": {"url": f"/w/preview/{name}@{latest}?n={nonce}", "nonce": nonce, "v": latest,
                                  "min_height": int(spec.get("min_height") or 160)} if html_on else None})
    order = list(registry.MOMENTS)
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(it["moment"] if it["moment"] in order else "other", []).append(it)
    return [{"moment": m, "items": groups[m]} for m in [*order, "other"] if m in groups]


@router.get("/widgets")
def widgets_page(request: Request):
    from orch.dashboard.views import _theme, page
    from orch.widgets.render import css_names
    ws = request.app.state.ws
    groups = library(ws, _theme(request, ws))
    from markupsafe import Markup

    from orch.widgets.render import inline
    return page(request, "widgets.html", nav="widgets", title="Widgets", groups=groups, widget_css=css_names(),
                inline_md=lambda text: Markup(inline(text)),
                html_on=Ctx.of(ws).html, unused_days=UNUSED_DAYS, broken=registry.template_problems(ws.home), total=sum(len(g["items"]) for g in groups))

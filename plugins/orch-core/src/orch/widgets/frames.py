"""The agent-HTML hook (layers `widget` and `html`): what a frame loads from GET /w/<ID>/<section>/<digest> (docs/widgets.md,
"The frame"). render.py reads these names through this module at call time, so a test or another renderer can still
replace them:
    INSTALLED: bool                              True: frames are drawn (False adds "renderer not installed")
    fallback(block, ctx) -> str                  HTML inside <div class="w-frame" …> until the host script swaps in
                                                 the iframe
    document(block, ctx) -> str                  the frame's whole document (CSP meta, tokens, kit, libs, data, body)
    text(block, ctx) -> str | None               a text alternative known server-side (None: the caption is used);
                                                 the frame replaces it with what it posts through orch.text
"""
from __future__ import annotations

import secrets

INSTALLED = True


def fallback(block, ctx) -> str:
    return ('<noscript><p class="w-note w-note-neu" role="note"><b>Note:</b> this widget needs JavaScript; '
            "the text alternative is below.</p></noscript>")


def text(block, ctx) -> str | None:
    return (block.data or {}).get("caption") or None


def nonce(ctx) -> str:
    """The nonce the host put in the frame's URL (?n=, checked by the route), else a fresh one (a saved document)."""
    from orch.widgets.assemble import NONCE
    given = getattr(ctx, "nonce", "") or ""
    return given if NONCE.match(given) else secrets.token_urlsafe(12)


def inline_images(ws, ticket_id: str, data, _seen: dict | None = None):
    """`data` with every {path, sha256} object that names an image of this ticket replaced by its `data:` URI (a
    frame loads nothing). Other objects stay as they are, and so does a file whose bytes no longer match its digest
    (never shown). Each file is read once however often it is named, and everything inlined, repeats included, stays
    under
    artifacts.MAX_INLINED (ValueError beyond: the frame shows an error, never a document of any size)."""
    from orch.widgets import artifacts
    seen = {"": 0} if _seen is None else _seen  # "": the bytes inlined so far
    if isinstance(data, dict):
        if isinstance(data.get("path"), str) and "sha256" in data:
            ref = data["path"]
            key = f'{ref}\0{data.get("sha256")}'
            if key not in seen:  # one read, hashed as read: bytes that do not match the pin are never inlined
                seen[key] = artifacts.data_uri(ws, ticket_id, ref, data.get("sha256"))
            if seen[key] is not None:
                seen[""] += len(seen[key])
                if seen[""] > artifacts.MAX_INLINED:
                    raise ValueError("too many or too large images (8 MB at most in one widget)")
                return seen[key]
        return {k: inline_images(ws, ticket_id, v, seen) for k, v in data.items()}
    if isinstance(data, list):
        return [inline_images(ws, ticket_id, v, seen) for v in data]
    return data


def build(body: str, data, *, libs, ctx, title: str) -> str:
    """One frame document: the page `body` with `data`, the kit and the named vendored libraries."""
    from orch.widgets.assemble import assemble, read_libs, read_static
    kit, tokens = read_static()
    theme = ctx.theme if ctx.theme in ("light", "dark", "system") else "system"
    return assemble(body, data, nonce=nonce(ctx), kit_js=kit, tokens_css=tokens, libs=read_libs(list(libs)),
                    theme=theme, title=title)


def document(block, ctx) -> str:
    from orch.core.artifacts import read_pinned
    from orch.widgets import artifacts, registry, render
    from orch.widgets.validate import validate
    data = block.data or {}
    title = data.get("title") or "Widget"
    try:
        errors = [p.message for p in validate(block, ctx.ticket, ws=ctx.ws) if p.level == "error"]
        if errors:
            raise ValueError("; ".join(errors))
        tid = getattr(ctx.ticket, "id", "")
        # The digest is checked on the very bytes that run: a file swapped after `validate` is still refused.
        if block.layer == "widget":
            spec, version = registry.template_version(ctx.home, data["widget"])
            if version is None:
                raise LookupError(f"no template {data['widget']}")
            raw = registry.template_body(spec, data["widget"])
            if registry.template_digest(spec, raw) != data.get("sha256"):
                raise ValueError(f"template {data['widget']} changed since this widget was written (drift)")
            libs = spec.get("libs", [])
        else:
            path = artifacts.resolve(ctx.ws, tid, data["html"])
            if path is None:
                raise LookupError(f"{data['html']} is missing")
            pin = data.get("sha256")  # only the page the block's text pins runs: the full digest, of the bytes read
            raw = read_pinned(path, pin) if isinstance(pin, str) and len(pin) == 64 else None
            if raw is None:
                raise ValueError(f"{data['html']} changed since this widget was written")
            libs = data.get("libs", [])
        body = raw.decode("utf-8")
        return build(body, inline_images(ctx.ws, tid, data.get("data", {})), libs=libs, ctx=ctx, title=title)
    except (LookupError, OSError, UnicodeDecodeError, ValueError) as e:  # KeyError: an unknown library
        return render.shell(render.note("err", "Error", f"widget not drawn: {e}"), ctx, title=title)

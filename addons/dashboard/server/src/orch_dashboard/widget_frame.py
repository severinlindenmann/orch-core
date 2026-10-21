"""Widgets (#604 task 7): the shipped templates, `widgets.catalog` and the locked `/widget-frame/` route.

A template is a fixed page of the add-on (``widget_templates/<name>@<version>.html``), never agent text. The page runs
only inside an iframe whose own response headers make it an opaque-origin, network-less box: the sandbox lives in the
response CSP (``security.py`` sets it, in one place, replacing the dashboard's CSP for this prefix), so even a document
opened as a top-level page cannot reach the dashboard token. The route is exact: the URL carries the sha256 of the
shipped bytes, any other path, query string, digest or fetch destination is a 404, and nothing here ever redirects.

The bytes of a template are pinned by ``digest`` (sha256 of the page, a NUL byte and the sorted libs as compact JSON,
the same formula as ``widgetTemplates.ts`` ``templateDigest``). The page is not inlined in the frame document: it
waits, inert, in a ``<template>`` until the parent's data message arrives, so the template bytes (and their pin) stay
exactly as shipped while ``orch.data`` is still there when its script runs.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response

from . import reads
from .errors import HostError, response
from .gateway import Gateway

PREFIX = "/widget-frame/"
ADDON = "widgets"
IMMUTABLE = "public, max-age=31536000, immutable"
FRAME_PATH = re.compile(r"/widget-frame/([a-z][a-z0-9-]*)@([1-9][0-9]{0,5})/([0-9a-f]{64})")
_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)
_TEMPLATES_DIR = Path(__file__).parent / "widget_templates"

#: name -> (title, description, starting height in px), the words of the dashboard's gallery
META = {
    "before-after": ("Before / after slider", "Two versions of the same text, revealed with a slider.", 200),
    "line-chart": ("Line chart", "A small line over x with a value label per point.", 190),
    "option-prototype": ("Option prototype", "Options with cost and risk that the reader can click through.", 230),
    "image-compare": (
        "Image compare",
        "Two screenshots, a slider or side by side. Inline data: images only.",
        240,
    ),
    "flow-diagram": ("Flow diagram", "Boxes and arrows from a list of steps and links, left to right.", 160),
    "table-explorer": ("Table explorer", "A table the reader can sort and filter.", 240),
}

# Core's own wrapper (not part of any pin): the frame cannot read the dashboard's CSS variables.
CORE_STYLE = (
    ":root{color-scheme:dark}html,body{margin:0}"
    "body{padding:8px;font:12px/1.45 ui-sans-serif,system-ui,sans-serif;color:CanvasText;background:Canvas}"
    "svg{max-height:782px}"
)
# The frame's whole runtime. It takes data once, from its parent window only, and puts the inert page into the
# document (a script made from a <template> still has to match a CSP hash), then reports its height (0..800 px).
RUNTIME = (
    "(function(){var started=false,last=0;"
    "function send(m){parent.postMessage(m,'*')}"
    "function size(){var b=document.body;if(!b)return;var h=Math.ceil(b.getBoundingClientRect().height);"
    "if(h>800)h=800;if(h>0&&h!==last){last=h;send({orch:'size',height:h})}}"
    "addEventListener('message',function(e){"
    "if(started||e.source!==parent)return;var m=e.data;"
    "if(!m||typeof m!=='object'||m.orch!=='widget-data'||m.v!==1||!m.data||typeof m.data!=='object')return;"
    "started=true;window.orch=Object.freeze({data:m.data});"
    "var t=document.getElementById('orch-page');document.body.appendChild(document.importNode(t.content,true));"
    "if(window.ResizeObserver)new ResizeObserver(size).observe(document.body);size()});"
    "send({orch:'ready'})})()"
)


def script_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


@dataclass(frozen=True)
class Template:
    name: str
    version: int
    title: str
    description: str
    min_height: int
    page: str
    digest: str
    document: bytes
    hashes: tuple[str, ...]

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def path(self) -> str:
        return f"{PREFIX}{self.ref}/{self.digest}"


def digest_of(page: bytes, libs: tuple[str, ...] = ()) -> str:
    """The pin: sha256 of the page, a NUL byte and the sorted libs as compact JSON."""
    import json

    return hashlib.sha256(page + b"\0" + json.dumps(sorted(libs), separators=(",", ":")).encode()).hexdigest()


def build(file: Path) -> Template:
    name, _, version = file.stem.partition("@")
    raw = file.read_bytes()
    page = raw.decode("utf-8")
    scripts = _SCRIPT.findall(page)
    if page.count("<script") != len(scripts) or "</template" in page.lower():
        raise ValueError(f"{file.name}: only plain <script> blocks and no </template> are allowed")
    title, description, height = META[name]
    document = (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f'<style>{CORE_STYLE}</style></head><body><template id="orch-page">{page}</template>'
        f"<script>{RUNTIME}</script></body></html>"
    ).encode()
    return Template(
        name, int(version), title, description, height, page, digest_of(raw), document,
        (script_hash(RUNTIME), *(script_hash(s) for s in scripts)),
    )  # fmt: skip


def load() -> dict[tuple[str, int], Template]:
    templates = [build(f) for f in sorted(_TEMPLATES_DIR.glob("*@*.html"))]
    return {(t.name, t.version): t for t in templates}


TEMPLATES = load()


def match(path: str) -> Template | None:
    """The template this exact path names (name, version and digest all equal), or ``None``."""
    m = FRAME_PATH.fullmatch(path)
    template = TEMPLATES.get((m[1], int(m[2]))) if m else None
    return template if template and m[3] == template.digest else None


def frame_csp(path: str, port: int) -> str:
    """The CSP of everything under ``/widget-frame/``, the only one that prefix carries. A known template lists the
    hashes of its scripts; anything else (a 404) has no script source at all."""
    template = match(path)
    scripts = " ".join(template.hashes) if template else "'none'"
    return (
        f"sandbox allow-scripts; default-src 'none'; script-src {scripts}; style-src 'unsafe-inline'; img-src data:; "
        "font-src 'none'; connect-src 'none'; frame-src 'none'; child-src 'none'; worker-src 'none'; "
        "object-src 'none'; manifest-src 'none'; base-uri 'none'; form-action 'none'; "
        f"frame-ancestors http://127.0.0.1:{port}"
    )


def addon_on(gateway: Gateway) -> bool:
    """Is Widgets on in any workspace this person may use? The frame route has no workspace (a frame request carries no
    token, and its body is the same public shipped page everywhere); the parent only builds the URL for a workspace
    where it is on. Unreadable counts as off."""
    try:
        wss = list(gateway._pins())
    except HostError:
        return False
    for ws in wss:
        try:
            if Gateway.addon_on(gateway.enabled(ws), ADDON):
                return True
        except HostError:
            continue
    return False


def not_found() -> Response:
    res = response("not_found", 404)
    res.headers["Cache-Control"] = "no-store"
    return res


def register(app: FastAPI, gateway: Gateway):
    @app.get("/api/w/{ws}/widgets/catalog")
    def catalog(ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        return {
            "available": True,
            "enabled": Gateway.addon_on(link, ADDON),
            "templates": [
                {
                    "name": t.name,
                    "version": t.version,
                    "ref": t.ref,
                    "sha256": t.digest,
                    "title": t.title,
                    "description": t.description,
                    "min_height": t.min_height,
                    "frame": t.path,
                }
                for t in sorted(TEMPLATES.values(), key=lambda t: (t.name, t.version))
            ],
        }

    # A catch-all under the prefix, so no near miss (a trailing slash, an extra segment) ever reaches the router's
    # slash redirect or the app's page fallback. Exact match or 404.
    @app.api_route(PREFIX + "{rest:path}", methods=["GET", "HEAD"])
    def frame(rest: str, request: Request):
        # The same decoded path the security layer matched (its CSP depends on it); the raw path may differ from it
        # only by the `%40` browsers write for `@`, never by another escape (no alias of a pin).
        path = request.scope["path"]
        raw = request.scope.get("raw_path", path.encode()).decode("ascii", "replace")
        template = match(path)
        if (
            template is None
            or raw.replace("%40", "@") != path
            or request.scope.get("query_string")
            or request.headers.getlist("sec-fetch-dest") != ["iframe"]
            or request.headers.getlist("sec-fetch-site") not in ([], ["same-origin"])
            or not addon_on(gateway)
        ):
            return not_found()
        return Response(
            template.document,
            media_type="text/html",
            headers={"Cache-Control": IMMUTABLE, "Vary": "Sec-Fetch-Dest, Sec-Fetch-Site"},
        )

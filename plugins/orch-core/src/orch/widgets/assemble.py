"""Assemble one agent-HTML frame document (docs/widgets.md, "The frame").

`assemble` is pure: strings and data in, one HTML document out. `read_libs` and `read_static` do the file reading
(vendored libraries are checked against static/vendor/MANIFEST.json) so callers can cache or test either side.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

from orch.widgets.render import FRAME_CSP, frame_tokens

STATIC = Path(__file__).resolve().parents[1] / "dashboard" / "static"
VENDOR = STATIC / "vendor"
BUILTIN = Path(__file__).with_name("builtin")

BASE_CSS = """html, body { margin: 0; padding: 0; }
body { background: var(--surface); color: var(--text); font: var(--type-body); overflow-x: hidden; }
*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
button { font: var(--type-control); color: inherit; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }"""

_END_SCRIPT = re.compile(r"</(script)", re.IGNORECASE)
_END_STYLE = re.compile(r"</(style)", re.IGNORECASE)
NONCE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def json_for_script(data: object) -> str:
    """JSON that cannot end its <script> element or open a comment inside it."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _style(text: str) -> str:
    return "<style>" + _END_STYLE.sub(r"<\\/\1", text) + "</style>"


def _script(text: str) -> str:
    return "<script>" + _END_SCRIPT.sub(r"<\\/\1", text) + "</script>"


def assemble(body: str, data: object, *, nonce: str, kit_js: str, tokens_css: str,
             libs: list[tuple[str, str]] = (), theme: str = "system", title: str = "Widget") -> str:
    """One standalone frame document.

    libs: (kind, text) chunks in inline order, kind "css" or "js" (see read_libs).
    theme: light | dark | system, written as <html data-theme>; the kit follows the host after that.
    """
    if not NONCE.match(nonce):
        raise ValueError("nonce must be 8-64 characters of [A-Za-z0-9_-]")
    if theme not in ("light", "dark", "system"):
        raise ValueError("theme must be light, dark or system")
    parts = [
        "<!doctype html>",
        f'<html lang="en" data-theme="{theme}">',
        "<head>",
        '<meta charset="utf-8">',
        f'<meta http-equiv="Content-Security-Policy" content="{FRAME_CSP}">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta name="orch-frame" content="{nonce}">',
        f"<title>{html.escape(title)}</title>",
        _style(tokens_css + "\n" + BASE_CSS),
    ]
    parts += [_style(text) for kind, text in libs if kind == "css"]
    parts.append(_script(kit_js))
    parts += [_script(text) for kind, text in libs if kind == "js"]
    parts += [
        f'<script type="application/json" id="orch-data">{json_for_script(data)}</script>',
        "</head>",
        "<body>",
        body,
        "</body>",
        "</html>",
    ]
    return "\n".join(parts) + "\n"


def read_manifest(vendor: Path = VENDOR) -> dict:
    return json.loads((vendor / "MANIFEST.json").read_text(encoding="utf-8"))


def read_libs(names: list[str], vendor: Path = VENDOR) -> list[tuple[str, str]]:
    """The (kind, text) chunks for the named libraries, in each library's inline_order, digest-checked."""
    manifest = read_manifest(vendor)["libs"]
    chunks = []
    for name in names:
        if name not in manifest:
            raise KeyError(f"unknown widget library {name!r}")
        lib = manifest[name]
        for path in lib["inline_order"]:
            raw = (vendor / path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != lib["files"][path]["sha256"]:
                raise ValueError(f"vendored file {path} does not match MANIFEST.json")
            chunks.append(("css" if path.endswith(".css") else "js", raw.decode("utf-8")))
    return chunks


def read_static() -> tuple[str, str]:
    """(kit_js, tokens_css) as shipped with the dashboard."""
    return ((STATIC / "widgets" / "orch-kit.js").read_text(encoding="utf-8"),
            frame_tokens())

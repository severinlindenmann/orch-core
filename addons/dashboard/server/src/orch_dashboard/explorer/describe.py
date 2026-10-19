"""What a file looks like in the preview (issue #491): text, markdown, an image, or metadata only.

Pure functions over bytes: they never touch the file system (the guard did that) and never decide what may be read.
Rendering stays safe because nothing here produces markup: text is sent as text (the page escapes it), markdown as
source (the page's sanitising renderer turns it into HTML), an image as a `data:` URL of an allow-listed type whose
first bytes match the type (never SVG: it can carry script, so it is shown as code).
"""

from __future__ import annotations

import base64
import os
import unicodedata

from ..text import hidden

#: text larger than this, and any binary, show metadata only (owner default, issue #491)
TEXT_MAX = 2 * 1024 * 1024
IMAGE_MAX = 4 * 1024 * 1024
IMAGES = {
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".gif": ("image/gif", (b"GIF87a", b"GIF89a")),
    ".webp": ("image/webp", (b"RIFF",)),
    ".bmp": ("image/bmp", (b"BM",)),
    ".ico": ("image/x-icon", (b"\x00\x00\x01\x00",)),
}
MARKDOWN = (".md", ".markdown")
#: file name extension -> the name the page's highlighter uses (display only)
LANGUAGES = {
    ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx", ".json": "json",
    ".css": "css", ".html": "html", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".sh": "shell", ".rs": "rust",
    ".go": "go", ".java": "java", ".c": "c", ".h": "c", ".cpp": "cpp", ".swift": "swift", ".rb": "ruby",
    ".sql": "sql", ".svg": "xml", ".xml": "xml", ".md": "markdown", ".txt": "text", ".kt": "kotlin",
}  # fmt: skip


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def describe(name: str, data: bytes | None, size: int, cut: bool = False) -> dict:
    """The preview of a file called ``name``: ``data`` is its first bytes (None: not read), ``size`` its size,
    ``cut`` that ``data`` stops before the end. Always has ``kind``: ``text``, ``markdown``, ``image``, ``binary``,
    ``too_large`` or ``empty``."""
    ext = _ext(name)
    base = {"size": size, "ext": ext.lstrip(".")}
    if size == 0:
        return {**base, "kind": "empty"}
    if ext in IMAGES:
        mime, magics = IMAGES[ext]
        if size > IMAGE_MAX or data is None or cut:
            return {**base, "kind": "too_large"}
        ok = data.startswith(magics) and (ext != ".webp" or data[8:12] == b"WEBP")
        if ok:
            return {**base, "kind": "image", "mime": mime, "data": base64.b64encode(data).decode("ascii")}
        return {**base, "kind": "binary"}
    if size > TEXT_MAX or data is None or cut:
        return {**base, "kind": "too_large"}
    if b"\0" in data[:8192]:
        return {**base, "kind": "binary"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {**base, "kind": "binary"}
    return {
        **base,
        "kind": "markdown" if ext in MARKDOWN else "text",
        "text": text,
        "language": LANGUAGES.get(ext),
        "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
        # invisible or direction-changing characters are in the file: the page says so (they can make code read
        # differently from what it does)
        "hidden_chars": hidden(text.replace("\n", "").replace("\t", "").replace("\r", "")),
    }


def clean_line(text: str, limit: int = 300) -> str:
    """One line of a search hit for display: cut, with control and invisible characters made visible."""
    out = []
    for c in text[:limit]:
        if c == "\t":
            out.append(" ")
        elif unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp", "Co"):
            out.append(f"\\u{ord(c):04x}")
        else:
            out.append(c)
    return "".join(out)

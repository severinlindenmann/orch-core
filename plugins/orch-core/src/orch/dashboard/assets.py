"""Static assets with a content version in their URL (/static/app.css?v=<hash>), so a browser keeps them for a year
and still never shows an old stylesheet or script after an update: a new file is a new URL."""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).with_name("static")
IMMUTABLE = "public, max-age=31536000, immutable"
BRIEF = "public, max-age=300"  # an unversioned or outdated URL (fonts named in app.css, the lock page)

_VERSIONS: dict[str, tuple[tuple, str]] = {}  # path -> ((mtime_ns, size), content hash)
_LOCK = threading.Lock()


def version(path: str) -> str | None:
    """The first 12 hex digits of the file's SHA-256, rehashed only when its mtime or size changed; None when the
    file does not exist."""
    file = STATIC_DIR / path
    try:
        st = os.stat(file)
    except OSError:
        return None
    key, stamp = os.fspath(file), (st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _VERSIONS.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        digest = hashlib.sha256(file.read_bytes()).hexdigest()[:12]
    except OSError:
        return None
    with _LOCK:
        _VERSIONS[key] = (stamp, digest)
    return digest


def static_url(path: str) -> str:
    v = version(path)
    return f"/static/{path}?v={v}" if v else f"/static/{path}"


class AssetFiles(StaticFiles):
    """StaticFiles (ETag and Last-Modified as before) plus Cache-Control: immutable for the current versioned URL,
    a few minutes for any other."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            query = scope.get("query_string", b"").decode("latin-1")
            wanted = dict(p.partition("=")[::2] for p in query.split("&") if p).get("v")
            current = version(path) if wanted else None
            response.headers["Cache-Control"] = IMMUTABLE if wanted and wanted == current else BRIEF
        return response

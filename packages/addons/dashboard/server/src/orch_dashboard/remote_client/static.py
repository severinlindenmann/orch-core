"""The UI files of the remote client: read ONCE from the installed dashboard build (never from the hub, never from a
workspace) into an exact path -> bytes map with a hash manifest (R5 design section 1, "UI only from the local install").

Nothing here makes a network request or imports an HTTP client; the tests scan for that."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path

__all__ = ["StaticSite", "installed_static_dir"]

TOTAL_MAX = 64 << 20
HOME_PAGE = "/remote-client.html"
SESSION_PAGE = "/index.html"
_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".woff2": "font/woff2",
    ".map": "application/json",
}


@dataclass(frozen=True)
class File:
    body: bytes
    content_type: str
    sha256: str


class StaticSite:
    def __init__(self, files: dict[str, File]) -> None:
        self.files = files

    @classmethod
    def load(cls, root: Path) -> StaticSite:
        root = Path(root).resolve()
        files: dict[str, File] = {}
        total = 0
        for p in sorted(root.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            rel = "/" + p.relative_to(root).as_posix()
            body = p.read_bytes()
            total += len(body)
            if total > TOTAL_MAX:
                raise ValueError("the UI build is too large")
            ctype = _TYPES.get(p.suffix.lower()) or mimetypes.guess_type(rel)[0] or "application/octet-stream"
            files[rel] = File(body, ctype, hashlib.sha256(body).hexdigest())
        return cls(files)

    def manifest(self) -> dict[str, str]:
        return {k: v.sha256 for k, v in sorted(self.files.items())}

    def page(self, kind: str) -> File | None:
        return self.files.get(HOME_PAGE if kind == "home" else SESSION_PAGE)

    def lookup(self, kind: str, path: str) -> File | None:
        """The file at ``path`` for an origin of ``kind`` (``home`` serves the client's own page, ``session`` the
        dashboard's; each origin refuses the other's page by name)."""
        if path in ("/", ""):
            return self.page(kind)
        other = SESSION_PAGE if kind == "home" else HOME_PAGE
        if path == other or path == (HOME_PAGE if kind == "home" else SESSION_PAGE):
            return None  # the page is served at "/" only
        return self.files.get(path)


def installed_static_dir() -> Path:
    from importlib.resources import files

    return Path(str(files("orch_dashboard") / "static"))

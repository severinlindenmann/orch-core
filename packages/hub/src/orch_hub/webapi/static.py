"""The browser console's static files (V3-A8, orch-core#860).

The console origin is DEDICATED (design-a8 I7): it serves exactly the files named in :data:`FILES` and the ``/api/v1``
JSON, nothing else, on any path. The set is fixed at import: names are looked up in a dict, never joined to a path, and
the bytes are read once from the package's ``web`` directory. Every response carries the same hard headers: a CSP with
no inline script or style, no worker, no third party, Trusted Types required (enforced in Chromium; the page also builds
its DOM with ``textContent`` only, which a lint test checks), ``nosniff``, ``no-referrer``, COOP/CORP same-origin, and,
when the cookie is Secure, HSTS. HTML is never cached; assets are revalidated.
"""

from __future__ import annotations

from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_JS = "text/javascript; charset=utf-8"
#: url path -> (file name, content type). The ONLY things the console origin serves besides ``/api/v1``.
FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/c": ("connect.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", _JS),
    "/channel.js": ("channel.js", _JS),
    "/cj.js": ("cj.js", _JS),
    "/route.js": ("route.js", _JS),
    "/home.js": ("home.js", _JS),
    "/inbox.js": ("inbox.js", _JS),
    "/account.js": ("account.js", _JS),
    "/admin.js": ("admin.js", _JS),
    "/render.js": ("render.js", _JS),
    "/switch.js": ("switch.js", _JS),
    "/ui.js": ("ui.js", _JS),
    "/feel.js": ("feel.js", _JS),
    "/services.js": ("services.js", _JS),
    "/workspaces.js": ("workspaces.js", _JS),
}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; worker-src 'none'; "
    "require-trusted-types-for 'script'; trusted-types 'none'"
)


class StaticSite:
    def __init__(self, *, hsts: bool, root: Path = WEB_DIR) -> None:
        self._hsts = hsts
        self._files = {p: ((root / name).read_bytes(), ctype) for p, (name, ctype) in FILES.items()}

    def get(self, path: str) -> tuple[int, list[tuple[str, str]], bytes] | None:
        """The answer for an exact allow-listed path, or ``None`` (the caller answers the JSON ``unknown_op``)."""
        hit = self._files.get(path)
        if hit is None:
            return None
        body, ctype = hit
        h = [
            ("Content-Type", ctype),
            ("Content-Security-Policy", CSP),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Cross-Origin-Opener-Policy", "same-origin"),
            ("Cross-Origin-Resource-Policy", "same-origin"),
            ("Cache-Control", "no-store" if ctype.startswith("text/html") else "no-cache"),
        ]
        if self._hsts:
            h.append(("Strict-Transport-Security", "max-age=31536000"))
        return 200, h, body

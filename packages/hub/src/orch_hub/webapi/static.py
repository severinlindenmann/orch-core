"""The browser console's static files (V3-A8, orch-core#860).

The console origin is DEDICATED (design-a8 I7): it serves exactly the files named in :data:`FILES` and the ``/api/v1``
JSON, nothing else, on any path. The set is fixed at import: names are looked up in a dict, never joined to a path, and
the bytes are read once from the package's ``web`` directory. Every response carries the same hard headers: a CSP with
no inline script or style, no worker, no third party, Trusted Types required (enforced in Chromium; the page also builds
its DOM with ``textContent`` only, which a lint test checks), ``nosniff``, ``no-referrer``, COOP/CORP same-origin, and,
when the cookie is Secure, HSTS. Nothing is cached without asking the hub: every file carries an ETag made from its bytes
(so an update is seen at once, never a stale shell or script) and answers ``If-None-Match`` with a 304; the text files are
also served gzipped to a client that accepts it. The page links its whole module graph with ``modulepreload`` so the browser
fetches the chain of imports in parallel; the list is built here from the real imports and cannot drift.
"""

from __future__ import annotations

import gzip
import hashlib
import posixpath
import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_JS = "text/javascript; charset=utf-8"
#: url path -> (file name, content type). The ONLY things the console origin serves besides ``/api/v1``.
FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/c": ("connect.html", "text/html; charset=utf-8"),
    "/oidc/callback": (
        "index.html",
        "text/html; charset=utf-8",
    ),  # the company login returns here; the page finishes it
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", _JS),
    "/channel.js": ("channel.js", _JS),
    "/cj.js": ("cj.js", _JS),
    "/route.js": ("route.js", _JS),
    "/connect.js": ("connect.js", _JS),
    "/home.js": ("home.js", _JS),
    "/inbox.js": ("inbox.js", _JS),
    "/account.js": ("account.js", _JS),
    "/activity.js": ("activity.js", _JS),
    "/admin.js": ("admin.js", _JS),
    "/changes.js": ("changes.js", _JS),
    "/admin_reach.js": ("admin_reach.js", _JS),
    "/render.js": ("render.js", _JS),
    "/switch.js": ("switch.js", _JS),
    "/ui.js": ("ui.js", _JS),
    "/feel.js": ("feel.js", _JS),
    "/serverguide.js": ("serverguide.js", _JS),
    "/services.js": ("services.js", _JS),
    "/handoffs.js": ("handoffs.js", _JS),
    "/workspaces.js": ("workspaces.js", _JS),
    "/phones.js": ("phones.js", _JS),
    "/service_actions.js": ("service_actions.js", _JS),
    "/invite.js": ("invite.js", _JS),
    "/oidc.js": ("oidc.js", _JS),
    "/qr.js": ("qr.js", _JS),
    "/totp.js": ("totp.js", _JS),
    "/seal.js": ("seal.js", _JS),
}

#: A page module's file name: the allowlist of the ``pages/`` folder. The folder is read by glob when the site is built; a file
#: that does not match makes the build fail, so nothing odd can be served by being dropped there.
PAGE_FILE = re.compile(r"[a-z][a-z0-9-]{0,31}\.js")


def page_files(root: Path = WEB_DIR) -> list[str]:
    """The page modules of ``<root>/pages``, sorted. Anything else in the folder is a build error."""
    folder = root / "pages"
    # dotfiles (.DS_Store, editor swap files) and __pycache__ are not pages and are never served; anything else that is not
    # a page module name still fails the build
    names = (
        sorted(p.name for p in folder.iterdir() if not p.name.startswith(".") and p.name != "__pycache__")
        if folder.is_dir()
        else []
    )
    bad = [n for n in names if not PAGE_FILE.fullmatch(n) or not (folder / n).is_file()]
    if bad:
        raise ValueError(f"not a page module name: {bad}")
    return names


def pages_module(names: list[str]) -> bytes:
    """``/pages.js``: static imports of every page module and the one ``PAGES`` array the console builds its router, tabs
    and dispatch from. Generated, never hand-kept (adding a page touches only that page's file)."""
    ids = {n: "p_" + n[:-3].replace("-", "_") for n in names}
    lines = [f'import {{ page as {ids[n]} }} from "./pages/{n}";' for n in names]
    lines.append("export const PAGES = [" + ", ".join(ids[n] for n in names) + "];")
    return (
        "// Generated from the pages/ folder when the site is built. Do not edit.\n" + "\n".join(lines) + "\n"
    ).encode()


CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; worker-src 'none'; "
    "require-trusted-types-for 'script'; trusted-types 'none'"
)


_IMPORT = re.compile(r'(?:\bfrom\s*|\bimport\s*)"(\.{1,2}/[^"]+\.js)"')
_COMPRESSIBLE = ("text/", "image/svg")
_ENTRY = "/app.js"


def module_graph(files: dict[str, bytes], entry: str = _ENTRY) -> list[str]:
    """Every module ``entry`` imports, statically and transitively, as url paths (entry first, import order, once each).
    Only imports that resolve to a served file count; a dynamic ``import(`` is not followed."""
    seen: list[str] = []
    todo = [entry]
    while todo:
        cur = todo.pop(0)
        if cur in seen or cur not in files:
            continue
        seen.append(cur)
        for spec in _IMPORT.findall(files[cur].decode("utf-8", "replace")):
            todo.append(posixpath.normpath(posixpath.join(posixpath.dirname(cur), spec)))
    return seen


def _accepts_gzip(value: str) -> bool:
    for part in value.split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() not in ("gzip", "x-gzip"):
            continue
        q = re.search(r"q\s*=\s*([0-9.]+)", params)
        try:
            return q is None or float(q.group(1)) > 0
        except ValueError:
            return False
    return False


def _etag_matches(header: str, etag: str) -> bool:
    if header.strip() == "*":
        return True
    return any(t.strip().removeprefix("W/") == etag for t in header.split(","))


class StaticSite:
    def __init__(self, *, hsts: bool, root: Path = WEB_DIR) -> None:
        self._hsts = hsts
        files = {p: ((root / name).read_bytes(), ctype) for p, (name, ctype) in FILES.items()}
        names = page_files(root)
        files.update({"/pages/" + n: ((root / "pages" / n).read_bytes(), _JS) for n in names})
        files["/pages.js"] = (pages_module(names), _JS)
        self.pages = names
        # the modules the shell needs, from the real import graph (app.js -> pages.js -> every page ...)
        self.preload = module_graph({p: b for p, (b, _) in files.items()})
        links = "".join(f'<link rel="modulepreload" href="{p}">\n' for p in self.preload).encode()
        for p in ("/", "/oidc/callback"):
            body, ctype = files[p]
            files[p] = (body.replace(b"</head>", links + b"</head>", 1), ctype)
        self._files: dict[str, tuple[bytes, str]] = {p: (b, c) for p, (b, c) in files.items()}
        # per path: (identity etag, gzip body or None, gzip etag)
        self._meta: dict[str, tuple[str, bytes | None, str]] = {}
        for p, (b, c) in self._files.items():
            tag = hashlib.sha256(b).hexdigest()[:32]
            z = gzip.compress(b, 9, mtime=0) if c.startswith(_COMPRESSIBLE) and len(b) > 256 else None
            self._meta[p] = (f'"{tag}"', z if z is not None and len(z) < len(b) else None, f'"{tag}-gz"')

    def get(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, list[tuple[str, str]], bytes] | None:
        """The answer for an exact allow-listed path, or ``None`` (the caller answers the JSON ``unknown_op``).
        ``headers`` (lower-case names) are the request's: ``if-none-match`` gives a 304, ``accept-encoding`` a gzip body."""
        hit = self._files.get(path)
        if hit is None:
            return None
        body, ctype = hit
        req = {k.lower(): v for k, v in (headers or {}).items()}
        etag, z, ztag = self._meta[path]
        h = [
            ("Content-Type", ctype),
            ("Content-Security-Policy", CSP),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Cross-Origin-Opener-Policy", "same-origin"),
            ("Cross-Origin-Resource-Policy", "same-origin"),
            # always ask the hub (cheap with the ETag): an update is never hidden behind a cached shell or script
            ("Cache-Control", "no-cache"),
        ]
        if z is not None:
            h.append(("Vary", "Accept-Encoding"))
            if _accepts_gzip(req.get("accept-encoding", "")):
                body, etag = z, ztag
                h.append(("Content-Encoding", "gzip"))
        h.append(("ETag", etag))
        if self._hsts:
            h.append(("Strict-Transport-Security", "max-age=31536000"))
        if "if-none-match" in req and _etag_matches(req["if-none-match"], etag):
            return 304, [x for x in h if x[0] != "Content-Encoding"], b""
        return 200, h, body

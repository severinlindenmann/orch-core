"""The browser console's static files (V3-A8, orch-core#860).

The console origin is DEDICATED (design-a8 I7): it serves exactly the files named in :data:`FILES` and the ``/api/v1``
JSON, nothing else, on any path. The set is fixed at import: names are looked up in a dict, never joined to a path, and
the bytes are read once from the package's ``web`` directory. Every response carries the same hard headers: a CSP with
no inline script or style, no worker, no third party, Trusted Types required (enforced in Chromium; the page also builds
its DOM with ``textContent`` only, which a lint test checks), ``nosniff``, ``no-referrer``, COOP/CORP same-origin, and,
when the cookie is Secure, HSTS. HTML is never cached; assets are revalidated.
"""

from __future__ import annotations

import re
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
    "/activity.js": ("activity.js", _JS),
    "/admin.js": ("admin.js", _JS),
    "/changes.js": ("changes.js", _JS),
    "/admin_reach.js": ("admin_reach.js", _JS),
    "/render.js": ("render.js", _JS),
    "/switch.js": ("switch.js", _JS),
    "/ui.js": ("ui.js", _JS),
    "/feel.js": ("feel.js", _JS),
    "/services.js": ("services.js", _JS),
    "/handoffs.js": ("handoffs.js", _JS),
    "/workspaces.js": ("workspaces.js", _JS),
    "/phones.js": ("phones.js", _JS),
    "/invite.js": ("invite.js", _JS),
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


class StaticSite:
    def __init__(self, *, hsts: bool, root: Path = WEB_DIR) -> None:
        self._hsts = hsts
        self._files = {p: ((root / name).read_bytes(), ctype) for p, (name, ctype) in FILES.items()}
        names = page_files(root)
        self._files.update({"/pages/" + n: ((root / "pages" / n).read_bytes(), _JS) for n in names})
        self._files["/pages.js"] = (pages_module(names), _JS)
        self.pages = names

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

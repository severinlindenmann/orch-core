from __future__ import annotations

import secrets
from urllib.parse import urlencode, urlsplit

from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

COOKIE = "orch_token"
UNAUTHORIZED = """<!doctype html><html lang="en" data-theme="system"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Locked · orch</title>
<link rel="stylesheet" href="/static/tokens.css"><link rel="stylesheet" href="/static/app.css"><main class="narrow"><h1>Locked</h1>
<p>Open the link that <code>orch serve</code> printed; it contains <code>?token=…</code>.</p></main></html>"""


def _matches(given: str, token: str) -> bool:
    return secrets.compare_digest(given.encode("utf-8"), token.encode("utf-8"))


def _same_origin(request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True  # same-site form posts from old browsers; the SameSite=Strict cookie still applies
    return urlsplit(origin).netloc == request.headers.get("host", "")


def strict_same_origin(request) -> bool:
    """For addon POSTs: an Origin header is required and must be this host (stricter than `_same_origin`)."""
    origin = request.headers.get("origin")
    return bool(origin) and urlsplit(origin).netloc == request.headers.get("host", "")


async def auth_middleware(request, call_next):
    # Unauthenticated, but safe: StaticFiles resolves paths within STATIC_DIR itself and
    # refuses any "../" traversal outside it, so this prefix check does not need to.
    if request.url.path.startswith("/static/"):
        return await call_next(request)
    token = request.app.state.token
    given = request.query_params.get("token")
    if given is not None and _matches(given, token):
        rest = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "token"])
        response = RedirectResponse(request.url.path + (f"?{rest}" if rest else ""), status_code=303)
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
        return response
    if not _matches(request.cookies.get(COOKIE, ""), token):
        return HTMLResponse(UNAUTHORIZED, status_code=401)
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        return PlainTextResponse("cross-origin request refused", status_code=403)
    return await call_next(request)

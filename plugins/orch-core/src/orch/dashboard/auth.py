from __future__ import annotations

import secrets
from urllib.parse import urlencode, urlsplit

from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from orch.dashboard.switcher import STATUS_PATH

COOKIE = "orch_token"
COOKIE_DAYS = 30
UNAUTHORIZED = """<!doctype html><html lang="en" data-theme="system"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Locked · orch</title>
<link rel="stylesheet" href="/static/tokens.css"><link rel="stylesheet" href="/static/app.css"><main class="narrow"><h1>Locked</h1>
<p>This browser is not signed in to this dashboard yet, or its sign-in ran out.</p>
<p>Run <code>orch serve --link</code> in your terminal, in this workspace, and open the link it prints; it contains
<code>?token=…</code>. You stay signed in for 30 days, across restarts of <code>orch serve</code>.</p></main></html>"""


def cookie_name(port: int | None) -> str:
    """The login cookie of the dashboard on `port`. Browsers keep cookies per host, not per port, so two dashboards on
    127.0.0.1 with one cookie name would sign each other out."""
    return COOKIE if port is None else f"{COOKIE}_{port}"


def app_cookie(app) -> str:
    return getattr(app.state, "cookie", None) or COOKIE


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
    if request.url.path == STATUS_PATH and request.method == "GET":  # the switcher probe; the route allows local only
        return await call_next(request)
    token = request.app.state.token
    cookie = app_cookie(request.app)
    given = request.query_params.get("token")
    if given is not None and _matches(given, token):
        rest = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "token"])
        response = RedirectResponse(request.url.path + (f"?{rest}" if rest else ""), status_code=303)
        response.set_cookie(cookie, token, max_age=COOKIE_DAYS * 24 * 3600, httponly=True, samesite="strict", path="/")
        return response
    if not _matches(request.cookies.get(cookie, ""), token):
        return HTMLResponse(UNAUTHORIZED, status_code=401)
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        return PlainTextResponse("cross-origin request refused", status_code=403)
    return await call_next(request)

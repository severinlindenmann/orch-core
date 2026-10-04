from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

COOKIE = "orch_theme"
THEMES = ("light", "dark", "system")


def _back_path(request: Request) -> str:
    """The same-origin page the switch was used on, else the start page."""
    referer = urlsplit(request.headers.get("referer", ""))
    if referer.netloc and referer.netloc == request.headers.get("host", ""):
        if referer.path.startswith("/") and not referer.path.startswith("//"):
            return referer.path + (f"?{referer.query}" if referer.query else "")
    return "/"


@router.post("/theme")
def set_theme(request: Request, theme: Annotated[str, Form()] = ""):
    response = RedirectResponse(_back_path(request), status_code=303)
    # "system" is stored too, so Auto overrides a light/dark workspace default, with or without JS.
    if theme in THEMES:
        response.set_cookie(COOKIE, theme, max_age=365 * 24 * 3600, path="/", samesite="strict")
    return response

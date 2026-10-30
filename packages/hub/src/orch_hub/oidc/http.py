"""The real network side of company login: two calls, 5 s timeout, answers capped at 64 KB, no redirects, https only
(plain http only to localhost, for the tests and a local IdP)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import Http, OidcError

TIMEOUT_S = 5
MAX_BYTES = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:
        return None


def _allowed(url: str) -> bool:
    u = urllib.parse.urlsplit(url)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"))


class UrlHttp(Http):
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _send(self, req: urllib.request.Request) -> dict[str, Any]:
        if not _allowed(req.full_url):
            raise OidcError("bad_discovery")
        try:
            with self._opener.open(req, timeout=TIMEOUT_S) as r:
                raw = r.read(MAX_BYTES + 1)
        except urllib.error.URLError, OSError, ValueError:
            raise OidcError("idp_unreachable") from None
        if len(raw) > MAX_BYTES:
            raise OidcError("idp_answer_too_big")
        try:
            obj = json.loads(raw)
        except ValueError:
            raise OidcError("idp_bad_answer") from None
        if type(obj) is not dict:
            raise OidcError("idp_bad_answer")
        return obj

    def get_json(self, url: str) -> dict[str, Any]:
        return self._send(urllib.request.Request(url, headers={"Accept": "application/json"}))

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(data).encode()
        return self._send(
            urllib.request.Request(
                url,
                data=body,
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            )
        )

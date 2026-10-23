"""Constrain all authenticated traffic to a small, auditable set of read routes."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from orch.net.http import RelayConn, Transport, TransportError

from .token import NoToken, Token

ORIGIN = "https://api.github.com"
DEADLINE_S = 15
BODY_CAP = 2 * 1024 * 1024
log = logging.getLogger("orch.github.client")
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_ETAG = re.compile(r'(?:W/)?"[\x21\x23-\x7e]{1,128}"')
_EXPIRATION = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} (?:UTC|[+-][0-9]{4})")
_PAGE_QUERY = frozenset({"per_page", "page"})
_LIST_QUERY = _PAGE_QUERY | {"state", "sort", "direction"}


@dataclass(frozen=True)
class Route:
    """Keep parameter grammar and response shape next to each allowed path."""

    template: str
    params: Mapping[str, str]
    query: frozenset[str] = frozenset()
    response_type: type = dict
    items_key: str | None = None


_REPO = {"owner": _NAME, "repo": _NAME}
_PULL = {**_REPO, "number": r"[1-9][0-9]{0,7}"}
_COMMIT = {**_REPO, "sha": r"[0-9a-f]{40}"}
ROUTES = {
    "repo": Route("/repos/{owner}/{repo}", _REPO),
    "pulls": Route("/repos/{owner}/{repo}/pulls", _REPO, _LIST_QUERY, list),
    "pull": Route("/repos/{owner}/{repo}/pulls/{number}", _PULL),
    "check_runs": Route("/repos/{owner}/{repo}/commits/{sha}/check-runs", _COMMIT, _PAGE_QUERY, dict, "check_runs"),
    "status": Route("/repos/{owner}/{repo}/commits/{sha}/status", _COMMIT),
    "reviews": Route("/repos/{owner}/{repo}/pulls/{number}/reviews", _PULL, _PAGE_QUERY, list),
    "issues": Route("/repos/{owner}/{repo}/issues", _REPO, _LIST_QUERY, list),
    "user": Route("/user", {}),
    "rate_limit": Route("/rate_limit", {}),
}
_KINDS = frozenset(
    {"network", "timeout", "too_large", "bad_reply", "unauthorized", "forbidden", "not_found", "rate_limited"}
)


class GitHubError(Exception):
    """Errors contain only bounded metadata, even when the server echoes credentials."""

    def __init__(self, kind: str, status: int = 0, route: str = "", *, retry_after_s: int = 60) -> None:
        self.kind = kind if type(kind) is str and kind in _KINDS else "bad_reply"
        self.status = status if type(status) is int and 0 <= status <= 599 else 0
        self.route = route if type(route) is str and route in {r.template for r in ROUTES.values()} else ""
        self.retry_after_s = 0
        if self.kind == "rate_limited":
            self.retry_after_s = max(0, min(3600, retry_after_s)) if type(retry_after_s) is int else 60
        super().__init__(self.kind, self.status, self.route)

    def __str__(self) -> str:
        return f"{self.kind} status={self.status} route={self.route}"

    def __repr__(self) -> str:
        return f"GitHubError({self.kind!r}, {self.status}, {self.route!r})"


@dataclass(frozen=True)
class RateInfo:
    limit: int | None
    remaining: int | None
    reset: int | None


@dataclass(frozen=True)
class Reply:
    status: int
    data: dict | list | None
    etag: str | None
    not_modified: bool
    rate: RateInfo
    token_expires: str | None


def _integer(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _shaped(value: object, pattern: re.Pattern) -> str | None:
    return value if type(value) is str and pattern.fullmatch(value) else None


def _header_int(value: str | None) -> int | None:
    # Bound decimal parsing without Python's integer-string limit losing absurd-but-numeric headers.
    if value is None or not re.fullmatch(r"[+-]?[0-9]+", value):
        return None
    digits = value.lstrip("+-").lstrip("0") or "0"
    number = int(digits[:12]) if len(digits) <= 12 else 10**12
    return -number if value.startswith("-") else number


def _rate(headers: Mapping[str, str]) -> RateInfo:
    values = [_header_int(headers.get(f"x-ratelimit-{key}")) for key in ("limit", "remaining", "reset")]
    return RateInfo(*(None if value is None else max(0, min(10**9, value)) for value in values))


def _reject_constant(value: str):
    raise ValueError("non-finite JSON")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON")
    return number


class GitHubClient:
    """Transport injection is for tests; the real transport is built only from ORIGIN.

    Credentials are fetched afresh on every request so rotation and logout take effect immediately.
    Like RelayConn, use one client per requesting thread.
    """

    def __init__(
        self,
        token_source: Callable[[], Token],
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._token_source = token_source
        self._transport = transport
        self._clock = clock
        self.last_rate: RateInfo | None = None
        self.requests_made = 0

    def __repr__(self) -> str:
        return "GitHubClient()"

    def get(self, route_name: str, params=None, query=None, *, etag: str | None = None) -> Reply:
        return self._request("GET", route_name, params, query, etag=etag)

    def _request(self, method: str, route_name: str, params=None, query=None, *, etag: str | None = None) -> Reply:
        """Validate and construct every target here; callers never supply a URL or raw target."""
        route = ROUTES.get(route_name) if type(route_name) is str else None
        template = route.template if route else ""
        if method != "GET" or route is None:
            raise GitHubError("bad_reply", route=template) from None
        params = {} if params is None else params
        query = {} if query is None else query
        if not isinstance(params, Mapping) or set(params) != set(route.params) or not isinstance(query, Mapping):
            raise GitHubError("bad_reply", route=template) from None
        for key, pattern in route.params.items():
            value = params[key]
            valid = _integer(value, 1, 10_000_000) if key == "number" else type(value) is str
            if not valid or not re.fullmatch(pattern, str(value)) or value in (".", ".."):
                raise GitHubError("bad_reply", route=template) from None
        enums = {"state": {"open", "closed", "all"}, "sort": {"created", "updated"}, "direction": {"asc", "desc"}}
        for key, value in query.items():
            if key not in route.query:
                raise GitHubError("bad_reply", route=template) from None
            valid = (
                _integer(value, 1, 100 if key == "per_page" else 1000)
                if key in _PAGE_QUERY
                else type(value) is str and value in enums[key]
            )
            if not valid:
                raise GitHubError("bad_reply", route=template) from None
        if etag is not None and _shaped(etag, _ETAG) is None:
            raise GitHubError("bad_reply", route=template) from None
        target = template.format(**params)
        if query:
            target += "?" + "&".join(f"{key}={query[key]}" for key in sorted(query))
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "orch-github",
        }
        if etag is not None:
            headers["If-None-Match"] = etag
        # a missing token is its own message-free error (NoToken, the caller maps it to github.no_token); any other
        # failure of the source is reported as "network" with no context, since its text is not ours to vouch for
        failure = None
        try:
            headers["Authorization"] = "Bearer " + self._token_source().reveal()
        except NoToken:
            raise
        except Exception:
            failure = "network"
        if failure is not None:
            raise GitHubError(failure, route=template) from None
        try:
            if self._transport is None:
                self._transport = RelayConn(ORIGIN)
            self.requests_made += 1
            response = self._transport.request(
                method, target, route=template, headers=headers, body=None, deadline_s=DEADLINE_S, cap=BODY_CAP
            )
        except TransportError as exc:
            failure = {"deadline": "timeout", "too_large": "too_large", "redirect": "bad_reply"}.get(
                exc.kind, "network"
            )
        except Exception:
            failure = "network"
        if failure is not None:
            # Outside the handler: neither cause nor context retains the lower-layer exception.
            raise GitHubError(failure, route=template) from None
        status, hdrs = response.status, response.headers
        self.last_rate = _rate(hdrs)
        log.debug("github %s status=%d bytes=%d", template, status, len(response.body))
        if status != 304 and not 200 <= status < 300:
            kind = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(status, "bad_reply")
            retry = 60
            if status in (403, 429) and (
                status == 429 or hdrs.get("x-ratelimit-remaining") == "0" or "retry-after" in hdrs
            ):
                kind = "rate_limited"
                if "retry-after" in hdrs:
                    retry = _header_int(hdrs["retry-after"])
                else:
                    reset = _header_int(hdrs.get("x-ratelimit-reset"))
                    retry = None if reset is None else math.ceil(reset - self._clock())
                retry = 60 if retry is None else retry
            raise GitHubError(kind, status, template, retry_after_s=retry) from None
        if len(response.body) > BODY_CAP:
            raise GitHubError("too_large", status, template) from None
        data = None
        invalid = False
        if status != 304:
            try:
                data = json.loads(
                    response.body.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float
                )
            except (ValueError, UnicodeError, RecursionError):
                invalid = True
            if invalid or type(data) is not route.response_type:
                raise GitHubError("bad_reply", status, template) from None
        return Reply(
            status,
            data,
            _shaped(hdrs.get("etag"), _ETAG),
            status == 304,
            self.last_rate,
            _shaped(hdrs.get("github-authentication-token-expiration"), _EXPIRATION),
        )

    def get_pages(self, route_name, params, query, *, max_pages, per_page=100, items_key=None) -> list:
        """Build bounded page numbers locally; response links never influence requests."""
        route = ROUTES.get(route_name) if type(route_name) is str else None
        template = route.template if route else ""
        if (
            route is None
            or not _PAGE_QUERY <= route.query
            or not _integer(max_pages, 1, 1000)
            or not _integer(per_page, 1, 100)
            or (query is not None and not isinstance(query, Mapping))
            or (query is not None and bool(_PAGE_QUERY.intersection(query)))
            or (items_key is not None and items_key != route.items_key)
        ):
            raise GitHubError("bad_reply", route=template) from None
        items_key = route.items_key
        result = []
        for page in range(1, max_pages + 1):
            reply = self.get(route_name, params, {**(query or {}), "page": page, "per_page": per_page})
            items = reply.data.get(items_key) if items_key and isinstance(reply.data, dict) else reply.data
            if type(items) is not list:
                raise GitHubError("bad_reply", reply.status, template) from None
            result.extend(items)
            if len(items) < per_page:
                break
        return result

    def shutdown(self) -> None:
        """Release the lazily created connection when its polling owner stops."""
        if self._transport is not None:
            self._transport.shutdown()

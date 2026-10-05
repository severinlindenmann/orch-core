"""Default deny for a request that came through the remote bridge.

A request carrying the remote marker (reach.remote_origin) is let through only when the dashboard's own router
matches it to a route whose tag, in the one central table below, is at or under the device's scope. A route with no
tag is refused, and so is every route tagged NEVER. Local requests (no marker) pass through untouched.

The tag is read for the route the dashboard's router itself matches (`route.matches`, the call its own routing
makes) on the request as received; shapes that could make two parsers disagree about the path are refused before
any matching.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, parse_qsl

from starlette.routing import Match

from orch.dashboard.reach import BadOrigin, RemoteOrigin, Scope, remote_origin

NEVER = None
ALLOWED_METHODS = ("GET", "HEAD", "POST")
MAX_PEEK = 65536  # the form body read to decide a conditional tag


@dataclass(frozen=True)
class Tag:
    scope: Scope | None  # None: never remote
    fresh: bool = False  # needs a fresh authenticator assertion for this very request (RemoteOrigin.fresh)
    kind: str | None = None  # a phone-switch kind (orch.remote.bridge.SWITCHED_KINDS) that must also be on


def _t(scope, *, fresh=False, kind=None) -> Tag:
    return Tag(scope, fresh, kind)


LOOK, DECIDE, OPERATE, TYPE = (_t(s) for s in Scope)
NO = _t(NEVER)
# Arming the AI Factory runner: the approve form carrying any of these fields arms or delegates.
_ARMING_FIELDS = ("factory", "delegate", "max_children", "max_size")


def _approve(params) -> Tag:
    """Decide for an ordinary approval; Type, with a fresh assertion, when the form carries the factory limits that
    arm the runner. A body this gate cannot read is treated as carrying them."""
    if params is None or any(v.strip() for k in _ARMING_FIELDS for v in params.get(k, ())):
        return _t(Scope.TYPE, fresh=True, kind="approve")
    return _t(Scope.DECIDE, kind="approve")


# (method, route path as declared) -> Tag, or a function of the request parameters (query and urlencoded form) that
# returns one. A route missing here is refused for a remote device.
TAGS: dict[tuple[str, str], Tag | Callable] = {
    # -- reads
    ("GET", "/"): LOOK, ("GET", "/board"): LOOK, ("GET", "/groom"): LOOK, ("GET", "/palette.json"): LOOK,
    ("GET", "/t/{ref}"): LOOK, ("GET", "/t/{ref}/raw"): LOOK, ("GET", "/t/{ref}/edit"): LOOK,
    ("GET", "/t/{ref}/agent/panel"): LOOK, ("GET", "/a/{ticket}/{name:path}"): LOOK, ("GET", "/new"): LOOK,
    ("GET", "/events"): LOOK, ("GET", "/agents"): LOOK, ("GET", "/timeline"): LOOK, ("GET", "/timeline.md"): LOOK,
    ("GET", "/activity"): LOOK, ("GET", "/activity.md"): LOOK, ("GET", "/reports"): LOOK,
    ("GET", "/reports.md"): LOOK, ("GET", "/design"): LOOK, ("GET", "/widgets"): LOOK,
    ("GET", "/w/preview/{ref}"): LOOK, ("GET", "/w/{ref}/{section}/{digest}"): LOOK,
    ("GET", "/wp/{addon}/{digest}"): LOOK, ("GET", "/wpf/{addon}/{digest}"): LOOK,
    ("GET", "/addons/{name}"): LOOK, ("GET", "/addons/{name}/"): LOOK, ("GET", "/addons/{name}/files/{token}"): LOOK,
    ("GET", "/terminals"): LOOK, ("GET", "/terminals/stream"): LOOK, ("GET", "/terminals/{name}"): LOOK,
    ("GET", "/terminals/{name}/stream"): LOOK,
    # -- the human decisions (a phone switch also applies where the signed phone path has one)
    ("POST", "/t/{ref}/approve"): _approve,
    ("POST", "/t/{ref}/approve-together"): _t(Scope.DECIDE, kind="approve"),
    ("POST", "/t/{ref}/request-changes"): _t(Scope.DECIDE, kind="request_changes"),
    ("POST", "/t/{ref}/answer"): _t(Scope.DECIDE, kind="answer"),
    ("POST", "/t/{ref}/verdict"): _t(Scope.DECIDE, kind="verdict"),
    ("POST", "/t/{ref}/move"): DECIDE,
    ("POST", "/t/{ref}/epic/pause"): DECIDE,  # only stops delegation
    ("POST", "/permits/{rid}/deny"): DECIDE,  # only refuses a request
    ("POST", "/permits/grants/{gid}/revoke"): DECIDE,  # only takes a grant away
    # -- ordinary ticket and wiki edits
    ("POST", "/t/{ref}/comment"): OPERATE, ("POST", "/t/{ref}/release"): OPERATE,
    ("POST", "/t/{ref}/artifacts"): OPERATE, ("POST", "/t/{ref}/edit"): OPERATE,
    ("POST", "/t/{ref}/task"): OPERATE, ("POST", "/t/{ref}/task/add"): OPERATE,
    ("POST", "/new"): _t(Scope.OPERATE, kind="ticket_request"),
    ("POST", "/board/backlog"): OPERATE, ("POST", "/theme"): OPERATE,
    ("POST", "/addons/{name}/refresh"): OPERATE,
    ("POST", "/addons/{name}/decisions"): OPERATE,  # an addon's decision applies an intent as the human
    # -- whatever makes the host run something
    ("POST", "/terminals/new"): TYPE, ("POST", "/terminals/{name}/keys"): TYPE,
    ("POST", "/terminals/{name}/size"): TYPE, ("POST", "/terminals/{name}/end"): TYPE,
    ("POST", "/t/{ref}/agent/start"): TYPE,
    ("POST", "/permits/{rid}/grant"): _t(Scope.TYPE, fresh=True),
    ("POST", "/addons/{name}/actions/{action_id}"): TYPE,  # runs addon code; a later change may lower it
    # -- never remote: the whole Workspace family (phones, permissions, addon install/update/trust/enable/disable,
    # settings, the agent-HTML switch, update-all), and anything that serves or starts the dashboard
    ("GET", "/workspace"): NO,
    ("POST", "/workspace/tidy"): NO, ("POST", "/workspace/shortcuts"): NO, ("POST", "/workspace/density"): NO,
    ("POST", "/workspace/addons/check-updates"): NO, ("POST", "/workspace/addons/update-all"): NO,
    ("POST", "/workspace/addons/{name}/enable"): NO, ("POST", "/workspace/addons/{name}/background"): NO,
    ("POST", "/workspace/addons/{name}/trust"): NO, ("POST", "/workspace/addons/{name}/settings"): NO,
    ("POST", "/workspace/phones/pair"): NO, ("POST", "/workspace/phones/{phone_id}/revoke"): NO,
    ("POST", "/workspace/phones/permissions"): NO,
}
STATIC_PREFIX = "/static/"  # the stylesheet, script and fonts: Look, GET or HEAD only

REFUSED = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not for this device</title><style>body{font:16px system-ui,sans-serif;margin:3rem 1rem;max-width:32rem}</style>
<h1>This device cannot do this</h1><p>%s</p></html>"""
_RAW_BAD = re.compile(rb"%2f|%5c|%00", re.I)


def _odd_path(path: str, raw: bytes) -> bool:
    """A path shape refused before any matching: an encoded slash or backslash, a null byte, an empty segment,
    a dot segment."""
    if _RAW_BAD.search(raw) or "\\" in path or "\x00" in path or "//" in path:
        return True
    return any(seg in (".", "..") for seg in path.split("/"))


def match_route(routes, scope):
    """The first route the dashboard's router itself matches fully, or None."""
    for route in routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def tag_for(routes, scope, params=None) -> Tag | None:
    """The tag of the route this scope matches; None when there is no route or no tag. A conditional tag is
    resolved with `params` (None means the parameters could not be read)."""
    method, path = scope["method"], scope["path"]
    if path.startswith(STATIC_PREFIX):
        return LOOK if method in ("GET", "HEAD") else None
    route = match_route(routes, scope)
    entry = TAGS.get((method, route.path)) if route is not None else None
    return entry(params) if callable(entry) else entry


def is_conditional(routes, scope) -> bool:
    route = match_route(routes, scope)
    return route is not None and callable(TAGS.get((scope["method"], route.path)))


async def _respond(send, status: int, message: str) -> None:
    body = (REFUSED % message).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
        (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'")]})
    await send({"type": "http.response.body", "body": body})


class RemoteGate:
    """ASGI middleware, outermost. `routes` is the dashboard's route list in routing order; `root` the workspace
    root (for the phone switches)."""

    def __init__(self, app, routes, root):
        self.app, self.routes, self.root = app, list(routes), root

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        try:
            origin = remote_origin(scope)
        except BadOrigin:
            return await self._deny(scope, send, "This request could not be identified.")
        if origin is None:
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await self._deny(scope, send, "")
        await self._remote(scope, receive, send, origin)

    async def _deny(self, scope, send, message, status=403):
        if scope["type"] == "http":
            await _respond(send, status, message or "Not available from here.")
        else:
            await send({"type": "websocket.close", "code": 1008})

    async def _remote(self, scope, receive, send, origin: RemoteOrigin):
        from orch.remote import bridge

        no = "Open the dashboard on the computer where it runs to do this."
        path, raw = scope.get("path", ""), scope.get("raw_path") or scope.get("path", "").encode()
        if scope["method"] not in ALLOWED_METHODS or _odd_path(path, raw):
            return await _respond(send, 403, no)
        query = scope.get("query_string", b"")
        pairs = parse_qsl(query.decode("latin-1").replace(";", "&"), keep_blank_values=True)
        if any(k.lower() == "token" for k, _ in pairs):
            return await _respond(send, 403, no)
        replay = receive
        params = None
        if is_conditional(self.routes, scope):
            body, replay, ok = await _peek(scope, receive)
            if ok:
                params = {}
                for k, v in pairs:
                    params.setdefault(k, []).append(v)
                for k, v in (parse_qs(body.decode("utf-8", "replace").replace(";", "&"), keep_blank_values=True)
                             if _is_form(scope) else {}).items():
                    params.setdefault(k, []).extend(v)
            elif body is None:
                return await _respond(send, 413, "That request is too large.")
        tag = tag_for(self.routes, scope, params)
        if tag is None or tag.scope is None:
            return await _respond(send, 403, no)
        if tag.fresh and not origin.fresh:
            return await _respond(send, 403, "This needs a fresh confirmation on this device first.")
        if not bridge.allows(self.root, tag.kind, origin, tag.scope):
            return await _respond(send, 403, "This device is not allowed to do this here.")
        await self.app(scope, replay, send)


def _is_form(scope) -> bool:
    for k, v in scope.get("headers", ()):
        if k.lower() == b"content-type":
            return v.lower().split(b";")[0].strip() == b"application/x-www-form-urlencoded"
    return False


async def _peek(scope, receive):
    """Read the request body (up to MAX_PEEK) and hand back a receive that replays it. (body, receive, readable):
    readable is False for a body that is not urlencoded or arrives with no length we trust; body None means too
    large."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        size += len(chunks[-1])
        if size > MAX_PEEK:
            return None, receive, False
        if not message.get("more_body"):
            break
    body = b"".join(chunks)
    sent = False

    async def replay():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay, _is_form(scope)

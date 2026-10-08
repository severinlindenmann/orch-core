"""Default deny for a request that came through the remote bridge.

A request carrying the remote marker (reach.remote_origin) is let through only when the dashboard's own router
matches it to a route whose tag, in the one central table below, is at or under the device's scope. A route with no
tag is refused, and so is every route tagged NEVER. Local requests (no marker) pass through untouched.

The tag is read for the route the dashboard's router itself matches (`route.matches`, the call its own routing
makes) on the request as received; shapes that could make two parsers disagree about the path are refused before
any matching.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, parse_qsl

from starlette.requests import Request
from starlette.routing import Match

from orch.dashboard.reach import SCOPE_KEY, BadOrigin, RemoteOrigin, Scope, remote_origin

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
    ("GET", "/t/{ref}/view/{name:path}"): LOOK,
    ("GET", "/events"): LOOK, ("GET", "/agents"): LOOK, ("GET", "/timeline"): LOOK, ("GET", "/timeline.md"): LOOK,
    ("GET", "/activity"): LOOK, ("GET", "/activity.md"): LOOK, ("GET", "/reports"): LOOK,
    ("GET", "/reports.md"): LOOK, ("GET", "/graph"): LOOK, ("GET", "/graph.json"): LOOK,
    ("GET", "/graph/related"): LOOK, ("GET", "/design"): LOOK, ("GET", "/guide"): LOOK, ("GET", "/widgets"): LOOK,
    ("GET", "/w/preview/{ref}"): LOOK, ("GET", "/w/{ref}/{section}/{digest}"): LOOK,
    ("GET", "/wp/{addon}/{digest}"): LOOK, ("GET", "/wpf/{addon}/{digest}"): LOOK,
    ("GET", "/addons/{name}"): LOOK, ("GET", "/addons/{name}/"): LOOK, ("GET", "/schedules"): LOOK,
    ("GET", "/quick"): LOOK, ("GET", "/quick/{qid}"): LOOK, ("GET", "/records"): LOOK,
    # -- watching a terminal needs Operate (live output can hold secrets); a download is a GET that consumes a
    # one-time token and deletes the file, so it counts as a change
    ("GET", "/terminals"): OPERATE, ("GET", "/terminals/stream"): OPERATE, ("GET", "/terminals/{name}"): OPERATE,
    ("GET", "/terminals/{name}/stream"): OPERATE,
    ("GET", "/terminals/{name}/snapshot"): OPERATE, ("GET", "/addons/{name}/files/{token}"): OPERATE,
    # -- the human decisions (a phone switch also applies where the signed phone path has one)
    ("POST", "/t/{ref}/approve"): _approve,
    ("POST", "/t/{ref}/approve-together"): _t(Scope.DECIDE, kind="approve"),
    ("POST", "/t/{ref}/request-changes"): _t(Scope.DECIDE, kind="request_changes"),
    ("POST", "/t/{ref}/answer"): _t(Scope.DECIDE, kind="answer"),
    ("POST", "/t/{ref}/verdict"): _t(Scope.DECIDE, kind="verdict"),
    ("POST", "/t/{ref}/move"): DECIDE,
    ("POST", "/t/{ref}/close"): DECIDE, ("POST", "/t/{ref}/reopen"): DECIDE,
    ("POST", "/t/{ref}/option"): DECIDE,  # an addon's yes/no on a ticket (the approve forms carry the same fields)
    ("POST", "/t/{ref}/epic/pause"): DECIDE,  # only stops delegation
    ("POST", "/permits/{rid}/deny"): DECIDE,  # only refuses a request
    ("POST", "/permits/grants/{gid}/revoke"): DECIDE,  # only takes a grant away
    ("POST", "/schedules/{sid}/pause"): DECIDE,  # only stops a schedule
    ("POST", "/schedules/runs/{rid}/{fid}/dismiss"): DECIDE,  # a finding needs nothing
    # -- ordinary ticket and wiki edits
    ("POST", "/t/{ref}/comment"): OPERATE, ("POST", "/t/{ref}/release"): OPERATE,
    ("POST", "/t/{ref}/artifacts"): OPERATE, ("POST", "/t/{ref}/edit"): OPERATE,
    ("POST", "/t/{ref}/task"): OPERATE, ("POST", "/t/{ref}/task/add"): OPERATE,
    ("POST", "/new"): _t(Scope.OPERATE, kind="ticket_request"),
    ("POST", "/schedules/runs/{rid}/{fid}/file"): _t(Scope.OPERATE, kind="ticket_request"),  # a backlog ticket
    ("POST", "/board/backlog"): OPERATE, ("POST", "/theme"): OPERATE,
    ("POST", "/addons/{name}/refresh"): OPERATE,
    ("POST", "/addons/{name}/decisions"): OPERATE,  # an addon's decision applies an intent as the human
    # -- whatever makes the host run something
    # a start (a new session, an agent) is never on the typing lease: each needs its own fresh assertion
    ("POST", "/terminals/new"): _t(Scope.TYPE, fresh=True), ("POST", "/terminals/{name}/keys"): TYPE,
    ("POST", "/terminals/{name}/size"): TYPE, ("POST", "/terminals/{name}/end"): TYPE,
    ("POST", "/t/{ref}/agent/start"): _t(Scope.TYPE, fresh=True),
    ("POST", "/records/commit"): TYPE,  # commits and pushes on the host (the route also refuses a paired device)
    ("POST", "/records/auto-off"): DECIDE,  # only takes power away (the route also refuses a paired device)
    ("POST", "/schedules/{sid}/run"): TYPE,  # starts an agent run on the host
    ("POST", "/schedules/{sid}/arm"): _t(Scope.TYPE, fresh=True),  # lets agents run on a clock, like the factory's start
    ("POST", "/permits/{rid}/grant"): _t(Scope.TYPE, fresh=True),
    ("POST", "/addons/{name}/actions/{action_id}"): TYPE,  # runs addon code; a later change may lower it
    # quick tasks (orch.core.quick): adding and closing are ordinary edits; reopen and drop are the human's call
    ("POST", "/quick/add"): OPERATE, ("POST", "/quick/{qid}/done"): OPERATE, ("POST", "/quick/{qid}/release"): OPERATE,
    ("POST", "/quick/{qid}/promote"): OPERATE,
    ("POST", "/quick/{qid}/reopen"): DECIDE, ("POST", "/quick/{qid}/drop"): DECIDE,
    ("POST", "/quick/{qid}/agent/start"): _t(Scope.TYPE, fresh=True),  # starts an agent on the host, like a ticket's Start agent
    # -- never remote: the whole Workspace family (phones, permissions, addon install/update/trust/enable/disable,
    # settings, the agent-HTML switch, update-all), and anything that serves or starts the dashboard
    ("GET", "/workspace"): NO,
    ("POST", "/workspace/tidy"): NO, ("POST", "/workspace/shortcuts"): NO, ("POST", "/workspace/density"): NO,
    ("POST", "/workspace/switcher"): NO,
    # the switcher probe: loopback only, never through the bridge
    ("GET", "/__orch/status"): NO,
    ("POST", "/workspace/addons/check-updates"): NO, ("POST", "/workspace/addons/update-all"): NO,
    ("POST", "/workspace/harnesses/check"): NO, ("POST", "/workspace/harnesses/{name}/update"): NO,
    ("POST", "/workspace/addons/{name}/enable"): NO, ("POST", "/workspace/addons/{name}/background"): NO,
    ("POST", "/workspace/addons/{name}/trust"): NO, ("POST", "/workspace/addons/{name}/settings"): NO,
    ("POST", "/workspace/phones/pair"): NO, ("POST", "/workspace/phones/{phone_id}/revoke"): NO,
    ("POST", "/workspace/phones/permissions"): NO,
    # the Remote tab: pairing, approving, rescoping, revoking and cutting off devices is only ever done here
    ("POST", "/workspace/remote/offer"): NO, ("POST", "/workspace/remote/disconnect"): NO,
    ("POST", "/workspace/remote/pending/{did}/approve"): NO, ("POST", "/workspace/remote/pending/{did}/reject"): NO,
    ("POST", "/workspace/remote/devices/{did}/scope"): NO, ("POST", "/workspace/remote/devices/{did}/revoke"): NO,
}
STATIC_PREFIX = "/static/"  # the stylesheet, script and fonts: Look, GET or HEAD only

REFUSED = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not for this device</title><style>body{font:16px system-ui,sans-serif;margin:3rem 1rem;max-width:32rem}</style>
<h1>This device cannot do this</h1><p>%s</p></html>"""
_RAW_BAD = re.compile(rb"%2f|%5c|%00", re.I)
_ENCODED_LEFT = re.compile(r"%[0-9a-fA-F]{2}")  # a decoded path that still holds an escape was double-encoded
FRESH = "This needs a fresh confirmation on this device first."
NO_WAY = "Open the dashboard on the computer where it runs to do this."  # the one text of every refusal
REMOTE_ADDON_UPLOAD = 25 * 1024 * 1024  # most a remote device may send to an addon action (local: MAX_UPLOAD)
REMOTE_POST_LIMIT = 25 * 1024 * 1024  # most any other remote POST body may carry
REMOTE_ARTIFACT_UPLOAD = 10 * 1024 * 1024  # most a remote device may send in one artifact upload request
TOO_BIG_REMOTE = "This file is too large for remote use. Send it as a separate file transfer instead."


def _odd_path(path: str, raw: bytes) -> bool:
    """A path shape refused before any matching: an encoded slash or backslash, a null byte, an empty segment,
    a dot segment."""
    if _RAW_BAD.search(raw) or "\\" in path or "\x00" in path or "//" in path \
            or _ENCODED_LEFT.search(path):
        return True
    return any(seg in (".", "..") for seg in path.split("/"))


def match_route(routes, scope):
    """The first route the dashboard's router itself matches fully, or None."""
    for route in routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def _match_params(routes, scope) -> dict:
    """The path parameters of the route the router matches ({} when none)."""
    for route in routes:
        match, child = route.matches(scope)
        if match is Match.FULL:
            return child.get("path_params", {})
    return {}


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


def refusal_response():
    """The gate's own refusal (403, the one text) for a route-level check that runs before a body is read."""
    from starlette.responses import HTMLResponse
    return HTMLResponse(REFUSED % NO_WAY, status_code=403, headers={
        "cache-control": "no-store", "x-content-type-options": "nosniff",
        "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'"})


def action_unlisted(request, name, action_id) -> bool:
    """Remote only: True when the addon does not list this action id in its manifest's remote_actions. An unknown
    addon or action answers the same, so a refusal never says whether the action exists. A local request is never
    refused here, and a malformed remote marker counts as remote."""
    if SCOPE_KEY not in request.scope:
        return False
    la = request.scope["app"].state.addons.registry.get(name)
    return la is None or not la.manifest.remote_action(action_id)


class RemoteGate:
    """ASGI middleware, outermost. `routes` is the dashboard's route list in routing order; `root` the workspace
    root (for the phone switches)."""

    def __init__(self, app, routes, ws):
        self.app, self.routes, self.ws = app, list(routes), ws
        self.root = ws.root

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        try:
            origin = remote_origin(scope)
        except BadOrigin:
            return await self._deny(scope, send, NO_WAY)
        if origin is None:
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return await self._deny(scope, send, "")
        await self._remote(scope, receive, send, origin)

    async def _deny(self, scope, send, message, status=403):
        if scope["type"] == "http":
            await _respond(send, status, message or NO_WAY)
        else:
            await send({"type": "websocket.close", "code": 1008})

    async def _remote(self, scope, receive, send, origin: RemoteOrigin):
        from orch.remote import bridge

        no = NO_WAY  # every refusal says the same, so a refusal never tells a route that exists from one that does not
        path, raw = scope.get("path", ""), scope.get("raw_path")
        if (not raw or scope.get("root_path") or scope["method"] not in ALLOWED_METHODS
                or _odd_path(path, raw)):
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
                return await _respond(send, 403, no)
        tag = tag_for(self.routes, scope, params)
        if tag is None or tag.scope is None:
            return await _respond(send, 403, no)
        if not bridge.allows(self.root, tag.kind, origin, tag.scope):
            return await _respond(send, 403, no)
        if scope["method"] == "POST" and path.startswith("/t/"):
            route_path, pp = self._match(scope)
            needed = await asyncio.to_thread(factory_need, self.ws, scope["method"], route_path, pp, tag)  # scans the workspace: in a thread
            if needed is not None:
                # a change under a running factory epic can start agents or alter a launched one's prompt; the epic's
                # own verdict ends it. Type for an edit, and always a fresh assertion over this very request.
                if origin.scope < needed[0] or not bridge.allows(self.root, tag.kind, origin, needed[0]):
                    return await _respond(send, 403, no)
                if not origin.fresh:
                    return await _respond(send, 403, FRESH)
        action = self._addon_action(scope)
        if action is not None and action_unlisted(Request(scope), *action):
            return await _respond(send, 403, no)  # this layer sits outside the others: the same bytes as any refusal
        if tag.fresh and not origin.fresh:  # only a device that may do this at all hears that it needs a confirmation
            return await _respond(send, 403, FRESH)
        await self.app(scope, replay, send)


    def _addon_action(self, scope):
        """(addon name, action id) when the router matches an addon action route, else None."""
        for route in self.routes:
            match, child = route.matches(scope)
            if match is Match.FULL:
                if route.path != "/addons/{name}/actions/{action_id}":
                    return None
                p = child.get("path_params", {})
                return p.get("name"), p.get("action_id")
        return None

    def _match(self, scope):
        """(the matched route's declared path, its path parameters); (None, {}) when nothing matches."""
        for route in self.routes:
            match, child = route.matches(scope)
            if match is Match.FULL:
                return route.path, child.get("path_params", {})
        return None, {}


# Under a running factory epic only pausing stays as tagged (Decide: it only stops work, and it is how a device
# stops the factory). Everything else, a comment included, needs a fresh assertion as on main; a comment keeps its
# own scope (Operate), an edit needs Type.
FACTORY_OPEN = ("/t/{ref}/epic/pause",)


def is_factory_epic(ws, ref) -> bool:
    """The ticket is an epic whose signed charter is a factory one (whatever its state). Fails closed: any doubt
    answers yes; only "no such ticket" and an epic-less ticket answer no."""
    from orch.core import epics, store
    from orch.errors import NotFoundError
    if not isinstance(ref, str) or not ref.strip():
        return True
    try:
        try:
            entry = store.resolve(ws, ref)
        except NotFoundError:
            return False
        if entry.meta is None:
            return True
        if not epics.is_epic(entry.meta):
            return False
        d = epics.delegation(ws, store.read_ticket(entry.path))
        return bool(d and d.get("factory"))
    except Exception:  # noqa: BLE001 - fail closed
        return True


def factory_need(ws, method, route_path, pp, tag):
    """For a remote POST under /t/: (the scope needed, the kind of subject the device approves) when the request must
    carry a fresh assertion because of the AI Factory, else None. The approve form that arms the runner (Type) and
    the epic's own verdict (Type); any other change on a factory epic or its child (Type; a comment keeps Operate).
    Pause is left as tagged."""
    if method != "POST" or not route_path or not route_path.startswith("/t/{ref}/") or route_path in FACTORY_OPEN:
        return None
    ref = pp.get("ref")
    if route_path == "/t/{ref}/approve" and tag is not None and tag.fresh:
        return Scope.TYPE, "charter"
    if route_path == "/t/{ref}/verdict" and is_factory_epic(ws, ref):
        return Scope.TYPE, "verdict"
    if tag is not None and tag.scope is not None and factory_guarded(ws, ref):
        kind = "verdict" if route_path == "/t/{ref}/verdict" else "action"
        # a decision, a close or a move here can start agents or reach a launched prompt: Type, like Start. Only a
        # comment keeps its own scope (Operate); pause is in FACTORY_OPEN.
        return (tag.scope if route_path == "/t/{ref}/comment" else Scope.TYPE), kind
    return None


def factory_guarded(ws, ref) -> bool:
    """The ticket is, or is a child of, an epic with an active factory delegation. Fails closed: an ambiguous
    ticket or parent, an unparseable or unreadable ticket or parent, any read error and a missing ref all answer
    yes. Only "no such ticket" (the handler refuses that itself) and a parent that does not exist answer no."""
    from orch.core import epics, store
    from orch.errors import NotFoundError
    if not isinstance(ref, str) or not ref.strip():
        return True
    try:
        entries = store.scan(ws)
        try:
            entry = store.resolve(ws, ref, entries)
        except NotFoundError:
            return False
        if entry.meta is None:
            return True
        epic_list = [store.read_ticket(entry.path)] if epics.is_epic(entry.meta) else []
        parent = epics._parent_id(entry.meta)  # the id the runner's epics.children() reads
        if parent:
            try:
                p = store.resolve(ws, parent, entries)
            except NotFoundError:
                p = None  # a dangling parent covers nothing
            if p is not None:
                if p.meta is None:
                    return True
                if epics.is_epic(p.meta):
                    epic_list.append(store.read_ticket(p.path))
        for epic in epic_list:
            d = epics.delegation(ws, epic)
            if d and d.get("factory") and d.get("active"):
                return True
        return False
    except Exception:  # noqa: BLE001 - fail closed (an ambiguous ref is a UsageError and lands here)
        return True


# an addon decision's intent kind -> (phone-switch kind, scope needed); anything else is refused
_INTENT_KINDS = {"none": (None, None), "answer": ("answer", Scope.DECIDE), "approve": ("approve", Scope.DECIDE),
                 "request_changes": ("request_changes", Scope.DECIDE), "verdict": ("verdict", Scope.DECIDE),
                 "move": (None, Scope.DECIDE), "new": ("ticket_request", Scope.OPERATE)}


def decision_refusal(request, ws, intent) -> str | None:
    """For a remote request only: why an addon decision's intent may not run (None: it may, or the request is
    local). The route is tagged Operate because the intent is only known after the addon answered; this applies
    the same scope, phone switch and factory rule the direct routes get. Unknown kinds are refused."""
    from orch.remote import bridge
    try:
        origin = remote_origin(request)
    except BadOrigin:
        return NO_WAY
    if origin is None:
        return None
    entry = _INTENT_KINDS.get(getattr(intent, "kind", None))
    if entry is None:
        return NO_WAY
    kind, needed = entry
    if needed is None:
        return None
    if not bridge.allows(ws.root, kind, origin, needed):
        return NO_WAY
    if intent.kind != "new" and not origin.fresh and factory_guarded(ws, intent.ref):
        return FRESH
    return None


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


_ACTION_KINDS = ("none", "close", "reopen", "import")  # all an addon action may return to change a ticket


def action_target_refusal(request, ws, target) -> str | None:
    """Remote only: refuse an addon action whose target ticket sits under a running factory epic, before any addon
    code runs, unless the device holds a fresh assertion."""
    try:
        origin = remote_origin(request)
    except BadOrigin:
        return NO_WAY
    if origin is None or origin.fresh or not isinstance(target, str) or not target.strip():
        return None
    return FRESH if factory_guarded(ws, target) else None


def action_refusal(request, ws, intent) -> str | None:
    """Remote only: the intent an addon action returned. Only none, close, reopen and import exist for actions;
    anything else is refused. A ticket-changing one under a running factory epic (or with no ticket named) needs a
    fresh assertion."""
    try:
        origin = remote_origin(request)
    except BadOrigin:
        return NO_WAY
    if origin is None:
        return None
    kind = getattr(intent, "kind", None)
    if kind not in _ACTION_KINDS:
        return NO_WAY
    if kind != "none" and not origin.fresh and factory_guarded(ws, getattr(intent, "ref", None)):
        return FRESH
    return None

import contextlib
import logging
import traceback
import urllib.parse
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from . import widget_frame
from .errors import HostError, host_response, presence_required_response, response, step_up_response
from .policy import ListenerPolicy, clean_path
from .remote_identity import REMOTE_STREAM, RemoteIdentity, route_digest, workspace_key
from .routegate import ALLOWED, PRE_LOGIN, PRESENCE, STEP_UP, TERMINALS, RouteGate, rule_device
from .testrun import UPLOAD_LIMIT

LOG = logging.getLogger(__name__)


def log_internal(exc: BaseException) -> None:
    """An `internal` answer says nothing to the client; the log says where it came from: the exception's type and the
    frames it passed (file:line function), never its message or any value, which may hold a secret (as core's
    `transport._log_internal`, #1659)."""
    where = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_tb(exc.__traceback__)[-8:]]
    LOG.error("request handler failed: %s at %s", type(exc).__name__, " < ".join(reversed(where)))


# Frames: only the locked Widgets route (see policy.py).
_FRAME_OWN = frozenset(
    {
        b"content-security-policy",
        b"x-frame-options",
        b"x-content-type-options",
        b"referrer-policy",
        b"cross-origin-resource-policy",
    }
)
# Bodies of the sign routes are tiny (ids, a hash, the switch's passphrase of at most 1024 characters);
# everything else keeps the general bound.
CLICK_BODY_LIMIT = 16_384
CLICK_STEP = ("/view", "/sign", "/cancel", "/state", "/custody")


#: The global routes a switch session may use (their answers are cut down to the bound workspace in app.py).
SWITCH_GLOBAL = frozenset(
    {
        ("GET", "/api/session"),
        ("GET", "/api/me"),
        ("GET", "/api/workspaces"),
        ("GET", "/api/events"),  # only with ?ws=<bound>
        ("GET", "/api/host"),  # the closed dev facts of the host (Settings › General); read only
        ("POST", "/auth/logout"),
        # E4: there is no way up from a switch session. A full session comes only from a browser key proof made
        # BEFORE the ticket is used (`/auth/switch`); a step-up adopts a new token from a launch link.
    }
)


#: The only non-GET routes of the bound workspace a switch session may call (a ticket to the hub, not a write).
SWITCH_POST = frozenset({"hub/switch"})
#: Writes a switch session is told plainly it cannot make (`session.readonly`, not a step-up offer): there is no way up
#: from a switch session (E4). The one-click share reductions are owner actions of a full session (hub.py).
READONLY_REFUSED = (
    "/click/prepare",
    "/hub/approve/prepare",
    "/market/add",
    "/hub/share/revoke",
    "/hub/profile",
    "/hub/share/rule-remove",
    "/shares/register",
    "/hub/share/rule-add",
    "/hub/share/sync",
    "/org/accept",
    "/org/decline",
    "/org/sync",
    "/org/tool",
    "/remote/off",
    "/remote/code",
)


def switch_allows(method, path, bound, query=None) -> bool:
    """The ONE table of what a switch session may reach: READS of its own workspace (GET/HEAD, never a shell on the
    server), `SWITCH_POST`, and the named global routes. `bound` is the workspace of its ticket."""
    if (method, path) in SWITCH_GLOBAL:
        return path != "/api/events" or (query or {}).get("ws") == [bound]
    parts = path.split("/")
    if len(parts) > 4 and parts[:3] == ["", "api", "w"] and parts[3] == bound:
        if parts[4] == "terminals" or parts[-1] == "attach":
            return False
        return method in ("GET", "HEAD") or (method == "POST" and "/".join(parts[4:]) in SWITCH_POST)
    return False


#: Headers a stream's device may not send (D89, contract 10.2c): a request that carries one is answered 400
#: `remote.bearer` (an ordinary response; the stream stays). Refused, never stripped.
BEARER_NAMES = (b"authorization", b"cookie", b"x-setup-token")
BEARER_PREFIX = b"x-orch-"
#: What the dashboard's own click routes read from a page that is current: the stream has no page, and the check is
#: ours to answer (Security adds it AFTER the device's headers passed, it never takes one from the device).
CLICK_CONTRACT = (b"x-orch-click-contract", b"4")


def bearer_headers(headers) -> bool:
    return any(k.lower() in BEARER_NAMES or k.lower().startswith(BEARER_PREFIX) for k, _ in headers)


class Security:
    """ASGI checks before routing, including duplicate headers and streamed body limits."""

    def __init__(self, app, auth, policy: ListenerPolicy, others=(), registry=None, router=None, presence=None):
        """``policy`` is the default listener (loopback). ``others`` are further listeners' policies; a request is
        handled by the one named in ``scope["orch_listener"]``, which only the server that accepted the connection
        sets (front.Front), never a header. A name that is not configured is refused (fail closed)."""
        self.app, self.auth, self.policy = app, auth, policy
        # Takes the presence of a stream's route from the click signer (D88, `click.py`): returns, or raises HostError.
        self.presence = presence
        # The app's router, for the remote route gate. Without one a gated listener answers nothing (fail closed).
        self.gate = RouteGate(router) if router is not None else None
        # `registry` (create_app's `app.state.policies`) is shared: taking a policy out of it turns that listener off
        self.policies = registry if registry is not None else {p.name: p for p in (policy, *others)}

    def policy_of(self, scope):
        return self.policies.get(scope.get("orch_listener", self.policy.name))

    def session_ok(self, token, policy=None):
        """A live session that belongs to this listener: opened on it (a remote bearer on loopback is as unknown as a
        made-up token, and a launch-code session on the front too) and of a kind it accepts."""
        policy = policy or self.policy
        return (
            self.auth.valid(token)
            and self.auth.listener_of(token) == policy.name
            and policy.session_kind_ok(self.auth.login_kind(token))
        )

    def stream_identity(self, scope, policy) -> RemoteIdentity | None:
        """The identity of a stream request, or None. It comes from the scope the in-process adapter built, bound three
        ways to ONE stream: the identity's, the context value only the adapter sets, and the stream the credential was
        minted for. Anything missing or different is None (fail closed); nothing here reads a header or a query."""
        state = scope.get("state") or {}
        identity, token = state.get("remote"), state.get("token")
        stream = REMOTE_STREAM.get()
        if (
            not isinstance(identity, RemoteIdentity)
            or not isinstance(token, str)
            or stream is None
            or stream != identity.stream_id
            or self.auth.stream_of(token) != identity.stream_id
            or not self.session_ok(token, policy)
        ):
            return None
        return identity

    def route_check(self, scope, policy):
        """The one place a route table is enforced: ``(refusal, rule, template)``; ``refusal`` is None when the route
        answers.

        On a listener with ``route_gate`` only a route whose rule is ALLOWED, STEP_UP (its handler asks for the passkey,
        `stepup.refuse_step_up`), PRE_LOGIN, or TERMINALS (where the policy has terminals on) answers; a refused,
        unlisted or unmatched route does not. A stream (``policy.stream``) uses its own column and the access level of
        the identity the host gave the stream (`routegate.REMOTE_DEVICE`), refusing with ``remote.level`` where a
        higher level would do. ``rule`` is the stream rule (PRESENCE: the caller must take a presence first)."""
        if not policy.route_gate:
            return None, None, None
        if self.gate is None:
            return "remote.refused", None, None
        template = self.gate.template(scope)
        if policy.stream:
            identity = self.stream_identity(scope, policy)
            if template is None or identity is None:
                return "remote.refused", None, template
            method = "WEBSOCKET" if scope["type"] == "websocket" else scope["method"]
            rule, refusal = rule_device(method, template, identity.level)
            return refusal, rule, template
        rule = self.gate.rule(scope)
        ok = rule in (ALLOWED, STEP_UP, PRE_LOGIN) or (rule == TERMINALS and policy.terminals)
        return (None if ok else "remote.refused"), rule, template

    def route_ok(self, scope, policy) -> bool:
        return self.route_check(scope, policy)[0] is None

    @staticmethod
    def in_stream_workspace(identity: RemoteIdentity, scope) -> bool:
        """A stream sees ONE workspace (contract row 66): a plain path, and every `{ws}` of the request (the path
        segment of `/api/w/{ws}/...`, the `ws` of `/api/events`) names the stream's workspace; else 404."""
        path = scope["path"]
        raw = scope.get("raw_path")
        if clean_path(path) is None or (raw is not None and raw != path.encode("ascii", "replace")):
            return False
        mine = workspace_key(identity.workspace_id)
        parts = path.split("/")
        if parts[:3] == ["", "api", "w"]:
            return len(parts) > 4 and workspace_key(parts[3]) == mine
        if path == "/api/events":
            wanted = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin1")).get("ws")
            return wanted is not None and len(wanted) == 1 and workspace_key(wanted[0]) == mine
        return True

    def take_presence(self, identity: RemoteIdentity, method, template, path, query, body) -> str | None:
        """Take the presence that covers this very request (D88): its `route_digest` must be in a live batch of THIS
        stream. None when taken; else the digest to ask for (`sha256:<hex>`), or a HostError of the signer."""
        digest = "sha256:" + route_digest(identity.workspace_id, method, template, path, query, body).hex()
        if self.presence is None:
            raise HostError({"code": "click.unavailable", "message": "no signer", "retryable": False}, 9)
        try:
            self.presence(identity.workspace_id, digest)
        except HostError as exc:
            if exc.error.get("code") == "remote.presence_required":
                return digest
            raise
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        policy = self.policy_of(scope)
        if policy is None:
            return await self.refuse(scope, receive, send)
        if scope["type"] == "websocket":
            return await self.websocket(scope, receive, send, policy)
        path, method = scope["path"], scope["method"]
        headers = scope["headers"]

        def values(name):
            return [v.decode("latin1") for k, v in headers if k.lower() == name]

        started = False

        async def secure_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                hs = list(message.get("headers", []))
                if policy.widget_frames and path.startswith(widget_frame.PREFIX):
                    # The Widgets frame: its own CSP REPLACES the global one (two CSP headers would both apply), and
                    # it carries no X-Frame-Options (DENY would stop the dashboard framing it; `frame-ancestors` names
                    # the dashboard). One place, whatever the route set.
                    hs = [(k, v) for k, v in hs if k.lower() not in _FRAME_OWN]
                    hs += [
                        (b"content-security-policy", widget_frame.frame_csp(path, policy.port).encode()),
                        (b"cross-origin-resource-policy", b"same-origin"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                    ]
                else:
                    hs += [
                        (b"content-security-policy", policy.csp().encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        *policy.extra_headers,
                    ]
                if path.startswith(("/api", "/auth")):
                    hs = [(k, v) for k, v in hs if k.lower() != b"cache-control"]
                    hs.append((b"cache-control", b"no-store"))
                message["headers"] = hs
            await send(message)

        async def reject(code, status):
            await response(code, status)(scope, receive, secure_send)

        setup = path == "/setup" or path == "/auth/setup" or path.startswith("/api/setup/")
        host = values(b"host")
        setup_host = setup or path.startswith("/assets/") or path in ("/favicon.svg", "/loading.css")
        allowed = policy.hosts_for(setup_host)
        origin = policy.origin_for(host[0]) if len(host) == 1 else ""
        if len(host) != 1 or host[0] not in allowed:
            return await reject("host.bad", 421)
        origins = values(b"origin")
        if policy.stream:
            if origins:  # the adapter sets no Origin; a device's is refused before it gets here (contract row 64)
                return await reject("origin.bad", 403)
        elif origins and origins != [origin]:
            return await reject("origin.bad", 403)
        api = path == "/api" or path.startswith("/api/")
        if not policy.launch_codes and path == "/auth/session":
            return await reject("not_found", 404)
        if not policy.widget_frames and path.startswith(widget_frame.PREFIX):
            return await reject("not_found", 404)  # off means off, before and after login
        identity = None
        if policy.stream:
            # D89: no route answers before login here, and the credential is the one the adapter minted for THIS
            # stream (scope, not a header). Then: no outside bearer, a plain path, and only this stream's workspace.
            identity = self.stream_identity(scope, policy)
            if identity is None:
                return await reject("not_found", 404)
            if bearer_headers(headers):
                return await reject("remote.bearer", 400)
            if not self.in_stream_workspace(identity, scope):
                return await reject("not_found", 404)
        elif not policy.pre_login(path, scope.get("raw_path"), method):
            # Before login only the policy's routes answer; everything else needs a session of this listener.
            auth = values(b"authorization")
            bearer = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
            if not self.session_ok(bearer, policy):
                return await reject("not_found", 404)
        refusal, rule, template = self.route_check(scope, policy)
        if refusal is not None:
            return await reject(refusal, 403)  # after the session test above: nobody learns routes before login
        if api or path == "/auth/logout":
            auth = values(b"authorization")
            token = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
            if policy.stream:
                token = scope["state"]["token"]  # `stream_identity` checked it above
            if setup:
                tokens = values(b"x-setup-token")
                token = tokens[0] if len(tokens) == 1 and len(tokens[0]) <= 200 else ""
            if not (self.auth.valid_setup(token) if setup else self.session_ok(token, policy)):
                return await reject("session.required", 401)
            scope.setdefault("state", {})["token"] = token
            refusal = self.switch_refused(token, path, method, scope)
            if refusal and path.endswith(READONLY_REFUSED):
                return await response("session.readonly", 403)(scope, receive, secure_send)
            if refusal:
                return await step_up_response(policy.name != "loopback")(scope, receive, secure_send)
        if method not in ("GET", "HEAD", "OPTIONS"):
            if not policy.stream and origins != [origin]:
                return await reject("origin.bad", 403)
        sites = values(b"sec-fetch-site")
        if sites and sites != ["same-origin"] and (method not in ("GET", "HEAD", "OPTIONS") or api):
            # Fetch Metadata when present: an API call (read or write) from another site is refused.
            return await reject("origin.bad", 403)
        if (
            method in ("POST", "PUT", "PATCH")
            and path != "/auth/logout"
            and not (method == "POST" and path.startswith("/api/w/") and path.endswith("/records/auto-off"))
            and values(b"content-type") != ["application/json"]
        ):
            return await reject("invalid.input", 400)  # every browser mutation is bounded JSON
        click_step = path.startswith("/api/w/") and "/click/" in path and path.endswith(CLICK_STEP)
        records_step = path.startswith("/api/w/") and path.endswith(("/records/commit", "/records/auto-off"))
        # #1198 C15: a records admin step carries at most one address (2048 characters, JSON-escaped)
        records_admin = path.startswith("/api/w/") and path.endswith(
            ("/records/init", "/records/remote", "/records/push-initial")
        )
        limit = (
            1024
            if path in ("/auth/session", "/auth/switch", "/auth/switch/challenge") or records_step
            else 8192
            if records_admin
            else CLICK_BODY_LIMIT
            if click_step or path.startswith(("/auth/passkey/", "/auth/enroll/"))  # pre-login, from the internet
            else 1_048_576
        )
        if method == "POST" and path.startswith("/api/testrun/") and path.endswith("/images"):
            limit = UPLOAD_LIMIT  # a screenshot of the tester's checklist (base64 of at most 5 MB)
        if setup:
            total = 0

            async def bounded_receive():
                nonlocal total
                event = await receive()
                total += len(event.get("body", b""))
                if total > (1024 if path == "/auth/setup" else 65536):
                    raise ValueError("setup body too large")
                return event

            try:
                return await self.app(scope, bounded_receive, secure_send)
            except Exception:
                if not started:
                    return await reject("invalid.input", 400)
                return
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > limit:
                return await reject("invalid.input", 413)
            if not event.get("more_body"):
                break
        if policy.stream and rule == PRESENCE:
            # D88: a presence route runs only with a presence signature of the device that covers THIS request (its
            # concrete path, query and body); the signer takes it from the stream's batch, once.
            try:
                missing = await run_in_threadpool(
                    self.take_presence,
                    identity,
                    method,
                    template,
                    path,
                    scope.get("query_string", b"").decode("latin1"),
                    bytes(body),
                )
            except HostError as exc:
                return await host_response(exc)(scope, receive, secure_send)
            if missing is not None:
                return await presence_required_response(missing, template)(scope, receive, secure_send)
        if policy.stream:
            # the dashboard's own click routes ask for a current page; a stream has none, and the answer is ours
            scope = {**scope, "headers": [*headers, CLICK_CONTRACT]}
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        try:
            await self.app(scope, replay, secure_send)
        except Exception as exc:
            log_internal(exc)
            if started:
                raise
            # No body/header interpolation in errors or logs.
            await reject("internal", 500)

    def switch_refused(self, token, path, method, scope) -> bool:
        """A session opened by a hub switch (H1) is read-mostly on ONE workspace until a real sign-in: routes default
        CLOSED. It reaches only `switch_allows` (below); every other API or auth route is refused, so a route added
        later is closed for it until someone opens it here on purpose."""
        bound = self.auth.switch_workspace(token)
        if bound is None:
            return False
        query = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin1"))
        return not switch_allows(method, path, bound, query)

    @staticmethod
    async def deny(scope, send, digest, template):
        """A WebSocket that needs a presence: the 428 as an HTTP denial response where the server has the extension
        (the adapter does), else just a close. A signer error is a close."""
        if digest.startswith("sha256:") and "websocket.http.response" in scope.get("extensions", {}):
            body = presence_required_response(digest, template).body
            await send(
                {
                    "type": "websocket.http.response.start",
                    "status": 428,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")],
                }
            )
            await send({"type": "websocket.http.response.body", "body": body})
            return
        await send({"type": "websocket.close", "code": 1008})

    async def refuse(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await response("not_found", 404)(scope, receive, send)

    async def websocket(self, scope, receive, send, policy):
        """Section 12.6.6: only `/term`; closed before `accept` unless Host and Origin are exactly this server's
        (`localhost`, a missing or `null` Origin refused), with no subprotocol. Cookies are never read (dropped from the
        scope). permessage-deflate is off and `ws_max_size` is 64 KiB in the server's configuration (`__main__`). The
        bearer is checked through the browser ticket (the first message), not here."""
        headers = scope["headers"]

        def values(name):
            return [v.decode("latin1") for k, v in headers if k.lower() == name]

        if (
            not policy.websocket_ok(scope["path"], values(b"host"), values(b"origin"))
            or values(b"sec-websocket-protocol")
            or scope.get("subprotocols")
            or scope.get("query_string")
        ):
            await send({"type": "websocket.close", "code": 1008})
            return
        identity = None
        if policy.stream:
            identity = self.stream_identity(scope, policy)
            if identity is None or bearer_headers(headers) or not self.in_stream_workspace(identity, scope):
                await send({"type": "websocket.close", "code": 1008})
                return
        refusal, rule, template = self.route_check(scope, policy)
        if refusal is not None:
            await send({"type": "websocket.close", "code": 1008})
            return
        if policy.stream and rule == PRESENCE:
            # each terminal open and each attach is its own presence request (D88); a WebSocket has no body
            try:
                missing = await run_in_threadpool(
                    self.take_presence, identity, "WEBSOCKET", template, scope["path"], "", b""
                )
            except HostError:
                missing = "refused"
            if missing is not None:
                await self.deny(scope, send, missing, template)
                return
        scope = {**scope, "headers": [(k, v) for k, v in headers if k.lower() != b"cookie"]}
        try:
            await self.app(scope, receive, send)
        except Exception:
            # No frame, ticket or byte in a log: the socket just closes.
            with contextlib.suppress(Exception):
                await send({"type": "websocket.close", "code": 4500})

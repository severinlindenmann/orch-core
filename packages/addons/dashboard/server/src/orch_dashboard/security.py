import contextlib
import urllib.parse

from . import widget_frame
from .errors import response, step_up_response
from .policy import ListenerPolicy
from .routegate import ALLOWED, PRE_LOGIN, STEP_UP, TERMINALS, RouteGate
from .testrun import UPLOAD_LIMIT

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
        # run 12: the way UP. A switch session proves it holds a browser key that a real local sign-in enrolled
        # (`Auth.upgrade`); nothing else is opened by these two.
        ("POST", "/api/auth/challenge"),
        ("POST", "/api/auth/upgrade"),
    }
)


#: The only non-GET routes of the bound workspace a switch session may call (a ticket to the hub, not a write).
SWITCH_POST = frozenset({"hub/switch"})


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


class Security:
    """ASGI checks before routing, including duplicate headers and streamed body limits."""

    def __init__(self, app, auth, policy: ListenerPolicy, others=(), registry=None, router=None):
        """``policy`` is the default listener (loopback). ``others`` are further listeners' policies; a request is
        handled by the one named in ``scope["orch_listener"]``, which only the server that accepted the connection
        sets (front.Front), never a header. A name that is not configured is refused (fail closed)."""
        self.app, self.auth, self.policy = app, auth, policy
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

    def route_ok(self, scope, policy) -> bool:
        """The one place the remote route table is enforced. On a listener with ``route_gate`` only a route whose rule
        is ALLOWED, STEP_UP (its handler asks for the passkey, `stepup.refuse_step_up`), PRE_LOGIN, or TERMINALS
        (where the policy has terminals on) answers; a refused, unlisted or unmatched route does not."""
        if not policy.route_gate:
            return True
        rule = self.gate.rule(scope) if self.gate is not None else None
        return rule in (ALLOWED, STEP_UP, PRE_LOGIN) or (rule == TERMINALS and policy.terminals)

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
        if origins and origins != [origin]:
            return await reject("origin.bad", 403)
        api = path == "/api" or path.startswith("/api/")
        if not policy.launch_codes and path == "/auth/session":
            return await reject("not_found", 404)
        if not policy.widget_frames and path.startswith(widget_frame.PREFIX):
            return await reject("not_found", 404)  # off means off, before and after login
        if not policy.pre_login(path, scope.get("raw_path"), method):
            # Before login only the policy's routes answer; everything else needs a session of this listener.
            auth = values(b"authorization")
            bearer = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
            if not self.session_ok(bearer, policy):
                return await reject("not_found", 404)
        if not self.route_ok(scope, policy):
            return await reject(
                "remote.refused", 403
            )  # after the session test above: nobody learns routes before login
        if api or path == "/auth/logout":
            auth = values(b"authorization")
            token = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
            if setup:
                tokens = values(b"x-setup-token")
                token = tokens[0] if len(tokens) == 1 and len(tokens[0]) <= 200 else ""
            if not (self.auth.valid_setup(token) if setup else self.session_ok(token, policy)):
                return await reject("session.required", 401)
            scope.setdefault("state", {})["token"] = token
            refusal = self.switch_refused(token, path, method, scope)
            if refusal:
                return await step_up_response(policy.name != "loopback")(scope, receive, secure_send)
        if method not in ("GET", "HEAD", "OPTIONS"):
            if origins != [origin]:
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
            if path in ("/auth/session", "/auth/switch") or records_step
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
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        try:
            await self.app(scope, replay, secure_send)
        except Exception:
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
        if not self.route_ok(scope, policy):
            await send({"type": "websocket.close", "code": 1008})
            return
        scope = {**scope, "headers": [(k, v) for k, v in headers if k.lower() != b"cookie"]}
        try:
            await self.app(scope, receive, send)
        except Exception:
            # No frame, ticket or byte in a log: the socket just closes.
            with contextlib.suppress(Exception):
                await send({"type": "websocket.close", "code": 4500})

import contextlib

from . import widget_frame
from .errors import response
from .testrun import UPLOAD_LIMIT

# §0.8.6 item 7: the required directives (default-src, script-src, object-src, base-uri, frame-ancestors, form-action)
# plus the narrow extras this app needs (its own styles, connections and inline-free images).
# `connect-src` adds the terminal WebSocket on this server's own port only (section 12.6.6: no `ws:` wildcard, no
# `localhost`); `script-src` and `style-src` are not loosened for xterm.
CSP = (
    "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; style-src 'self'; connect-src 'self' ws://127.0.0.1:{port}; img-src 'self' data:; "
    "frame-src http://127.0.0.1:{port}/widget-frame/"
)
# Frames: only the locked Widgets route (a path source, never `'self'`). Without `frame-src` the `default-src 'self'`
# fallback allowed any same-origin frame; this narrows that. On or off is the route's own check, not this header.
_FRAME_OWN = frozenset(
    {
        b"content-security-policy",
        b"x-frame-options",
        b"x-content-type-options",
        b"referrer-policy",
        b"cross-origin-resource-policy",
    }
)
TERM_PATH = "/term"
# Bodies of the sign routes are tiny (ids, a hash, the switch's passphrase of at most 1024 characters);
# everything else keeps the general bound.
CLICK_BODY_LIMIT = 16_384
CLICK_STEP = ("/view", "/sign", "/cancel", "/state", "/custody")


class Security:
    """ASGI checks before routing, including duplicate headers and streamed body limits."""

    def __init__(self, app, port, auth):
        self.app, self.port, self.auth = app, port, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await self.websocket(scope, receive, send)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
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
                if path.startswith(widget_frame.PREFIX):
                    # The Widgets frame: its own CSP REPLACES the global one (two CSP headers would both apply), and
                    # it carries no X-Frame-Options (DENY would stop the dashboard framing it; `frame-ancestors` names
                    # the dashboard). One place, whatever the route set.
                    hs = [(k, v) for k, v in hs if k.lower() not in _FRAME_OWN]
                    hs += [
                        (b"content-security-policy", widget_frame.frame_csp(path, self.port).encode()),
                        (b"cross-origin-resource-policy", b"same-origin"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                    ]
                else:
                    hs += [
                        (b"content-security-policy", CSP.format(port=self.port).encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
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
        allowed = [f"127.0.0.1:{self.port}", f"localhost:{self.port}"] if setup_host else [f"127.0.0.1:{self.port}"]
        origin = f"http://{host[0]}" if len(host) == 1 else ""
        if len(host) != 1 or host[0] not in allowed:
            return await reject("host.bad", 421)
        origins = values(b"origin")
        if origins and origins != [origin]:
            return await reject("origin.bad", 403)
        api = path == "/api" or path.startswith("/api/")
        if api or path == "/auth/logout":
            auth = values(b"authorization")
            token = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
            if setup:
                tokens = values(b"x-setup-token")
                token = tokens[0] if len(tokens) == 1 and len(tokens[0]) <= 200 else ""
            if not (self.auth.valid_setup(token) if setup else self.auth.valid(token)):
                return await reject("session.required", 401)
            scope.setdefault("state", {})["token"] = token
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
            if path == "/auth/session" or records_step
            else 8192
            if records_admin
            else CLICK_BODY_LIMIT
            if click_step
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

    async def websocket(self, scope, receive, send):
        """Section 12.6.6: only `/term`; closed before `accept` unless Host and Origin are exactly this server's
        (`localhost`, a missing or `null` Origin refused), with no subprotocol. Cookies are never read (dropped from the
        scope). permessage-deflate is off and `ws_max_size` is 64 KiB in the server's configuration (`__main__`). The
        bearer is checked through the browser ticket (the first message), not here."""
        headers = scope["headers"]

        def values(name):
            return [v.decode("latin1") for k, v in headers if k.lower() == name]

        if (
            scope["path"] != TERM_PATH
            or values(b"host") != [f"127.0.0.1:{self.port}"]
            or values(b"origin") != [f"http://127.0.0.1:{self.port}"]
            or values(b"sec-websocket-protocol")
            or scope.get("subprotocols")
            or scope.get("query_string")
        ):
            await send({"type": "websocket.close", "code": 1008})
            return
        scope = {**scope, "headers": [(k, v) for k, v in headers if k.lower() != b"cookie"]}
        try:
            await self.app(scope, receive, send)
        except Exception:
            # No frame, ticket or byte in a log: the socket just closes.
            with contextlib.suppress(Exception):
                await send({"type": "websocket.close", "code": 4500})

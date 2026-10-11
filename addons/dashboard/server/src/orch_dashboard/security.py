from .errors import response

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


class Security:
    """ASGI checks before routing, including duplicate headers and streamed body limits."""

    def __init__(self, app, port, auth):
        self.app, self.port, self.auth = app, port, auth

    async def __call__(self, scope, receive, send):
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
                hs += [
                    (b"content-security-policy", CSP.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ]
                if path.startswith(("/api", "/auth")):
                    hs = [(k, v) for k, v in hs if k.lower() != b"cache-control"]
                    hs.append((b"cache-control", b"no-store"))
                message["headers"] = hs
            await send(message)

        async def reject(code, status):
            await response(code, status)(scope, receive, secure_send)

        origin = f"http://127.0.0.1:{self.port}"
        if values(b"host") != [f"127.0.0.1:{self.port}"]:
            return await reject("host.bad", 421)
        origins = values(b"origin")
        if origins and origins != [origin]:
            return await reject("origin.bad", 403)
        api = path == "/api" or path.startswith("/api/")
        if api or path == "/auth/logout":
            auth = values(b"authorization")
            token = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
            if not self.auth.valid(token):
                return await reject("session.required", 401)
            scope.setdefault("state", {})["token"] = token
        if method not in ("GET", "HEAD", "OPTIONS"):
            if origins != [origin]:
                return await reject("origin.bad", 403)
            sites = values(b"sec-fetch-site")
            if sites and sites != ["same-origin"]:
                return await reject("origin.bad", 403)
        if path == "/auth/session" and method == "POST" and not self.auth.allow_attempt():
            return await reject("rate.limited", 429)
        if path == "/auth/session" and values(b"content-type") != ["application/json"]:
            return await reject("invalid.input", 400)
        limit = 1024 if path == "/auth/session" else 1_048_576
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

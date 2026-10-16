"""Body-oblivious HTTP proxy to the setup process. No retries, caches, logs or host/key APIs.

Only /claim is used for the launch exchange; launch and shutdown are never browser routes.
The setup process owns code expiry, one-use binding, idle expiry (except during a run), and completion.
"""

import asyncio
import http.client
import json
import os
import socket
import stat
import threading
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..hostlink import check_uid

MAX_BODY = 64 * 1024
ROUTES = {
    "state": "GET",
    "profile": "POST",
    "secrets/reveal": "POST",
    "secrets/confirm": "POST",
    "review": "POST",
    "create": "POST",
    "progress": "GET",
    "finish": "POST",
    "cancel": "POST",
}


def refusal(code, status):
    return JSONResponse(
        {"error": {"code": code, "message": code}}, status_code=status, headers={"Cache-Control": "no-store"}
    )


class SetupConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=15)
        self.path = Path(path)

    def connect(self):
        info = self.path.lstat()
        parent = self.path.parent.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077
        ):
            raise OSError("unsafe setup socket")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(str(self.path))
            check_uid(self.sock, os.getuid())
        except Exception:
            self.close()
            raise


class SetupProxy:
    def __init__(self, path):
        self.path = Path(path)
        self.claim_lock = threading.Lock()

    def claim(self, port):
        # Serialize read/delete/claim: only one browser receives the setup credential.
        with self.claim_lock:
            path = self.path.parent / "setup.code"
            parent = path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                raise OSError("unsafe setup directory")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                    raise OSError("unsafe setup code")
                path.unlink()
                with os.fdopen(fd, "rb", closefd=False) as source:
                    data = source.read(4097)
                if len(data) > 4096:
                    raise ValueError("invalid setup code")
                value = json.loads(data)
                if (
                    not isinstance(value.get("code"), str)
                    or not isinstance(value.get("setup_id"), str)
                    or type(value.get("expires_at")) not in (int, float)
                    or value["expires_at"] <= time.time()
                ):
                    raise ValueError("expired setup code")
                conn, reply = self.open(
                    "POST",
                    "/claim",
                    {"Content-Type": "application/json"},
                    json.dumps({"code": value["code"], "port": port}).encode(),
                )
                try:
                    data = reply.read(4097)
                    if len(data) > 4096:
                        raise ValueError("invalid claim response")
                    result = json.loads(data)
                    if reply.status == 200 and result.get("setup_id") != value["setup_id"]:
                        raise ValueError("wrong setup session")
                    return reply.status, result
                finally:
                    conn.close()
            finally:
                os.close(fd)

    def open(self, method, target, headers, body):
        conn = SetupConnection(self.path)
        try:
            conn.request(method, target, body=body, headers=headers)
            reply = conn.getresponse()
            return conn, reply
        except Exception:
            conn.close()
            raise


def register(app, auth, proxy, port):
    async def body_bytes(request):
        body = bytearray()
        try:
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise ValueError("too large")
            return body
        except BaseException:
            body[:] = b"\0" * len(body)
            body.clear()
            raise

    @app.post("/auth/setup")
    async def claim(request: Request):
        if os.environ.get("ORCH_GRANT"):
            return refusal("setup.agent", 403)
        body = await body_bytes(request)
        try:
            # No browser-supplied launch material, including query parameters.
            if request.url.query or json.loads(body or b"{}") != {}:
                return refusal("invalid.input", 400)
            status, value = await asyncio.to_thread(proxy.claim, port)
            if status == 200:
                if not isinstance(value.get("token"), str) or not isinstance(value.get("setup_id"), str):
                    return refusal("setup.unavailable", 502)
                auth.bind_setup(value["token"], value["setup_id"])
            return JSONResponse(value, status_code=status)
        except FileNotFoundError:
            return refusal("setup.code_invalid", 403)
        except ValueError:
            return refusal("setup.code_invalid", 403)
        except Exception:
            return refusal("setup.unavailable", 503)
        finally:
            body[:] = b"\0" * len(body)
            body.clear()

    @app.api_route("/api/setup/{route:path}", methods=["GET", "POST"])
    async def forward(route: str, request: Request):
        if ROUTES.get(route) != request.method:
            return refusal("setup.not_found", 404)
        # Only progress has a query, and it contains only an opaque run id. Never forward arbitrary request targets.
        query = request.url.query
        if query and (
            route != "progress"
            or set(request.query_params) != {"run_id"}
            or len(request.query_params.getlist("run_id")) != 1
        ):
            return refusal("invalid.input", 400)
        if os.environ.get("ORCH_GRANT"):
            return refusal("setup.agent", 403)
        body = await body_bytes(request)
        conn = None
        try:
            headers = {
                key: request.headers[key]
                for key in ("host", "origin", "content-type", "x-setup-token")
                if key in request.headers
            }
            conn, reply = await asyncio.to_thread(
                proxy.open, request.method, "/api/setup/" + route + ("?" + query if query else ""), headers, body
            )
        except Exception:
            if conn:
                conn.close()
            return refusal("setup.unavailable", 503)
        finally:
            body[:] = b"\0" * len(body)
            body.clear()
        if reply.status in (401, 410) or (route in ("cancel", "finish") and reply.status == 200):
            auth.forget_setup(request.state.token)

        async def chunks():
            try:
                while chunk := await asyncio.to_thread(reply.read, 8192):
                    yield chunk
            finally:
                reply.close()
                conn.close()

        return StreamingResponse(
            chunks(), status_code=reply.status, media_type="application/json", headers={"Cache-Control": "no-store"}
        )

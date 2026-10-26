"""Plain-HTTP transport of :class:`~orch_hub.webapi.api.WebApi` on a loopback address (the same rule as the link
listener, ``serve.parse_listen``). No TLS here: the edge terminates it. Requests are read with hard limits, answers are
never cached, and nothing about a request (path, headers, body) is logged."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .api import MAX_BODY, WebApi


class _Handler(BaseHTTPRequestHandler):
    api: WebApi
    timeout = 10  # a stalled client frees its thread
    server_version = "orch-hub"
    sys_version = ""

    def log_message(self, *_a: Any) -> None:  # tokens and ids stay out of every log
        return

    def _serve(self) -> None:
        headers = {k.lower(): v for k, v in self.headers.items()}
        body = b""
        if "transfer-encoding" in headers:
            return self._send(*self.api.error(400, "malformed"))
        try:
            n = int(headers.get("content-length", "0") or "0")
        except ValueError:
            return self._send(*self.api.error(400, "malformed"))
        if n < 0 or n > MAX_BODY:
            return self._send(*self.api.error(413, "too_large"))
        if n:
            body = self.rfile.read(n)
            if len(body) != n:
                return self._send(*self.api.error(400, "malformed"))
        self._send(*self.api.handle(self.command, self.path, headers, body, self.client_address[0]))

    def _send(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _serve


MAX_WORKERS = 32  # connections served at once; more are answered 503 and closed, never queued without bound


class _BoundedServer(ThreadingHTTPServer):
    """One thread per connection like the stdlib, but at most ``max_workers`` at a time: the (max_workers+1)th
    connection gets a bare 503 on the accept thread and is closed."""

    daemon_threads = True

    def __init__(self, addr: Any, handler: Any, max_workers: int) -> None:
        super().__init__(addr, handler)
        self._slots = threading.BoundedSemaphore(max_workers)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class WebServer:
    def __init__(self, api: WebApi, host: str, port: int, max_workers: int = MAX_WORKERS) -> None:
        handler = type("Handler", (_Handler,), {"api": api})
        self._srv = _BoundedServer((host, port), handler, max_workers)
        self._thread: threading.Thread | None = None

    @classmethod
    def from_listen(cls, api: WebApi, listen: str) -> WebServer:
        """Bind like the link listener: ``host:port`` and loopback only (``serve.parse_listen``); the edge fronts it."""
        from ..serve import parse_web_listen

        host, port = parse_web_listen(listen)
        return cls(api, host, port)

    @property
    def address(self) -> tuple[str, int]:
        return self._srv.server_address[0], self._srv.server_address[1]  # type: ignore[return-value]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._srv.serve_forever, name="orch-hub-web", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        if self._thread:
            self._thread.join(5)

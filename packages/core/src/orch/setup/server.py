"""The setup process's socket server: HTTP/1.1 over ``<state>/setup/setup.sock`` (0600, in a 0700 directory).

* Socket-only routes (the CLI and the dashboard server, never a browser): ``POST /launch {dir, port?}`` ->
  ``{setup_id, code}``, ``POST /claim {code, port}`` -> ``{token, setup_id}``, ``POST /shutdown``.
* Browser routes ``/api/setup/*`` (proxied unchanged): the ``X-Setup-Token`` header the claim returned, the ``Host``
  header named explicitly (127.0.0.1 or localhost, with the dashboard's port), on a POST the ``Origin`` equal to
  ``http://<Host>`` and a JSON content type. Every answer is ``Cache-Control: no-store``.

Session binding: the launch code is single use, lives 5 min and is bound to the ``setup_id``; the first claim wins, a
second one is refused (409) and reported to the terminal. The code never travels in a URL or an argument: it is written
to ``<state>/setup/setup.code`` (0600, in the 0700 setup directory), which the dashboard server reads and deletes,
and claimed over this socket together with the dashboard's own port; the browser is bound to that port
(``Host``/``Origin``), and no browser request is answered before a claim named one. The session dies on done, cancel
or 10 minutes without a request, never while a run is in progress (the flow says so through ``busy``).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import socketserver
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Protocol

from .protocol import BROWSER_PREFIX, MAX_BODY, SOCKET_ROUTES, SetupError

__all__ = ["CODE_SECONDS", "IDLE_SECONDS", "LOST_SECONDS", "LOST_LINE", "SetupServer"]

CODE_SECONDS = 300.0  # 5 min: the person may copy the printed link elsewhere; still single use
IDLE_SECONDS = 600.0
LOST_SECONDS = 150.0  # K2 fallback for a crashed tab: well above the ~60 s a background tab is throttled to
LOST_LINE = "The setup page was closed or reloaded. Press Enter to open a fresh setup link, or Ctrl-C to stop."
CODE_TRIES = 5
_HOST = re.compile(r"(?:127\.0\.0\.1|localhost):([0-9]{1,5})")


class Flow(Protocol):
    """What the server needs from the flow."""

    def launch(self, directory: str) -> None: ...
    @property
    def dir(self) -> str: ...
    def dispatch(self, method: str, route: str, query: dict[str, str], body: Any) -> tuple[int, dict[str, Any]]: ...
    @property
    def busy(self) -> bool: ...
    @property
    def finished(self) -> bool: ...
    def end(self, why: str) -> None: ...
    @property
    def outcome(self) -> str: ...


class SetupServer:
    def __init__(
        self,
        sock_path: Path,
        flow: Flow,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_seconds: float = IDLE_SECONDS,
        code_seconds: float = CODE_SECONDS,
        lost_seconds: float = LOST_SECONDS,
        notify: Callable[[str], None] = lambda line: None,
    ) -> None:
        self.path = Path(sock_path)
        self.flow = flow
        self.clock = clock
        self.idle_seconds, self.code_seconds = idle_seconds, code_seconds
        self.lost_seconds = lost_seconds
        self._seen = 0.0  # the last request the bound browser made (a poll counts), unlike ``_last``
        self.lost = False  # K2: the bound browser went quiet and the terminal was told
        self.notify = notify
        self._lock = threading.Lock()
        self.setup_id: str | None = None
        self._code_hash: bytes | None = None
        self._code_until = 0.0
        self._tries = 0
        self._token: bytes | None = None
        self._last = 0.0
        self.ports: frozenset[str] | None = None  # the dashboard's host names (host:port), set by the claim
        self.launch_port: int | None = None
        self.ended: str | None = None
        self.stop_event = threading.Event()
        self._srv: socketserver.BaseServer | None = None
        self._thread: threading.Thread | None = None
        self._watch: threading.Thread | None = None
        self._inflight = 0  # requests being handled or answered; stop() waits for them (#949)
        self._idle = threading.Condition()

    # ------------------------------------------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = parent.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise SetupError("setup.socket_unsafe", "the setup directory is not private to this user", 500)
        with contextlib.suppress(FileNotFoundError):
            if self.path.is_socket():
                self.path.unlink()
            elif os.path.lexists(self.path):
                raise SetupError("setup.socket_unsafe", "setup.sock exists and is not a socket", 500)
        server = _Server(str(self.path), _make_handler(self))
        os.chmod(self.path, 0o600)
        self._srv = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        self._watch = threading.Thread(target=self._watchdog, daemon=True)
        self._watch.start()

    def request_started(self) -> None:
        with self._idle:
            self._inflight += 1

    def request_done(self) -> None:
        with self._idle:
            self._inflight -= 1
            self._idle.notify_all()

    def stop(self) -> None:
        self.stop_event.set()
        self._remove_code_file()
        # The request that ended the session (finish, shutdown) calls die() before its response is written, and the
        # command's main loop wakes on stop_event. Let that answer go out before the listener closes and the process
        # can exit (handler threads are daemons); bounded, so a stuck peer cannot hold the terminal.
        with self._idle:
            self._idle.wait_for(lambda: self._inflight == 0, timeout=5.0)
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        with contextlib.suppress(OSError):
            self.path.unlink()

    def _watchdog(self) -> None:
        while not self.stop_event.wait(0.25):
            self.check_idle()

    def check_idle(self) -> None:
        with self._lock:
            if self._code_hash is not None and self.clock() >= self._code_until:
                self._code_hash = None
                self._remove_code_file()
            if self._token is None or self.ended or self.flow.busy:
                return
            now = self.clock()
            if now - self._last >= self.idle_seconds:
                idle, tell = True, False
            else:
                idle = False
                tell = not self.lost and now - self._seen > self.lost_seconds
                self.lost = self.lost or tell
        if idle:
            self.die("idle")
        elif tell:
            self.notify(LOST_LINE)

    def page_left(self) -> None:
        """K2: the bound page announced that it is closing or reloading (``POST /api/setup/left``, which passed the same
        Host, Origin and token checks as every browser POST). Hiding a tab is not leaving; a run in progress goes on."""
        with self._lock:
            if self.lost or self._token is None or self.ended or self.flow.busy:
                return
            self.lost = True
        self.notify(LOST_LINE)

    def relink(self) -> dict[str, Any] | None:
        """K2: the bound browser is gone (closed, reloaded: its token lived only in that page). Mint a NEW single-use
        link: the old token stops working, the flow starts over (secrets already shown are dropped, so new words are
        drawn and never shown again to anyone), then a fresh launch code. ``None`` when there is nothing to replace
        (no lost browser, a run in progress, an ended session). Checked and swapped inside the lock (invariant 4)."""
        with self._lock:
            if not self.lost or self._token is None or self.ended or self.flow.busy:
                return None
            self._token, self.ports, self.lost = None, None, False  # the old page is refused from here on
            directory = self.flow.dir
        return self.launch(directory)

    def die(self, why: str) -> None:
        with self._lock:
            if self.ended:
                return
            self.ended, self._token, self._code_hash = why, None, None
        self._remove_code_file()
        self.flow.end(why)
        line = self.ending_line(why)
        if line:
            self.notify(line)
        self.stop_event.set()

    def ending_line(self, why: str) -> str:
        """The last line on the setup terminal. Only a finished setup says so; every other end (Finish setup later,
        Ctrl-C, hang-up, idle, an error) reads as paused, never as "(done)" (B11 finding 25). Once the workspace is set
        up there is nothing paused (B12 N8): no line here, ``orch setup`` prints its one closing line."""
        if getattr(self.flow, "ready", False) or getattr(self.flow, "outcome", "paused") == "done":
            return ""
        return "setup paused; run orch setup again to continue"

    # ---------------------------------------------------------------------------------------- socket-only routes
    def _launch(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or not isinstance(body.get("dir"), str):
            raise SetupError("invalid.input", "launch takes dir")
        return self.launch(body["dir"], body.get("port"))

    def launch(self, directory: str, port: Any = None) -> dict[str, Any]:
        """Check the directory (the flow refuses), then mint the single-use code bound to a new ``setup_id``."""
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise SetupError("invalid.input", "port")
        with self._lock:
            if self._token is not None and not self.ended:
                raise SetupError("setup.claimed", "a browser is already connected to this setup", 409)
        self.flow.launch(directory)  # raises its own refusals (setup.dir_refused ...)
        code = secrets.token_urlsafe(24)
        with self._lock:
            self.setup_id = "su_" + secrets.token_hex(8)
            self._code_hash = hashlib.sha256(code.encode()).digest()
            self._code_until = self.clock() + self.code_seconds
            self._tries = 0
            self.ports = None
            self.launch_port = port
            self._write_code_file(code)
            return {"setup_id": self.setup_id, "code": code}

    # -- the code file: the one way the code reaches the dashboard server (never a URL, never an argument)
    @property
    def code_path(self) -> Path:
        return self.path.parent / "setup.code"

    def _write_code_file(self, code: str) -> None:
        self._remove_code_file()
        doc = json.dumps({"setup_id": self.setup_id, "code": code, "expires_at": time.time() + self.code_seconds})
        fd = os.open(self.code_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, doc.encode())
        finally:
            os.close(fd)

    def _remove_code_file(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.code_path.unlink()

    def _claim(self, body: Any) -> dict[str, Any]:
        code = body.get("code") if isinstance(body, dict) else None
        port = body.get("port") if isinstance(body, dict) else None
        if not isinstance(code, str) or len(code) > 200:
            raise SetupError("invalid.input", "claim takes code")
        if type(port) is not int or not 1 <= port <= 65535:
            raise SetupError("invalid.input", "claim takes the dashboard's port")
        with self._lock:
            if self.ended:
                raise SetupError("setup.ended", "this setup has ended", 410)
            if self._token is not None:
                self.notify("a second browser tried to connect to this setup and was refused")
                raise SetupError("setup.claimed", "a browser is already connected to this setup", 409)
            if self._code_hash is None or self.clock() >= self._code_until:
                self._remove_code_file()
                raise SetupError("setup.code_invalid", "the launch code expired or was used", 403)
            if self.launch_port is not None and port != self.launch_port:
                raise SetupError("setup.code_invalid", "the launch code is for another dashboard port", 403)
            if not hmac.compare_digest(hashlib.sha256(code.encode()).digest(), self._code_hash):
                self._tries += 1
                if self._tries >= CODE_TRIES:
                    self._code_hash = None
                    self._remove_code_file()
                raise SetupError("setup.code_invalid", "the launch code is wrong", 403)
            self._code_hash = None  # single use
            self._remove_code_file()
            self.ports = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
            token = secrets.token_urlsafe(32)
            self._token = hashlib.sha256(token.encode()).digest()
            self._last = self._seen = self.clock()
            self.lost = False
        self.notify("browser connected")
        return {"token": token, "setup_id": self.setup_id}

    # ------------------------------------------------------------------------------------------ browser routes
    def _check_browser(self, method: str, headers: Any, *, passive: bool = False, leaving: bool = False) -> None:
        host = headers.get("Host", "")
        m = _HOST.fullmatch(host)
        if m is None or int(m.group(1)) > 65535 or self.ports is None or host not in self.ports:
            raise SetupError("setup.host", "wrong Host header", 403)
        if method == "POST":
            if headers.get("Origin") != f"http://{host}":
                raise SetupError("setup.origin", "wrong Origin header", 403)
            if headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise SetupError("setup.content_type", "JSON bodies only", 415)
        given = headers.get("X-Setup-Token", "")
        with self._lock:
            if self.ended:
                raise SetupError("setup.ended", "this setup has ended", 410)
            ok = (
                self._token is not None
                and len(given) <= 200
                and hmac.compare_digest(hashlib.sha256(given.encode()).digest(), self._token)
            )
            if not ok:
                raise SetupError("setup.token", "missing or wrong setup token", 401)
            back = self.lost and not leaving
            if not leaving:
                self._seen, self.lost = self.clock(), False
            if not passive:  # the page's own once-a-second GET state must not keep an abandoned session alive
                self._last = self.clock()
        if back:
            self.notify("the setup page is back; carry on there")

    def handle(self, method: str, target: str, headers: Any, raw: bytes) -> tuple[int, dict[str, Any]]:
        """One request -> (status, JSON-able body). Never raises."""
        try:
            path, _, qs = target.partition("?")
            query = dict(p.partition("=")[::2] for p in qs.split("&") if p)
            if method not in ("GET", "POST"):
                raise SetupError("setup.method", "GET or POST", 405)
            body: Any = None
            if method == "POST":
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, ValueError):
                    raise SetupError("invalid.input", "the body is not JSON") from None
            if path in SOCKET_ROUTES:
                if method != "POST":
                    raise SetupError("setup.method", "POST", 405)
                if path == "/launch":
                    return 200, self._launch(body)
                if path == "/claim":
                    return 200, self._claim(body)
                self.die("shutdown")
                return 200, {"ok": True}
            if not path.startswith(BROWSER_PREFIX):
                raise SetupError("setup.not_found", "not found", 404)
            self._check_browser(
                method,
                headers,
                passive=(method == "GET" and path == BROWSER_PREFIX + "state") or path == BROWSER_PREFIX + "left",
                leaving=path == BROWSER_PREFIX + "left",
            )
            if path == BROWSER_PREFIX + "left":  # K2: the page said it is going away (pagehide), with the valid token
                if method != "POST":
                    raise SetupError("setup.method", "POST", 405)
                self.page_left()
                return 200, {"ok": True}
            status, out = self.flow.dispatch(method, path[len(BROWSER_PREFIX) :], query, body)
            if self.flow.finished:
                self.die("done")
            return status, out
        except SetupError as e:
            return e.status, {"error": {"code": e.code, "message": e.message, **e.extra}}
        except Exception as e:  # noqa: BLE001 - never a traceback or a value to the browser
            self.notify(f"a setup step failed ({type(e).__name__}); try again on the page")  # the class only, no value
            return 500, {"error": {"code": "internal", "message": "setup failed"}}


if hasattr(socketserver, "UnixStreamServer"):
    _UnixBase: type = socketserver.UnixStreamServer
else:  # pragma: no cover - Windows has no UnixStreamServer; the setup server is a later step of the port

    class _UnixBase(socketserver.BaseServer):  # type: ignore[no-redef]
        def __init__(self, *_a: object, **_k: object) -> None:
            raise NotImplementedError("the setup server is not supported on Windows yet")


class _Server(socketserver.ThreadingMixIn, _UnixBase):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self) -> None:
        old = os.umask(0o177)  # the socket is created 0600, never briefly wider
        try:
            super().server_bind()
        finally:
            os.umask(old)


def _make_handler(owner: SetupServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 10.0
        server_version = "orch-setup"
        sys_version = ""

        def log_message(self, *args: Any) -> None:  # no request line, header or body ever reaches a log
            return

        def address_string(self) -> str:
            return "unix"

        def _go(self) -> None:
            owner.request_started()
            try:
                self._serve()
            finally:
                owner.request_done()

        def _serve(self) -> None:
            length = self.headers.get("Content-Length", "0")
            if not length.isdigit() or int(length) > MAX_BODY or self.headers.get("Transfer-Encoding"):
                status, body = 413, {"error": {"code": "setup.too_large", "message": "request too large"}}
                raw = b""
                self.close_connection = True
            else:
                raw = self.rfile.read(int(length)) if int(length) else b""
                status, body = owner.handle(self.command, self.path, self.headers, raw)
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _go

    return Handler

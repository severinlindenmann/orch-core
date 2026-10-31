"""The /h/ listener: its own socket, its own small worker pool, nothing shared with the admin web API.

* one request is `POST /h/<token>` (no query); anything else is the uniform 404;
* a TOTAL time limit for head and body together (``total_s``, a monotonic deadline set when the connection is taken, not a
  per-read timeout: a slow sender cannot hold a worker longer than that);
* the head is at most ``MAX_HEAD`` bytes; ``Content-Length`` is required and checked against the cap BEFORE any body
  byte is read (``413``); chunked bodies are refused;
* only the headers in :data:`KEEP` are kept; a ``Cookie`` is dropped on parse and never reaches any code, and no session,
  channel or bearer lookup exists in this module (``Authorization`` is the hook's own Databricks Basic credential, checked
  against that hook's secret by the inbox and nothing else);
* the reply is a status line with an empty body, ``Connection: close``; the request line, headers and body are never logged.
"""

from __future__ import annotations

import queue
import re
import socket
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from .. import forwarded as FW
from ..contract import hooks as HK
from . import inbox as IB

MAX_HEAD = 8 * 1024
WORKERS = 8
BACKLOG = 16  # accepted connections waiting for a worker; more are closed at once
TOTAL_S = 10.0
KEEP = frozenset(
    {"x-hub-signature-256", "x-orch-signature", "x-github-event", "authorization", "x-forwarded-for", "content-length"}
)
_LINE = re.compile(rb"POST /h/(hh_[A-Za-z0-9_-]{43}) HTTP/1\.[01]")
_REASON = {204: "No Content", 400: "Bad Request", 404: "Not Found", 411: "Length Required", 413: "Content Too Large"}
_REASON |= {429: "Too Many Requests", 503: "Service Unavailable"}


def parse_public_url(value: str, *, dev: bool) -> str:
    """``--public-hooks-url`` -> the ``url_base`` (``<origin>/h/``). https origin only; http only for a dev hub."""
    u = urlsplit(value)
    ok_scheme = u.scheme == "https" or (dev and u.scheme == "http")
    if not ok_scheme or not u.hostname or u.path not in ("", "/") or u.query or u.fragment or u.username:
        raise ValueError("--public-hooks-url must be an origin: https://host[:port] (http only for a dev hub)")
    return f"{u.scheme}://{u.netloc}/h/"


class HookListener:
    def __init__(
        self,
        inbox: IB.HookInbox,
        listen: str,
        *,
        trusted_peers: frozenset[str] = frozenset(),
        total_s: float = TOTAL_S,
        workers: int = WORKERS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        host, _, port = listen.rpartition(":")
        self._addr = (host.strip("[]") or "127.0.0.1", int(port))
        self.inbox, self._trusted, self._total, self._clock = inbox, trusted_peers, total_s, clock
        self._q: queue.Queue[tuple[socket.socket, str] | None] = queue.Queue(maxsize=BACKLOG)
        self._workers = workers
        self._threads: list[threading.Thread] = []
        self._sock: socket.socket | None = None
        self.address: tuple[str, int] = self._addr

    def start(self) -> None:
        s = socket.socket(socket.AF_INET6 if ":" in self._addr[0] else socket.AF_INET)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(self._addr)
        s.listen(32)
        self._sock, self.address = s, s.getsockname()[:2]
        for _ in range(self._workers):
            self._threads.append(threading.Thread(target=self._work, daemon=True, name="hooks-worker"))
        self._threads.append(threading.Thread(target=self._accept, daemon=True, name="hooks-accept"))
        for t in self._threads:
            t.start()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        for _ in range(self._workers):
            try:
                self._q.put_nowait(None)
            except queue.Full:
                break

    def _accept(self) -> None:
        while (s := self._sock) is not None:
            try:
                conn, peer = s.accept()
            except OSError:
                return
            try:
                self._q.put_nowait((conn, peer[0]))
            except queue.Full:  # every worker busy and the queue full: refuse at once, read nothing
                self._reply(conn, 503)

    def _work(self) -> None:
        while (item := self._q.get()) is not None:
            conn, peer = item
            try:
                self._reply(conn, self._serve(conn, peer))
            except Exception:  # noqa: BLE001 - nothing of the request is ever logged
                self._reply(conn, 404)

    @staticmethod
    def _reply(conn: socket.socket, status: int) -> None:
        try:
            conn.settimeout(1.0)
            conn.sendall(
                f"HTTP/1.1 {status} {_REASON[status]}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
            )
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read(self, conn: socket.socket, deadline: float, n: int) -> bytes:
        left = deadline - self._clock()
        if left <= 0:
            raise TimeoutError
        conn.settimeout(left)  # the remaining TOTAL time, not a fresh per-read timeout
        return conn.recv(n)

    def _serve(self, conn: socket.socket, peer: str) -> int:
        deadline = self._clock() + self._total
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > MAX_HEAD:
                return 404
            chunk = self._read(conn, deadline, 4096)
            if not chunk:
                return 404
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        m = _LINE.fullmatch(lines[0])
        headers: dict[str, str] = {}
        chunked = False
        for ln in lines[1:]:
            k, sep, v = ln.partition(b":")
            name = k.strip().lower().decode("latin-1")
            if sep and name == "transfer-encoding":
                chunked = True
            if sep and FW.refuse((name,), peer, self._trusted):
                return 400  # not served through a proxy (forwarded.py)
            if sep and name in KEEP:
                headers[name] = v.strip().decode("latin-1")
        if m is None:
            return 404
        if chunked:
            return 400
        try:
            n = int(headers["content-length"]) if re.fullmatch(r"[0-9]{1,9}", headers.get("content-length", "")) else -1
        except ValueError:
            n = -1
        if n < 0:
            return 411
        if n > HK.BODY_CAP:
            return IB.TOO_LARGE  # before the lookup and before any body byte
        source = FW.source_of(headers, peer, self._trusted)
        if not self.inbox.enabled or not self.inbox.admit(source):
            return IB.TOO_MANY if self.inbox.enabled else 404
        hook = self.inbox.find(m.group(1).decode("ascii"), source)
        if hook is None:
            return IB.NOT_FOUND  # unknown token: the body is never read
        body = rest[:n]
        while len(body) < n:
            chunk = self._read(conn, deadline, min(16 * 1024, n - len(body)))
            if not chunk:
                return IB.NOT_FOUND
            body += chunk
        return self.inbox.finish(hook, headers, body, source)

"""Status of the company's remote MCP servers, seen from the hub (M3 #1439, Opus condition 9, host design 6.5).

The hub asks each server one plain question and keeps one word of the answer. It never sends a credential (no
Authorization, no cookie, no secret), never follows a redirect, uses only HEAD (GET with ``Range: bytes=0-0`` when HEAD
answers 405), gives each probe 3 seconds in all, and reads at most 4 KB of the answer, which it throws away. The address
comes from the stored, already validated server record, never from a request. The same rule as at the host (design 6.5):
the name is resolved on every probe and EVERY address must be a global unicast address; the probe then connects to that
address (pinned, so a later lookup cannot swap it) and checks the certificate against the name.

What the answer means (a decision, v1): 2xx, 404, 405 (after the GET fallback) and 406 = the server is there
(``reachable``; 404/405/406 only say the bare address is not a page, which is normal for an MCP endpoint); 401 and 403 =
``needs_sign_in`` (it is there and wants a sign-in, which the probe will not give); anything else (other codes, 3xx,
5xx, DNS, refused, TLS, timeout, an address that is not public) = ``failing``. The ``detail`` is one word from a fixed
list, never text from the server. Use counts stay at each workspace for now (#1263)."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from .store import Access, AccessError

TIMEOUT_S = 3.0
READ_CAP = 4096
MAX_PARALLEL = 4
CHECK_GAP_MS = 30_000  # "Check now": at most once per 30 s per server
STALE_MS = 10 * 60_000  # opening the page re-checks a server last checked longer ago than this

_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_BAD6 = (*_NAT64, ipaddress.ip_network("2002::/16"), ipaddress.ip_network("2001::/32"))  # 6to4, Teredo
_GLOBAL6 = ipaddress.ip_network("2000::/3")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_REACHABLE = frozenset({404, 405, 406})


def global_unicast(addr: str) -> bool:
    """True only for an address on the public internet (IPv4-mapped IPv6 is judged as the IPv4 inside it)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.scope_id is not None or ip not in _GLOBAL6 or any(ip in n for n in _BAD6):
            return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    return ip.is_global and not ip.is_multicast


class _Fail(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


_lookups = threading.BoundedSemaphore(8)  # running DNS lookups, hub-wide: a lookup cannot be interrupted


def _counted(fn: Callable[[], None]) -> None:
    try:
        fn()
    finally:
        _lookups.release()


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


def _connect(ip: str, port: int, server_hostname: str, timeout: float) -> Any:
    """A TLS connection to the pinned address, certificate and name verified (the default context, TLS 1.2+)."""
    raw = socket.create_connection((ip, port), timeout=timeout)
    try:
        return ssl.create_default_context().wrap_socket(raw, server_hostname=server_hostname)
    except BaseException:
        raw.close()
        raise


def _ask(conn: Any, method: str, host: str, target: str, deadline: float) -> int:
    """Send one request with no credential and read at most READ_CAP bytes; return the status code."""
    head = [
        f"{method} {target} HTTP/1.1",
        f"Host: {host}",
        "User-Agent: orch-hub-probe",
        "Accept: */*",
        "Connection: close",
    ]
    if method == "GET":
        head.append("Range: bytes=0-0")
    conn.settimeout(max(0.05, deadline - time.monotonic()))
    conn.sendall(("\r\n".join(head) + "\r\n\r\n").encode("ascii"))
    buf = b""
    while len(buf) < READ_CAP and b"\r\n" not in buf:
        if time.monotonic() >= deadline:
            raise _Fail("timeout")
        conn.settimeout(max(0.05, deadline - time.monotonic()))
        chunk = conn.recv(min(1024, READ_CAP - len(buf)))
        if not chunk:
            break
        buf += chunk
    parts = buf.split(b"\r\n", 1)[0].split(b" ")
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/1.") or not parts[1].isdigit() or len(parts[1]) != 3:
        raise _Fail("bad_response")
    return int(parts[1])  # the rest of the answer is never read


def _classify(code: int) -> tuple[str, str]:
    if 200 <= code < 300:
        return "reachable", "ok"
    if code in (401, 403):
        return "needs_sign_in", "sign_in"
    if code in _REACHABLE:
        return "reachable", "ok"
    return "failing", "redirect" if 300 <= code < 400 else "http_error"


def probe(
    url: str,
    *,
    _resolve: Callable[[str, int], list[str]] = _resolve,
    _connect: Callable[[str, int, str, float], Any] = _connect,
    _clock: Callable[[], int] = lambda: int(time.time() * 1000),
) -> dict[str, Any]:
    """One probe: ``{status, checked_ms, detail}``. Never raises, never returns text from the server."""
    try:
        status, detail = _run(url, _resolve, _connect)
    except _Fail as e:
        status, detail = "failing", e.detail
    except ssl.SSLError, ssl.CertificateError:
        status, detail = "failing", "tls"
    except TimeoutError:
        status, detail = "failing", "timeout"
    except OSError:
        status, detail = "failing", "refused"
    except Exception:  # noqa: BLE001 - a probe must never break the page; the word says only "not working"
        status, detail = "failing", "error"
    return {"status": status, "checked_ms": _clock(), "detail": detail}


def _run(url: str, resolve: Callable, connect: Callable) -> tuple[str, str]:
    deadline = time.monotonic() + TIMEOUT_S
    u = urllib.parse.urlsplit(url)
    host = u.hostname
    if u.scheme != "https" or not host or u.username or u.password or u.fragment:
        raise _Fail("scheme")
    port = u.port or 443
    target = (u.path or "/") + ("?" + u.query if u.query else "")
    box: dict[str, Any] = {}

    def look() -> None:
        try:
            box["ips"] = resolve(host, port)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    if not _lookups.acquire(blocking=False):
        raise _Fail("busy")  # too many lookups are stuck already; do not start another thread
    t = threading.Thread(target=_counted, args=(look,), daemon=True)
    t.start()
    t.join(TIMEOUT_S)
    if t.is_alive():
        raise _Fail("timeout")
    if "err" in box:
        raise _Fail("dns")
    ips = box["ips"]
    if not ips:
        raise _Fail("dns")
    if not all(global_unicast(ip) for ip in ips):
        raise _Fail("address")  # one private address among public ones is already too many
    code = _once(connect, ips[0], port, host, "HEAD", target, deadline)
    if code == 405:
        code = _once(connect, ips[0], port, host, "GET", target, deadline)
    return _classify(code)


def _once(connect: Callable, ip: str, port: int, host: str, method: str, target: str, deadline: float) -> int:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _Fail("timeout")
    conn = connect(ip, port, host, left)
    try:
        return _ask(conn, method, host if port == 443 else f"{host}:{port}", target, deadline)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110 - a failed close changes nothing
            pass


class McpProbes:
    """The last result per server (in memory: it is a hint that is cheap to redo), the rate rules and the global cap."""

    def __init__(
        self, *, now_ms: Callable[[], int] = lambda: int(time.time() * 1000), run: Callable | None = None
    ) -> None:
        self._now, self._run = now_ms, run
        self._last: dict[str, dict[str, Any]] = {}
        self._asked: dict[str, int] = {}  # name -> when a probe last started
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_PARALLEL)

    def status(self, name: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._last.get(name) or {"status": "not_checked", "checked_ms": None, "detail": "none"})

    def _do(self, name: str, url: str) -> dict[str, Any]:
        fn = self._run or probe  # looked up late so a test can replace ``probe``
        res = fn(url)
        with self._lock:
            self._last[name] = res
        return dict(res)

    def check_now(self, name: str, url: str) -> dict[str, Any]:
        """The admin's button: at most once per 30 s per server, and never more than 4 probes at once hub-wide."""
        with self._lock:
            now = self._now()
            if now - self._asked.get(name, -CHECK_GAP_MS) < CHECK_GAP_MS:
                raise AccessError("rate_limited", "checked a moment ago")
            if not self._slots.acquire(blocking=False):
                raise AccessError("rate_limited", "busy")
            self._asked[name] = now
        try:
            return self._do(name, url)
        finally:
            self._slots.release()

    def refresh_stale(self, servers: list[dict[str, Any]]) -> None:
        """Opening the page: start (in the background) a check for each server not checked within 10 minutes."""
        for s in servers:
            name, url = s["name"], s["url"]
            with self._lock:
                now = self._now()
                last = (self._last.get(name) or {}).get("checked_ms")
                if (last is not None and now - last < STALE_MS) or now - self._asked.get(
                    name, -STALE_MS
                ) < CHECK_GAP_MS:
                    continue
                if not self._slots.acquire(blocking=False):
                    return
                self._asked[name] = now
            threading.Thread(target=self._bg, args=(name, url), daemon=True).start()

    def _bg(self, name: str, url: str) -> None:
        try:
            self._do(name, url)
        finally:
            self._slots.release()

    def forget(self, name: str) -> None:
        with self._lock:
            self._last.pop(name, None)
            self._asked.pop(name, None)


def mcp_probes(a: Access) -> McpProbes:
    s = getattr(a, "_mcp_probes", None)
    if s is None:
        s = a._mcp_probes = McpProbes()  # type: ignore[attr-defined]
    return s

"""Upstream client of the company MCP proxy (H-4, Opus condition 10).

Talks MCP streamable HTTP (JSON-RPC 2.0 over ``POST``) to one company server. Security rules, all enforced here:

* ``https://`` only, a DNS name or an IP literal, no userinfo.
* The name is resolved fresh for every connection, and **every** resolved address must be a global unicast address
  (:func:`address_ok`). The connection is then pinned to one of those IPs: TCP to the IP, TLS with the system trust
  store, certificate and host name checked against the name, TLS 1.2 or newer.
* One connection per request (``Connection: close``), no pooling, no proxy from the environment, no redirects.
* Error bodies are never read into an exception; sizes and time are capped.

``_resolve`` and ``_connect`` are test seams and the only bypass: no env var, no setting.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
MAX_HEADERS = 64 * 1024
MAX_SESSION_ID = 200

_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_BLOCKED6 = (
    *_NAT64,
    ipaddress.ip_network("2002::/16"),  # 6to4
    ipaddress.ip_network("2001::/32"),  # Teredo
    ipaddress.ip_network("::/96"),  # IPv4-compatible (deprecated)
)
_GLOBAL6 = ipaddress.ip_network("2000::/3")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class UpstreamError(Exception):
    """A failure talking to the upstream. ``code`` is a fixed word; the text never carries upstream bytes."""

    CODES = ("address", "tls", "connect", "http", "redirect", "size", "timeout", "protocol")

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def address_ok(ip: str) -> bool:
    """True only for a global unicast address that is not a translation or tunnel prefix."""
    try:
        if "%" in ip:
            return False
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr not in _GLOBAL6 or any(addr in net for net in _BLOCKED6):  # IPv6 only inside 2000::/3
            return False
    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT:
        return False
    return bool(addr.is_global and not addr.is_multicast)


def _default_resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        raise UpstreamError("connect") from None
    out: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in out:
            out.append(ip)
    return out


def _make_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _default_connect(ip: str, port: int, host: str, timeout: float) -> Any:
    """TCP to the pinned IP, then TLS verified against the name (SNI and certificate)."""
    try:
        raw = socket.create_connection((ip, port), timeout=timeout)
    except TimeoutError:
        raise UpstreamError("timeout") from None
    except OSError:
        raise UpstreamError("connect") from None
    try:
        return _make_context().wrap_socket(raw, server_hostname=host)
    except TimeoutError:
        raw.close()
        raise UpstreamError("timeout") from None
    except (ssl.SSLError, ssl.CertificateError, OSError):
        raw.close()
        raise UpstreamError("tls") from None


def _split(url: str) -> tuple[str, int, str, str]:
    """(host, port, host header, path) of a checked https URL."""
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in url):
        raise UpstreamError("address")
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise UpstreamError("address") from None
    if parts.scheme != "https" or not host or "@" in parts.netloc or parts.fragment:
        raise UpstreamError("address")
    if port == 0:
        raise UpstreamError("address")
    port = port or 443
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if not path.startswith("/"):
        raise UpstreamError("address")
    shown = f"[{host}]" if ":" in host else host
    return host, port, shown if port == 443 else f"{shown}:{port}", path


class _Reader:
    """Buffered reads from a socket-like object with an overall deadline and a body cap."""

    def __init__(self, sock: Any, deadline: float) -> None:
        self.sock = sock
        self.deadline = deadline
        self.buf = b""
        self.eof = False
        self.total = 0

    def _fill(self) -> None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise UpstreamError("timeout")
        if hasattr(self.sock, "settimeout"):
            self.sock.settimeout(left)
        try:
            data = self.sock.recv(65536)
        except TimeoutError:
            raise UpstreamError("timeout") from None
        except (ssl.SSLError, OSError):
            raise UpstreamError("connect") from None
        if not data:
            self.eof = True
            return
        self.total += len(data)
        if self.total > MAX_RESPONSE + MAX_HEADERS:
            raise UpstreamError("size")
        self.buf += data

    def until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self.buf:
            if len(self.buf) > limit:
                raise UpstreamError("protocol")
            if self.eof:
                raise UpstreamError("protocol")
            self._fill()
        head, _, self.buf = self.buf.partition(marker)
        if len(head) > limit:
            raise UpstreamError("protocol")
        return head

    def take(self, n: int) -> bytes:
        while len(self.buf) < n:
            if self.eof:
                raise UpstreamError("protocol")
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def rest(self, limit: int) -> bytes:
        while not self.eof:
            if len(self.buf) > limit:
                raise UpstreamError("size")
            self._fill()
        if len(self.buf) > limit:
            raise UpstreamError("size")
        out, self.buf = self.buf, b""
        return out


def _read_chunked(rd: _Reader) -> bytes:
    out = bytearray()
    while True:
        line = rd.until(b"\r\n", 1024).split(b";", 1)[0].strip()
        try:
            size = int(line, 16)
        except ValueError:
            raise UpstreamError("protocol") from None
        if size < 0:
            raise UpstreamError("protocol")
        if size == 0:
            while rd.until(b"\r\n", MAX_HEADERS):  # trailers, up to the blank line
                pass
            return bytes(out)
        if len(out) + size > MAX_RESPONSE:
            raise UpstreamError("size")
        out += rd.take(size)
        if rd.take(2) != b"\r\n":
            raise UpstreamError("protocol")


def _json_objects(value: Any) -> list[dict]:
    items = value if isinstance(value, list) else [value]
    if not all(isinstance(i, dict) for i in items):
        raise UpstreamError("protocol")
    return items


def _parse_sse(text: str) -> list[dict]:
    out: list[dict] = []
    for event in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        data = []
        for line in event.split("\n"):
            if line.startswith("data:"):
                v = line[5:]
                data.append(v[1:] if v.startswith(" ") else v)
        if not data:
            continue
        try:
            out.extend(_json_objects(json.loads("\n".join(data))))
        except ValueError:
            raise UpstreamError("protocol") from None
    return out


class Upstream:
    """One company MCP server (never shared across servers). Blocking; the proxy calls it in a thread."""

    def __init__(
        self,
        url: str,
        bearer: str | None,
        *,
        _resolve: Callable[[str], list[str]] | None = None,
        _connect: Callable[[str, int, str, float], Any] | None = None,
    ) -> None:
        self._host, self._port, self._host_header, self._path = _split(url)
        self._bearer = bearer
        self._resolve = _resolve or _default_resolve
        self._connect = _connect or _default_connect
        self.session_id: str | None = None
        self.protocol_version: str | None = None

    def close(self) -> None:
        """Nothing is kept open; forget the upstream session."""
        self.session_id = None

    def _request(self, body: bytes) -> bytes:
        lines = [
            f"POST {self._path} HTTP/1.1",
            f"Host: {self._host_header}",
            "Connection: close",
            "Content-Type: application/json",
            "Accept: application/json, text/event-stream",
            f"Content-Length: {len(body)}",
        ]
        for name, value in (
            ("MCP-Protocol-Version", self.protocol_version),
            ("Mcp-Session-Id", self.session_id),
            ("Authorization", None if self._bearer is None else f"Bearer {self._bearer}"),
        ):
            if value is None:
                continue
            if not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise UpstreamError("protocol")
            lines.append(f"{name}: {value}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body

    def post(self, message: dict, *, timeout: float = 30.0) -> list[dict]:
        """POST one JSON-RPC message; return the JSON-RPC messages of the answer."""
        try:
            body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise UpstreamError("protocol") from None
        if len(body) > MAX_REQUEST:
            raise UpstreamError("size")
        data = self._request(body)
        deadline = time.monotonic() + timeout
        ips = self._resolve(self._host)
        if not ips or not all(address_ok(ip) for ip in ips):
            raise UpstreamError("address")
        sock = self._connect(ips[0], self._port, self._host, timeout)
        try:
            try:
                sock.sendall(data)
            except TimeoutError:
                raise UpstreamError("timeout") from None
            except OSError:
                raise UpstreamError("connect") from None
            return self._answer(_Reader(sock, deadline))
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _answer(self, rd: _Reader) -> list[dict]:
        head = rd.until(b"\r\n\r\n", MAX_HEADERS)
        try:
            lines = head.decode("latin-1").split("\r\n")
            status = int(lines[0].split(" ", 2)[1])
        except (ValueError, IndexError):
            raise UpstreamError("protocol") from None
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if not sep:
                raise UpstreamError("protocol")
            headers.setdefault(name.strip().lower(), value.strip())
        if 300 <= status < 400:
            raise UpstreamError("redirect")
        if not 200 <= status < 300:
            raise UpstreamError("http")
        sid = headers.get("mcp-session-id")
        if sid and len(sid) <= MAX_SESSION_ID and all(33 <= ord(c) <= 126 for c in sid):
            self.session_id = sid
        if "chunked" in headers.get("transfer-encoding", "").lower():
            raw = _read_chunked(rd)
        elif "content-length" in headers:
            try:
                n = int(headers["content-length"])
            except ValueError:
                raise UpstreamError("protocol") from None
            if n < 0:
                raise UpstreamError("protocol")
            if n > MAX_RESPONSE:
                raise UpstreamError("size")
            raw = rd.take(n)
        else:
            raw = rd.rest(MAX_RESPONSE)
        if not raw.strip():
            return []
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise UpstreamError("protocol") from None
        ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if ctype == "application/json":
            try:
                return _json_objects(json.loads(text))
            except ValueError:
                raise UpstreamError("protocol") from None
        if ctype == "text/event-stream":
            return _parse_sse(text)
        raise UpstreamError("protocol")

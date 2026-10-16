"""Test client for the setup socket (HTTP over a unix socket)."""

from __future__ import annotations

import http.client
import json
import socket
from typing import Any


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str, host: str = "127.0.0.1:8765") -> None:
        super().__init__(host, timeout=10)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self._path)


def call(
    path: str,
    method: str,
    route: str,
    body: Any = None,
    *,
    token: str | None = None,
    host: str = "127.0.0.1:8765",
    origin: str | None = "same",
    ctype: str = "application/json",
) -> tuple[int, dict[str, Any], Any]:
    conn = UnixConn(path, host)
    headers = {"Host": host}
    if token:
        headers["X-Setup-Token"] = token
    if method == "POST":
        headers["Content-Type"] = ctype
        if origin == "same":
            headers["Origin"] = f"http://{host}"
        elif origin:
            headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, route, body=data, headers=headers)
    res = conn.getresponse()
    raw = res.read()
    out = (res.status, json.loads(raw) if raw else {}, res.headers)
    conn.close()
    return out

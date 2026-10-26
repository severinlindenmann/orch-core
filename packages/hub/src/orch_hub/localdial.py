"""Dial this computer's own addresses over loopback.

A computer cannot connect to its own overlay (NetBird, utun) address, so a part on it that is reached by that address is
dialled on ``127.0.0.1``. Only the socket is redirected: Host headers, TLS names and signed origins stay as they are.
"""

from __future__ import annotations

import functools
import ipaddress
import socket
from typing import Any

__all__ = ["create_connection", "dial_host", "dialling_locally"]


@functools.lru_cache(maxsize=64)
def dial_host(host: str) -> str:
    """``127.0.0.1`` when ``host`` is an IPv4 address assigned to one of this computer's interfaces, else ``host``."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.version != 4 or ip.is_loopback:
        return host
    s = socket.socket(socket.AF_INET)
    try:
        s.bind((host, 0))  # succeeds only for an address of a local interface
    except OSError:
        return host
    finally:
        s.close()
    return "127.0.0.1"


def create_connection(address: tuple[str, int], timeout: Any = None, source_address: Any = None) -> socket.socket:
    return socket.create_connection((dial_host(address[0]), address[1]), timeout, source_address)


def dialling_locally(conn: Any) -> Any:
    """Make an ``http.client`` connection dial :func:`dial_host` of its host (Host header and TLS name unchanged)."""
    conn._create_connection = create_connection
    return conn

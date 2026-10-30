"""The ssh tunnel line for a headless machine (HUB-WS-07). One place in core builds it; the hub guide shows the same.

``ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:<port>:127.0.0.1:<port> USER@SERVER``

User and server are checked against closed patterns (ASCII, never a leading ``-``) so a name can never become an ssh
option or shell syntax; a name that fails (or is unknown) is replaced by the ``USER`` / ``SERVER`` placeholders (no angle brackets: a shell reads them as redirection)
instead of failing. The line carries no secret: the single-use launch link is shown on /dev/tty only.
"""

from __future__ import annotations

import getpass
import re
import socket
import sys
from collections.abc import Mapping

__all__ = ["is_headless", "tunnel_line", "valid_server", "valid_user"]

_USER = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,63}")
_SERVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_IPV6 = re.compile(r"[0-9A-Fa-f:]{2,39}")


def valid_user(value: object) -> bool:
    return isinstance(value, str) and _USER.fullmatch(value) is not None


def valid_server(value: object) -> bool:
    return isinstance(value, str) and (_SERVER.fullmatch(value) is not None or _IPV6.fullmatch(value) is not None)


def is_headless(environ: Mapping[str, str], platform: str | None = None) -> bool:
    """No desktop to open a browser on: reached over SSH, or Linux with no display."""
    platform = sys.platform if platform is None else platform
    if environ.get("SSH_CONNECTION") and platform in ("linux", "darwin"):
        return True
    return platform.startswith("linux") and not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def tunnel_line(port: int, environ: Mapping[str, str], *, user: str | None = None, server: str | None = None) -> str:
    """The command for the person's own computer. The server is the address this SSH session reached us on when there
    is one, else this machine's name; either that fails the patterns becomes ``SERVER``."""
    if server is None:
        parts = (environ.get("SSH_CONNECTION") or "").split()
        server = parts[2] if len(parts) == 4 else ""
        if not valid_server(server):
            try:
                server = socket.getfqdn() or socket.gethostname()
            except OSError:
                server = ""
    if user is None:
        try:
            user = getpass.getuser()
        except (OSError, KeyError, ImportError):
            user = ""
    user = user if valid_user(user) else "USER"
    server = server if valid_server(server) else "SERVER"
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("port")
    return f"ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:{port}:127.0.0.1:{port} {user}@{server}"

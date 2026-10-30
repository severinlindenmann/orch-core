"""The host's network settings for the hub link (R6 #1594, design section 3): one optional HTTP proxy.

Host configuration only. ``<dir>/hub-net.json`` (0600) holds ``{"proxy": null | "http://h:p" | "https://h:p",
"proxy_user": null | str}`` and ``<dir>/hub-proxy.secret`` (0600) the Basic password, never inside the JSON, a log, a
status or any message. The environment (``HTTPS_PROXY``, ``ALL_PROXY``, ``NO_PROXY``), PAC, WPAD and OS settings are not
read here or anywhere on the link path. The hub never supplies any of it. This module loads and validates; writing the
files is the owner's route at the workspace (T-C).
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "NET_FILE", "SECRET_FILE", "NetConfigError", "NetSettings", "ProxySettings", "load_net", "parse_proxy_url",
]  # fmt: skip

NET_FILE = "hub-net.json"
SECRET_FILE = "hub-proxy.secret"
_HOST = re.compile(r"(?:\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9.-]{1,253}):[1-9][0-9]{0,4}")
_TEXT = re.compile(r"[\x21-\x7e]{1,255}")  # printable ASCII, no space: no CR/LF can reach a header
MAX_FILE = 4096


class NetConfigError(ValueError):
    """The network settings files are not usable (wrong shape, permissions or characters)."""


@dataclass(frozen=True)
class ProxySettings:
    scheme: str  # http | https: how the proxy itself is reached
    host: str  # without brackets
    port: int
    user: str | None = None
    password: str | None = field(default=None, repr=False)  # never shown

    @property
    def authorization(self) -> str | None:
        """The ``Proxy-Authorization`` value (Basic), sent on the CONNECT only; ``None`` without a user."""
        if self.user is None:
            return None
        raw = f"{self.user}:{self.password or ''}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")


@dataclass(frozen=True)
class NetSettings:
    proxy: ProxySettings | None = None


def parse_proxy_url(text: object) -> tuple[str, str, int]:
    """``http://host:port`` or ``https://host:port`` only (port required); user info, path, query, fragment refused.
    Returns ``(scheme, host, port)``."""
    if not isinstance(text, str) or not text.isascii() or len(text) > 300:
        raise NetConfigError("proxy")
    scheme, sep, rest = text.partition("://")
    if not sep or scheme not in ("http", "https") or _HOST.fullmatch(rest) is None:
        raise NetConfigError("proxy")
    hostpart, _, port_s = rest.rpartition(":")
    port = int(port_s)
    if port > 65535:
        raise NetConfigError("proxy")
    host = hostpart[1:-1] if hostpart.startswith("[") else hostpart
    return scheme, host, port


def _read_private(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise NetConfigError("unreadable") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_size > MAX_FILE:
            raise NetConfigError("permissions")
        return os.read(fd, MAX_FILE + 1)
    finally:
        os.close(fd)


def load_net(directory: Path | str) -> NetSettings:
    """The settings kept in ``directory``; no file is no proxy. Anything unreadable, loose (group/other bits), with an
    unknown key or a bad value raises :class:`NetConfigError`: a half-read proxy setting must not turn into a direct
    dial."""
    d = Path(directory)
    raw = _read_private(d / NET_FILE)
    if raw is None:
        return NetSettings()
    try:
        doc = json.loads(raw)
    except ValueError:
        raise NetConfigError("shape") from None
    if type(doc) is not dict or set(doc) != {"proxy", "proxy_user"}:
        raise NetConfigError("shape")
    if doc["proxy"] is None:
        return NetSettings()
    scheme, host, port = parse_proxy_url(doc["proxy"])
    user = doc["proxy_user"]
    if user is not None and (type(user) is not str or _TEXT.fullmatch(user) is None or ":" in user):
        raise NetConfigError("user")
    password = None
    if user is not None:
        secret = _read_private(d / SECRET_FILE)
        if secret is not None:
            try:
                password = secret.decode("utf-8")
            except UnicodeDecodeError:
                raise NetConfigError("password") from None
            password = password.removesuffix("\n")
            if "\n" in password or "\r" in password or "\x00" in password:
                raise NetConfigError("password")
    return NetSettings(ProxySettings(scheme, host, port, user, password))

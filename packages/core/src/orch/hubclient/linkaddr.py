"""Link addresses (orch-hub ``contracts/hub/protocol.md`` 10.10, R6 #1594): ``host:port`` (direct TLS) or
``ws://host[:port]`` / ``wss://host[:port]`` (the link's TLS inside a WebSocket). Written from the protocol text (D79:
no hub import); the ``address`` / ``connect_endpoint`` / ``hub_where`` cases of ``vectors_link_ws.json`` are replayed in
``tests/hub/test_link_ws_vectors.py``.

An address is a hint, never trust: whatever form it has, the link's own TLS 1.3 runs end to end and is pinned to the
hub card's SPKI. Anything outside the grammar is ``malformed`` (:class:`HubRefused`)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from .errors import HubRefused

__all__ = ["DEFAULT_PORT", "MAX_ADDRESS", "LinkAddr", "parse_link_address"]

MAX_ADDRESS = 300
DEFAULT_PORT = {"ws": 80, "wss": 443}
_HOSTPORT = re.compile(r"(?P<host>\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9.-]{1,253})(?::(?P<port>[1-9][0-9]{0,4}))?")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


@dataclass(frozen=True)
class LinkAddr:
    """``scheme`` is ``tcp`` (direct), ``ws`` or ``wss``; ``host`` without brackets; ``port`` explicit or default."""

    scheme: str
    host: str
    port: int
    text: str

    @property
    def is_ip(self) -> bool:
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            return False
        return True

    @property
    def sni(self) -> str | None:
        """Server name for the inner TLS: the DNS name in lower case; an IP literal sends none."""
        return None if self.is_ip else self.host.lower()

    @property
    def host_header(self) -> str | None:
        """The upgrade request's ``Host`` (ws/wss only), ``:port`` only when not the scheme's default."""
        if self.scheme == "tcp":
            return None
        h = "[" + self.host + "]" if ":" in self.host else self.host
        return h if DEFAULT_PORT[self.scheme] == self.port else f"{h}:{self.port}"

    @property
    def same_as(self) -> tuple[str, str, int]:
        """Identity for "no address twice": scheme, lower-case host, port."""
        return self.scheme, self.host.lower(), self.port


def _host_ok(raw: str) -> str | None:
    if raw[0] == "[":
        try:
            ipaddress.IPv6Address(raw[1:-1])
        except ValueError:
            return None
        return raw[1:-1]
    labels = raw.split(".")
    if any(_LABEL.fullmatch(x) is None for x in labels):
        return None
    if re.fullmatch(r"[0-9]+", labels[-1]):  # numeric: a whole, canonical dotted quad only
        try:
            return raw if str(ipaddress.IPv4Address(raw)) == raw else None
        except ValueError:
            return None
    return raw


def parse_link_address(text: object) -> LinkAddr:
    """Parse one address of the grammar (protocol 10.10); refuse (``malformed``) anything else: non-ASCII (IDN only as
    ``xn--``), over 300 characters, another or upper-case scheme, user info, path (even ``/``), query, fragment,
    percent-escapes, white space, a port 0, over 65535 or with a leading zero, a direct address without a port."""
    if not isinstance(text, str) or not text or len(text) > MAX_ADDRESS or not text.isascii():
        raise HubRefused("malformed")
    scheme, rest = "tcp", text
    if text.startswith("wss://"):
        scheme, rest = "wss", text[6:]
    elif text.startswith("ws://"):
        scheme, rest = "ws", text[5:]
    m = _HOSTPORT.fullmatch(rest)
    host = _host_ok(m["host"]) if m else None
    if m is None or host is None:
        raise HubRefused("malformed")
    if m["port"] is None:
        if scheme == "tcp":
            raise HubRefused("malformed")
        return LinkAddr(scheme, host, DEFAULT_PORT[scheme], text)
    port = int(m["port"])
    if port > 65535:
        raise HubRefused("malformed")
    return LinkAddr(scheme, host, port, text)

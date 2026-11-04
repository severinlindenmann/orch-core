"""Errors of the link's transports (``contracts/hub/protocol.md`` 10.10). Kept in one small module so the dial code
(:mod:`.transport`) and the TLS code (:mod:`.tls`) share them without importing each other; :mod:`.tls` re-exports the
first four, where callers have always found them."""

from __future__ import annotations

__all__ = [
    "LinkClosed", "LinkInspected", "LinkTimeout", "NoPin", "PinMismatch", "ProxyAuth", "ProxyError", "ProxyRefused",
    "ProxyUnreachable",
]  # fmt: skip


class LinkClosed(ConnectionError):
    """The peer closed the connection or the transport failed."""


class LinkTimeout(TimeoutError):
    """A deadline passed."""


class PinMismatch(Exception):
    """The peer's certificate does not hash to the pinned SPKI (nothing was sent)."""


class NoPin(PinMismatch):
    """There is no usable pin to dial with (missing, malformed or the card's placeholder): nothing was dialled."""


class LinkInspected(PinMismatch):
    """The pin did not match on a path through a proxy or a WebSocket front: something on that network opens
    encrypted connections (a company proxy). Host code ``hub.link_inspected``; the link stays down, never falls back."""

    code = "hub.link_inspected"


class ProxyError(LinkClosed):
    """The configured HTTP proxy could not carry the link (never answered by a direct dial). ``code`` is the host code,
    ``detail`` a plain sentence without product names."""

    code = "hub.proxy_refused"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail


class ProxyAuth(ProxyError):
    """407: the proxy wants a sign-in the host does not have or does not support (NTLM / Negotiate)."""

    code = "hub.proxy_auth"


class ProxyRefused(ProxyError):
    """The proxy answered the CONNECT with anything but 2xx, or with a head that is not HTTP."""

    code = "hub.proxy_refused"


class ProxyUnreachable(ProxyError):
    """The proxy itself could not be reached (refused, timed out, its TLS failed)."""

    code = "hub.proxy_unreachable"

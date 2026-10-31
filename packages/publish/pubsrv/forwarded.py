"""The ONE place publish reads ``X-Forwarded-*`` / ``X-Real-IP`` (a guard test enforces it).

Structural rule (orch-core #1796): publish grants NOTHING for being on the private network (VPN) or forwarded. Every
request is public; Host and X-Forwarded-* are never a trust or locality input (links and apps use the CONFIGURED
origin). The forwarded client address is only a RATE-LIMIT KEY (any VPN member can forge it). Missing or malformed:
the socket address.
"""

from __future__ import annotations

import ipaddress

__all__ = ["rate_key"]


def _addr(text):
    a = ipaddress.ip_address(str(text).strip().split("%")[0])
    return a.ipv4_mapped if a.version == 6 and a.ipv4_mapped else a


def _norm(a):
    return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)


def rate_key(scope):
    """The rate-limit identity of an ASGI request: last X-Forwarded-For entry, else X-Real-IP, else the socket peer
    (IPv6 by /64). Never a trust or locality input."""
    h = {}
    for k, v in scope.get("headers") or ():
        if k in (b"x-forwarded-for", b"x-real-ip"):
            h[k] = v.decode("latin-1")
    for c in (h.get(b"x-forwarded-for", "").split(",")[-1], h.get(b"x-real-ip", "")):
        if c.strip():
            try:
                return _norm(_addr(c))
            except ValueError:
                break
    peer = (scope.get("client") or ("?", 0))[0]
    try:
        return _norm(_addr(peer))
    except ValueError:
        return peer

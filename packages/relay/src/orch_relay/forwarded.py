"""The ONE place the relay reads ``X-Forwarded-*`` / ``X-Real-IP`` (a guard test enforces it).

Structural rule (orch-core #1796): the relay grants NOTHING for being on the private network (VPN) or for being
forwarded. Every request is public: there is no local, private-network or trusted-forwarder privilege anywhere, and
Host / X-Forwarded-* are never used for a trust decision. The forwarded client address is only a RATE-LIMIT KEY
(any VPN member can forge it, so it can never be more than that). Missing or malformed: the socket address.
"""

from __future__ import annotations

import ipaddress

__all__ = ["rate_key"]


def _addr(text):
    a = ipaddress.ip_address(str(text).strip().split("%")[0])
    return a.ipv4_mapped if a.version == 6 and a.ipv4_mapped else a


def _norm(a):
    return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)


def rate_key(peer, headers):
    """The rate-limit identity: the last X-Forwarded-For entry (the proxy appends what it saw), else X-Real-IP,
    else the socket peer. An IPv6 address is its /64. Never a trust or locality input."""
    candidates = [
        ((headers.get("x-forwarded-for") or "").split(",")[-1]),
        headers.get("x-real-ip") or "",
    ]
    for c in candidates:
        if c.strip():
            try:
                return _norm(_addr(c))
            except ValueError:
                break  # malformed: the socket address, never a fresh identity
    try:
        return _norm(_addr(peer))
    except ValueError:
        return peer

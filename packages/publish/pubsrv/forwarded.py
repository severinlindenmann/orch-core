"""The ONE place a part reads ``X-Forwarded-For`` / ``X-Real-IP`` (a guard test enforces it).

This file is mirrored byte for byte in orch-relay and orch-publish (neither may depend on the other or on core); a test
checks that the copies are identical. Change both together.

Structural rule (orch-core #1796): a part grants NOTHING for being on the private network (VPN) or for being forwarded.
Every request is public: no local, private-network or trusted-forwarder privilege exists, and Host / X-Forwarded-* are
never a trust or locality input. The forwarded client address is only a RATE-LIMIT KEY, and it is used only when the
socket peer is inside the configured trusted-proxy list (comma-separated addresses or CIDR networks, for example the
private network's ranges 100.71.0.0/16 and fd0f:80b5:c7cd:bbad::/64). Empty list (the default): no forwarded header is
read; the key is the socket address.

Trade-off: with a VPN-only bind every real connection arrives through the VPN, so a public client cannot forge the
header. A VPN member can, but that only shifts a rate key; it grants nothing.
"""

from __future__ import annotations

import ipaddress

__all__ = ["parse_trusted", "headers_of_scope", "rate_key"]


def parse_trusted(spec):
    """``"10.0.0.1, 100.71.0.0/16"`` -> tuple of networks; empty -> (). A malformed entry raises ValueError."""
    if not spec:
        return ()
    if not isinstance(spec, str):
        return tuple(spec)
    return tuple(ipaddress.ip_network(p.strip(), strict=False) for p in spec.split(",") if p.strip())


def _addr(text):
    a = ipaddress.ip_address(str(text).strip().split("%")[0])
    return a.ipv4_mapped if a.version == 6 and a.ipv4_mapped else a


def _norm(a):
    return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)


def headers_of_scope(scope):
    """The request headers of an ASGI scope as ``{lower-case name: value}`` (last one wins)."""
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or ()}


def rate_key(peer, headers, nets=()):
    """The rate-limit identity: IPv4 per address, IPv6 per /64. The rightmost X-Forwarded-For entry, else X-Real-IP,
    is used ONLY when ``peer`` is inside ``nets``; otherwise (and when it is missing or malformed) the socket peer."""
    try:
        me = _addr(peer)
    except ValueError:
        return str(peer)
    if any(me in n for n in nets if n.version == me.version):
        for c in ((headers.get("x-forwarded-for") or "").split(",")[-1], headers.get("x-real-ip") or ""):
            if c.strip():
                try:
                    return _norm(_addr(c))
                except ValueError:
                    break  # malformed: the socket address, never a fresh identity
    return _norm(me)

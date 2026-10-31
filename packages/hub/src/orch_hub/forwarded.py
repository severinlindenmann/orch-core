"""The ONE place the hub reads ``X-Forwarded-*`` / ``X-Real-IP`` (a guard test enforces it).

The hub is private-network only and is not served through a proxy. A request that carries any forwarding header is
REFUSED (400) unless its socket peer is a configured ``trusted_peers`` address (the Caddy on this computer in the
server role). So a proxy pointed at the hub later cannot make public traffic look private or local. For a request from
a configured edge the client is the rightmost X-Forwarded-For entry that is not itself trusted (the existing rule).
"""

from __future__ import annotations

import ipaddress

__all__ = ["FORWARDING", "FORWARDING_PREFIX", "NOT_PROXIED", "carries", "norm_ip", "refuse", "source_of"]

#: refused whatever follows the prefix (x-forwarded-prefix, x-forwarded-server ...), and these exact names
FORWARDING_PREFIX = "x-forwarded-"
FORWARDING = (
    "x-forwarded-for",
    "x-real-ip",
    "forwarded",
    "x-client-ip",
    "true-client-ip",
    "cf-connecting-ip",
)
NOT_PROXIED = "this hub is not served through a proxy"


def carries(names) -> bool:
    """True when any of the header ``names`` (lower case) is a forwarding header."""
    return any(n.lower().startswith(FORWARDING_PREFIX) or n.lower() in FORWARDING for n in names)


def refuse(names, peer: str, trusted: frozenset[str]) -> bool:
    return carries(names) and peer not in trusted


def norm_ip(addr: str) -> str:
    """One source per network that a host can freely pick addresses in: an IPv6 /64 is one source."""
    ip = ipaddress.ip_address(addr)  # ValueError for "" and anything that is not an address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def source_of(headers: dict[str, str], peer: str, trusted: frozenset[str]) -> str:
    """The socket peer, or, only when it is a configured edge, the rightmost ``X-Forwarded-For`` entry that is not
    itself trusted (entries left of it are client-controlled and never read). IPv6 is its /64."""
    if peer in trusted:
        for entry in reversed(headers.get("x-forwarded-for", "").split(",")):
            entry = entry.strip()
            if entry in trusted:
                continue
            try:
                return norm_ip(entry)
            except ValueError:
                break
    try:
        return norm_ip(peer)
    except ValueError:
        return peer or "unknown"  # a source is never empty

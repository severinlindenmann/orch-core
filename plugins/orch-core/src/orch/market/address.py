"""Where the installer may connect (spec 5.1 point 3, vectors ``address_*``). IPv4 must be global unicast; IPv6 is an
allow list (``2000::/3`` minus Teredo and the rest of ``2001::/23``, documentation and 6to4); IPv4-mapped is judged as
the IPv4 address it carries. Anything that is not an address is refused. Every address a name resolves to must pass:
there is no "pick the good one" (E7a)."""

from __future__ import annotations

import ipaddress
import socket
import threading

from . import MarketError

_V6_OK = ipaddress.ip_network("2000::/3")
_V6_NO = tuple(ipaddress.ip_network(n) for n in ("2001::/23", "2001:db8::/32", "3fff::/20", "2002::/16"))


def check_address(text: object) -> str:
    """The address to connect to (IPv4 text, or IPv6 text), or ``market.address_refused``."""
    if not isinstance(text, str) or "%" in text:
        raise MarketError("market.address_refused")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise MarketError("market.address_refused") from None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address):
        bad = ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback or ip.is_link_local
        if bad or not ip.is_global or ip == ipaddress.IPv4Address("255.255.255.255"):
            raise MarketError("market.address_refused")
        return str(ip)
    if ip not in _V6_OK or any(ip in n for n in _V6_NO):
        raise MarketError("market.address_refused")
    return str(ip)


def resolve(host: str, timeout: float) -> list[str]:
    """Every address of ``host`` (port 443), each checked. The lookup runs under a deadline (a thread: a resolver
    cannot be interrupted). No result, a timeout or any refused address is refused."""
    box: list[list[str]] = []

    def work() -> None:
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            box.append(sorted({i[4][0] for i in infos}))
        except OSError:
            box.append([])

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise MarketError("market.fetch_failed")
    found = box[0] if box else []
    if not found:
        raise MarketError("market.fetch_failed")
    return [check_address(a) for a in found]  # all of them, in a fixed order; the first is pinned by the caller

"""``hub.publish_where`` (protocol.md §10.9.1, #1838): where this hub's workspaces publish (``server``) and share
(``share``), signed by ``HK_op`` under ``sig_hub_publish_where`` and delivered in the ``hub_where`` and join answers.

Pure checks, no I/O, no clock. :func:`check_publish_where` verifies one object a hub sent; :func:`canonical_origin` is the
origin rule the hub applies when it signs and a workspace applies when it verifies (the same function, so the two cannot
drift). Core has its own copy written from the protocol text (``orch.hubclient.publish_where``); both replay the
``publish_where`` cases of ``contracts/hub/vectors_hub_backup.json``.

The caller owns what needs state: that ``op_pub`` is the key of the CURRENT op cert of the pinned card (not revoked, not
expired), re-checked on every read of a held object (decision 4), and what to do with the result.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from . import hub as H
from . import suite as S
from .canon import CanonError, cj, unb64u

KIND = "hub.publish_where"
LABEL = "sig_hub_publish_where"
SEQ_MAX = 2**53 - 1
MAX_ORIGIN = 300

_NETS_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # CGNAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local
        "172.16.0.0/12",  # private
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # documentation
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # documentation
        "203.0.113.0/24",  # documentation
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, broadcast
    )
)
_NETS_V6 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "::/128",  # unspecified
        "::1/128",  # loopback
        "::ffff:0:0/96",  # IPv4-mapped
        "64:ff9b::/96",  # NAT64
        "100::/64",  # discard
        "2001:db8::/32",  # documentation
        "fc00::/7",  # unique local (ULA)
        "fe80::/10",  # link-local
        "fec0::/10",  # site-local (deprecated)
        "ff00::/8",  # multicast
    )
)
_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.ip_address("::1")
_BLOCKED_NAMES = ("localhost", "local", "internal")
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PORT_RE = re.compile(r"[1-9][0-9]{0,4}")
_DEFAULT_PORT = {"https": "443", "http": "80"}
#: characters IDNA 2003 (the stdlib codec) and IDNA 2008 treat differently, and the dots other than ``.``: refused
_IDNA_UNSAFE = frozenset("ßς\u200c\u200d\u3002\uff0e\uff61")


def _bad() -> H.Refused:
    return H.Refused("bad_origin")


def canonical_origin(x: object, dev: bool) -> str:
    """The canonical form of an origin a hub advertises, or ``Refused("bad_origin")``.

    ``https://host[:port]`` or, only with ``dev``, ``http://`` on a loopback host (``localhost``, ``127.0.0.0/8``,
    ``[::1]``). No userinfo, path (not even ``/``), query or fragment. The host is lower-case, an IDN host is its A-label
    form, one trailing dot is dropped, the default port of the scheme is dropped. Outside that loopback case the scheme
    must be ``https`` and a host that is ``localhost``, ``local``, ``internal`` or under ``.localhost``, ``.local``,
    ``.internal``, or an IP literal in a loopback, private, link-local, ULA, CGNAT, documentation, multicast or reserved
    block is refused. Public IP literals are allowed (a no-domain install). Idempotent."""
    if type(x) is not str or type(dev) is not bool or not 1 <= len(x) <= MAX_ORIGIN:
        raise _bad()
    scheme, sep, rest = x.partition("://")
    if not sep or scheme not in _DEFAULT_PORT or not rest:
        raise _bad()
    if any(c in "/?#@\\%" or c.isspace() or not c.isprintable() for c in rest):
        raise _bad()
    port: str | None
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    if rest.startswith("["):
        host, close, tail = rest[1:].partition("]")
        if not close or (tail and not tail.startswith(":")):
            raise _bad()
        port = tail[1:] if tail else None
        try:
            ip = ipaddress.IPv6Address(host)
        except ValueError:
            raise _bad() from None
        shown = f"[{ip.compressed}]"
    else:
        host, colon, port_s = rest.partition(":")
        if ":" in port_s:
            raise _bad()
        port = port_s if colon else None
        host = host.lower().removesuffix(".")
        if not host.isascii():
            if unicodedata.normalize("NFC", host) != host or _IDNA_UNSAFE & set(host):
                raise _bad()
            try:
                host = host.encode("idna").decode("ascii")
            except UnicodeError:
                raise _bad() from None
        labels = host.split(".")
        if len(host) > 253 or not all(_LABEL_RE.fullmatch(lab) for lab in labels):
            raise _bad()
        if labels[-1].isdigit():  # a dotted quad, canonical only; "127.1", "2130706433" and the like are refused
            try:
                ip = ipaddress.IPv4Address(host)
            except ValueError:
                raise _bad() from None
            if str(ip) != host:
                raise _bad()
        shown = host
    if port is not None and (_PORT_RE.fullmatch(port) is None or int(port) > 65535):
        raise _bad()
    if ip is None:
        loopback = shown == "localhost"
        blocked = shown in _BLOCKED_NAMES or shown.endswith(_BLOCKED_SUFFIXES)
    else:
        loopback = ip == _LOOPBACK_V6 or (ip.version == 4 and ip in _LOOPBACK_V4)
        blocked = any(ip in n for n in (_NETS_V4 if ip.version == 4 else _NETS_V6))
    if not (dev and loopback) and (scheme != "https" or blocked):
        raise _bad()
    return f"{scheme}://{shown}" + (f":{int(port)}" if port is not None and port != _DEFAULT_PORT[scheme] else "")


def check_publish_where_shape(signed: Any) -> dict[str, Any]:
    """Only the shape (what a ``hub_where`` or join answer must carry): the envelope ``{o, sig}``, the header, the exact
    field set and types, ``seq`` in range and a signature of the right length. No signature check, no origin rule: those
    need the pinned card and the dev marker and are :func:`check_publish_where`'s. Returns ``o``."""
    o = H._envelope(signed, KIND)
    H._fields(o, KIND)
    if o["seq"] > SEQ_MAX:
        raise H.Refused("malformed")
    try:
        unb64u(signed["sig"], S.SIG_LEN)
    except CanonError:
        raise H.Refused("malformed") from None
    return o


def check_publish_where(
    signed: Any, *, op_pub: bytes, hub_id: str, dev: bool, held: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Verify one ``hub.publish_where`` and return its ``o`` with both origins canonical (the signature covers the origins
    as the hub sent them; the canonical form is what is compared, shown and kept as the value).

    ``op_pub`` is the pinned card's current ``HK_op`` key, ``hub_id`` the pinned hub, ``dev`` the pinned card's dev
    marker, ``held`` the signed object (the envelope as received) already kept for this hub, or ``None``.
    Refusals, in order: ``malformed`` / ``wrong_suite`` (envelope, header, exact fields), ``bad_signature``, ``other_hub``,
    ``dev_refused`` / ``dev_mismatch``, ``bad_origin`` (``server`` or ``share``), ``stale_seq`` (``seq`` below the held
    one), ``seq_conflict`` (the held ``seq`` with other bytes)."""
    if type(op_pub) is not bytes or not S.valid_public_key(op_pub) or type(dev) is not bool:
        raise H.Refused("malformed")
    o = H._envelope(signed, KIND)
    H._signature(signed, H.L[LABEL], op_pub)
    check_publish_where_shape(signed)
    if o["hub_id"] != hub_id:
        raise H.Refused("other_hub")
    H._dev(o, dev)
    out = {**o, "server": canonical_origin(o["server"], dev), "share": canonical_origin(o["share"], dev)}
    if held is not None:
        try:
            held_seq = held["o"]["seq"]
            same = cj(signed) == cj(held)
        except KeyError, TypeError, CanonError:
            raise H.Refused("malformed") from None
        if type(held_seq) is not int:
            raise H.Refused("malformed")
        if o["seq"] < held_seq:
            raise H.Refused("stale_seq")
        if o["seq"] == held_seq and not same:
            raise H.Refused("seq_conflict")
    return out

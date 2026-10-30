"""Hub addresses on the host (M4 #1461, design ``m4-settings-design.md`` section 3, Opus conditions 1-4).

The hub's link address is not signed: the TLS pin of the card the host holds decides who answers. So an address is
only ever a place to try that pin. Two rules carry everything here:

* an address is stored (``addr`` / ``addrs`` of ``hub.json``) only after a TLS handshake AT that address passed
  against the pin the host already holds. A ``hub_where`` entry and the card a connect link serves are hints, never
  evidence (condition 1);
* the address in use stays until a replacement passed that handshake; a hub that advertises only dead addresses
  changes nothing (condition 2).

Two ways in: :func:`learn` (after a good link session, the hub's own ``hub_where``) and :func:`move` (the owner opens a
connect link of the SAME hub in Mission Control or ``orch hub address --link``; condition 3: person only, the invite
is not used, the card is not replaced)."""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

#: Test seam ``(host, port, pin) -> None``: raises when the handshake does not pass. ``None``: the real pinned TLS dial.
PROBE: Callable[[str, int, str], None] | None = None
PROBE_TIMEOUT_S = 3.0
MAX_PROBES = 4  # per refresh

_LOG = logging.getLogger("orch.host.hubaddr")


FAIL_BACKOFF_S = 3600.0  # an address that failed is not tried again for an hour
_FAILED: dict[tuple[str, str], float] = {}
_MU = threading.Lock()
_METADATA = (ipaddress.ip_address("fd00:ec2::254"),)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_V4_COMPAT = ipaddress.ip_network("::/96")


def clear() -> None:
    """Forget the failed-address memory (tests)."""
    with _MU:
        _FAILED.clear()


def _ips(host: str) -> list[str]:
    """Resolve ``host`` ONCE (an IP literal needs no lookup). The lookup is bounded: a resolver cannot be interrupted,
    so it runs in a helper thread that is abandoned on timeout."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    out: list[list[str]] = []

    def work() -> None:
        try:
            out.append([i[4][0] for i in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)])
        except OSError:
            out.append([])

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(PROBE_TIMEOUT_S)
    return out[0] if out else []


def ip_allowed(ip: str, *, allow_loopback: bool) -> bool:
    """The probe policy. A hub (or a link) must not make this computer touch internal services it has no business
    with: link-local (cloud metadata lives there), multicast, unspecified, broadcast and the IPv6 metadata address are
    never probed; loopback only when the address in use is loopback too. LAN and private-network addresses are fine
    (the pin decides who answers, and the owner needs no domain)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return all(_base_ok(c, allow_loopback) for c in _forms(a))


def _forms(a: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The address and every IPv4 address an IPv6 form embeds: mapped, 6to4 (2002::/16), Teredo, NAT64 (64:ff9b::/96)
    and IPv4-compatible (::/96), so a wrapped 169.254.169.254 or 127.0.0.1 is judged as what it reaches."""
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [a]
    if isinstance(a, ipaddress.IPv6Address):
        for v4 in (a.ipv4_mapped, a.sixtofour):
            if v4 is not None:
                out.append(v4)
        if a.teredo is not None:
            out.extend(a.teredo)
        if a in _NAT64 or a in _V4_COMPAT:
            out.append(ipaddress.IPv4Address(int(a) & 0xFFFFFFFF))
    return out


def _base_ok(a: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_loopback: bool) -> bool:
    if a.is_unspecified or a.is_multicast or a.is_link_local or a in _METADATA:
        return False
    if isinstance(a, ipaddress.IPv4Address) and a == ipaddress.IPv4Address("255.255.255.255"):
        return False
    return allow_loopback or not a.is_loopback


def is_loopback(addr: str | None) -> bool:
    """Whether the stored address is loopback by its own text: a loopback IP literal or exactly ``localhost``. Never
    from a DNS answer (a name that resolves to 127.0.0.1 grants nothing)."""
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.tlsadapter import parse_addr

    if not addr:
        return False
    try:
        host = parse_addr(addr).host
    except HubRefused:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


#: Address classes for a dial through a proxy (the proxy resolves names on its own side, so the text decides):
#: ``lan`` (private/shared ranges, single-label, .local/.lan/.localdomain/.home.arpa names),
#: ``loopback``
#: (loopback, localhost, *.localhost), ``sensitive`` (link-local, metadata: 169.254/16, fe80::/10, fd00:ec2::254,
#: *.internal, metadata.*), ``public`` (anything else). Only ``public`` goes through a proxy; ``lan`` IP literals
#: also when the address in use is a ``lan`` IP literal. ``loopback`` and ``sensitive`` never do.
_LAN_SUFFIXES = (".local", ".lan", ".localdomain", ".home.arpa")


def _name_class(host: str) -> str:
    h = host.lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        return "loopback"
    if h.endswith(".internal") or h.split(".")[0] == "metadata":
        return "sensitive"
    if "." not in h or h.endswith(_LAN_SUFFIXES):
        return "lan"
    return "public"


def _ip_class(a: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    forms = _forms(a)
    if any(f.is_link_local or f in _METADATA for f in forms):
        return "sensitive"
    if any(f.is_loopback or f.is_unspecified for f in forms):
        return "loopback"
    return "public" if all(f.is_global and not getattr(f, "is_site_local", False) for f in forms) else "lan"


def address_class(host: str) -> str:
    """``public`` | ``lan`` | ``loopback`` | ``sensitive`` by the text of ``host`` (an IP literal or a name)."""
    try:
        return _ip_class(ipaddress.ip_address(host))
    except ValueError:
        return _name_class(host)


def _is_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_internal(addr: str | None) -> bool:
    """Whether the stored address is in the ``lan`` class by its own text (only that class lets other ``lan`` addresses
    be tried through a proxy)."""
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.tlsadapter import parse_addr

    if not addr:
        return False
    try:
        host = parse_addr(addr).host
        return address_class(host) == "lan" and _is_literal(host)
    except HubRefused:
        return False


def checked_ip(host: str, *, allow_loopback: bool, proxied: bool = False, allow_internal: bool = False) -> str:
    """THE address policy, used by the probe and by every dial of a hub-learned address: resolve ``host`` once, check
    every answer with :func:`ip_allowed` (one forbidden answer refuses the name) and return the IP to connect to. The
    caller connects to that exact IP; the name is for display only. Raises ``OSError`` when refused.

    ``proxied``: the dial goes through the host's proxy, which resolves a name on its own side, so a name is not
    resolved here and is judged by its text instead (:func:`address_class`): only public ones pass; ``lan`` ones pass
    when ``allow_internal`` (the address in use is ``lan`` too); loopback and sensitive ones never."""
    try:
        lit = ipaddress.ip_address(host)
    except ValueError:
        lit = None
    if proxied:
        cls = address_class(host)
        # a LAN current address (an IP literal) unlocks LAN IP literals only, never a name
        if cls != "public" and not (cls == "lan" and allow_internal and lit is not None):
            raise OSError("address refused by policy")
    if proxied and lit is None:
        return host  # the proxy resolves it; nothing to check locally beyond the text
    ips = _ips(host)
    if not ips or not all(ip_allowed(i, allow_loopback=allow_loopback) for i in ips):
        raise OSError("address refused by policy")
    return ips[0]


def dial_resolver(current_addr: str | None, net: Any = None) -> Callable[[str], str]:
    """The ``resolve`` hook for dialling hub-learned addresses: policy-checked IP, loopback only next to a loopback
    current address (decided by the stored text). With a proxy the same policy decides the text of a name."""
    from orch.hubclient.tlslink import transport

    allow, internal = is_loopback(current_addr), is_internal(current_addr)
    return lambda host: checked_ip(
        host,
        allow_loopback=allow,
        proxied=net is not None and net.proxy is not None and not transport.is_loopback_host(host),
        allow_internal=internal,
    )


def dial_host(a: Any, net: Any, *, allow_loopback: bool, allow_internal: bool = False) -> str:
    """The host to hand to the dial of link address ``a``: the policy-checked IP, except a NAME behind the host's
    proxy, which is the proxy's to resolve (sent as text, after the same policy judged it)."""
    from orch.hubclient.tlslink import transport

    return checked_ip(
        a.host,
        allow_loopback=allow_loopback,
        proxied=transport.proxy_applies(a, net) is not None,
        allow_internal=allow_internal,
    )


def handshake_ok(
    addr: str, spki: str | None, *, allow_loopback: bool = False, net: Any = None, allow_internal: bool = False
) -> bool:
    """True when a TLS handshake AT ``addr`` presents the held pin ``spki``. The name is resolved once, the IP checked
    (:func:`checked_ip`), and that exact IP is dialled. Nothing is sent over the connection and it is closed at once.
    No pin, no handshake. Nothing about the outcome is ever reported to the hub."""
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.tlsadapter import parse_addr
    from orch.hubclient.tlslink import tls

    if not spki:
        return False
    try:
        a = parse_addr(addr)
        ip = dial_host(a, net, allow_loopback=allow_loopback, allow_internal=allow_internal)
    except (HubRefused, OSError):
        return False
    try:
        if PROBE is not None:
            PROBE(ip, a.port, spki)
        else:  # ws(s):// and a proxy included: the same dial as the link, then the pinned handshake
            tls.dial_link(a, spki, timeout=PROBE_TIMEOUT_S, connect_host=ip, net=net).close()
    except Exception:  # noqa: BLE001 - refused, timed out, wrong certificate: all the same answer, "not verified"
        return False
    return True


def _probe_new(scope: str, state: Any, links: list[str], net: Any = None) -> list[str]:
    """Probe ``links`` (at most :data:`MAX_PROBES` attempts) and return the ones that passed. A link the policy
    refuses still counts as an attempt (no dial happens); one that failed within the backoff is skipped."""
    allow_lo = is_loopback(state.addr)
    now = time.monotonic()
    with _MU:  # forget the failures whose hour is over
        for k in [k for k, at in _FAILED.items() if now - at >= FAIL_BACKOFF_S]:
            del _FAILED[k]
    passed: list[str] = []
    tried = 0
    for link in links:
        if tried >= MAX_PROBES:
            break
        with _MU:
            at = _FAILED.get((scope, link))
        if at is not None and now - at < FAIL_BACKOFF_S:
            continue
        tried += 1
        if handshake_ok(link, state.tls_spki, allow_loopback=allow_lo, net=net, allow_internal=is_internal(state.addr)):
            passed.append(link)
        else:
            with _MU:
                _FAILED[(scope, link)] = time.monotonic()
    return passed


def _net(service: Any) -> Any:
    """The host's proxy settings. Unusable files raise ``ProxyRefused`` (an ``OSError``): nothing is dialled direct."""
    from . import hubnet

    return hubnet.load(service.state_dir)


def known(state: Any) -> list[str]:
    return [a for a in [state.addr, *state.addrs] if a]


def fetch_where(hub_link: Any) -> dict[str, Any] | None:
    """Inside the link session: ask ``hub_where`` and validate the answer; nothing else. ``None`` on any failure (the
    host keeps what it holds). No probe happens here: the hub must not be able to time them."""
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.where import check_hub_where

    try:
        return check_hub_where(hub_link.call("hub_where", {}))  # type: ignore[no-any-return]
    except (HubRefused, OSError) as exc:
        _LOG.info("hub_where not used (%s)", getattr(exc, "code", type(exc).__name__))
        return None


def apply_where(service: WorkspaceService, state: Any, answer: dict[str, Any]) -> bool:
    """After the link is closed: probe the entries not held yet (at most :data:`MAX_PROBES`) and store the ones whose
    handshake passed the held pin. True when ``hub.json`` changed."""
    have = set(known(state))
    fresh = [e["link"] for e in answer["addresses"] if e["link"] not in have]
    passed: list[str] = []
    if fresh:
        try:
            net = _net(service)
        except OSError:
            return False  # settings unusable: no probe, nothing stored, nothing dialled
        passed = _probe_new(f"{service.workspace_id}:{state.hub_id}", state, fresh, net)
    return _store(service, state, passed, [e["link"] for e in answer["addresses"]], answer["name"])


_RUNNING: set[str] = set()


def apply_in_background(service: WorkspaceService, state: Any, answer: dict[str, Any]) -> threading.Thread | None:
    """Run :func:`apply_where` in a daemon thread, so a refresh (and the gated action waiting on it) never waits for a
    probe, and the hub cannot read probe timing off its own link. One run per workspace and hub at a time."""
    scope = f"{service.workspace_id}:{state.hub_id}"
    with _MU:
        if scope in _RUNNING:
            return None
        _RUNNING.add(scope)

    def work() -> None:
        try:
            apply_where(service, state, answer)
        except Exception:  # noqa: BLE001 - best effort, never loud
            _LOG.info("hub addresses not updated", exc_info=True)
        finally:
            with _MU:
                _RUNNING.discard(scope)

    t = threading.Thread(target=work, name="hub-addrs", daemon=True)
    t.start()
    with _MU:
        _THREADS[:] = [x for x in _THREADS if x.is_alive()]
        _THREADS.append(t)
    return t


_THREADS: list[threading.Thread] = []


def wait_idle(timeout: float = 30.0) -> None:
    """Wait for background address work (tests)."""
    for t in list(_THREADS):
        t.join(timeout)
    _THREADS[:] = [t for t in _THREADS if t.is_alive()]


def _store(service: Any, started: Any, passed: list[str], advertised: list[str], name: str | None) -> bool:
    from orch.hubclient.service import MAX_ADDRS

    from . import hubops

    with hubops._MU:  # noqa: SLF001
        cur = hubops._load_state(service)  # noqa: SLF001
        if (
            cur is None
            or cur.status != "bound"
            or cur.hub_id != started.hub_id
            or cur.binding_seq != started.binding_seq
            or cur.tls_spki != started.tls_spki  # the pin moved while we probed: the handshakes proved the old one
        ):
            return False
        pool = [a for a in [*cur.addrs, *passed] if a != cur.addr]
        # the hub's preference first, then the verified ones it no longer names (kept: they cost only a dial)
        ordered = [a for a in advertised if a in pool] + [a for a in pool if a not in advertised]
        merged = list(dict.fromkeys(ordered))[:MAX_ADDRS]
        changed = merged != cur.addrs or (name is not None and name != cur.hub_name)
        if changed:
            cur.addrs, cur.hub_name = merged, name if name is not None else cur.hub_name
            hubops._write_state(service, cur)  # noqa: SLF001
        return changed


def move(call: Call, link_text: str, *, now_ms: int, dev: bool) -> dict[str, Any]:
    """``hub.address {link}``: the owner moves this workspace to the address a connect link of the SAME hub names. The
    link's card is fetched and checked (same hub, same root, same TLS pin) but is never evidence: the address is
    stored only if a handshake AT it passes the pin already held. The invite code is never used, the card in
    ``hub.json`` is not touched, and nothing is signed."""
    from orch.crypto.encoding import unb64u
    from orch.hubclient import HubRefused, connectlink, wire
    from orch.hubclient.service import MAX_ADDRS

    from . import hubops

    service = call.service
    state = hubops._load_state(service)  # noqa: SLF001
    if state is None or state.status != "bound":
        raise HostError("hub.not_joined", "this workspace is not connected to a hub")
    other = HostError("hub.refused", "this is not the hub you are connected to")
    try:
        link = connectlink.parse_link(link_text)
        if link.hub_id != state.hub_id:
            raise other
        if link.endpoint in (None, "local"):
            raise HostError("invalid.input", "this link names no address: make a new link in the hub console")
        card = connectlink.fetch_card(link)
        verified = wire.verify_card(card, now_ms=now_ms, dev_mode=dev, pinned_root=unb64u(state.hk_root_pub, 65))
    except HubRefused as exc:
        raise hubops._refused(exc.code) from None  # noqa: SLF001
    if verified["card"]["hub_id"] != state.hub_id or verified["card"]["tls_spki_sha256"] != state.tls_spki:
        raise other
    new = link.endpoint
    assert new is not None
    try:
        net = _net(service)
    except OSError as exc:
        raise hubops._refused(getattr(exc, "code", "hub.proxy_refused").removeprefix("hub.")) from None  # noqa: SLF001
    if new not in known(state) and not handshake_ok(
        new, state.tls_spki, allow_loopback=is_loopback(state.addr), net=net
    ):
        raise HostError(
            "hub.unreachable",
            "no hub answers at that address with the certificate this workspace trusts; nothing was changed",
            retry_after_ms=2000,
        )
    with hubops._MU:  # noqa: SLF001
        cur = hubops._load_state(service)  # noqa: SLF001
        if (
            cur is None
            or cur.status != "bound"
            or cur.hub_id != state.hub_id
            or cur.binding_seq != state.binding_seq
            or cur.tls_spki != state.tls_spki
        ):
            raise HostError("hub.not_joined", "this workspace is not connected to that hub any more")
        if cur.addr != new:
            cur.addrs = list(dict.fromkeys([*([cur.addr] if cur.addr else []), *(a for a in cur.addrs if a != new)]))
            cur.addrs = cur.addrs[:MAX_ADDRS]
            cur.addr = new
            hubops._write_state(service, cur)  # noqa: SLF001
    hubops._audit(call, "hub.address", hub_id=state.hub_id)  # noqa: SLF001
    return {"hub_id": state.hub_id, "addr": new, "addrs": list(cur.addrs)}

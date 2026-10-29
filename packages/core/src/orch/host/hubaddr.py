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

import logging
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


def handshake_ok(addr: str, spki: str | None) -> bool:
    """True when a TLS handshake at ``addr`` (any address, loopback and LAN included) presents the held pin ``spki``.
    Nothing is sent over the connection and it is closed at once. No pin, no handshake."""
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.tlsadapter import parse_addr
    from orch.hubclient.tlslink import tls

    if not spki:
        return False
    try:
        host, port = parse_addr(addr)
    except HubRefused:
        return False
    try:
        if PROBE is not None:
            PROBE(host, port, spki)
        else:
            tls.connect_pinned(host, port, spki, timeout=PROBE_TIMEOUT_S).close()
    except Exception:  # noqa: BLE001 - refused, timed out, wrong certificate: all the same answer, "not verified"
        return False
    return True


def known(state: Any) -> list[str]:
    return [a for a in [state.addr, *state.addrs] if a]


def learn(service: WorkspaceService, state: Any, hub_link: Any) -> bool:
    """After a good link session: ask ``hub_where``, validate it, probe the entries not held yet (at most
    :data:`MAX_PROBES`) and store the ones whose handshake passed the held pin. Best effort: any failure keeps exactly
    what the host holds. True when ``hub.json`` changed."""
    from orch.hubclient.errors import HubRefused
    from orch.hubclient.where import check_hub_where

    try:
        answer = check_hub_where(hub_link.call("hub_where", {}))
    except (HubRefused, OSError) as exc:
        _LOG.info("hub_where not used (%s)", getattr(exc, "code", type(exc).__name__))
        return False
    have = set(known(state))
    fresh = [e["link"] for e in answer["addresses"] if e["link"] not in have][:MAX_PROBES]
    passed = [a for a in fresh if handshake_ok(a, state.tls_spki)]
    return _store(service, state, passed, [e["link"] for e in answer["addresses"]], answer["name"])


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
    if new not in known(state) and not handshake_ok(new, state.tls_spki):
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

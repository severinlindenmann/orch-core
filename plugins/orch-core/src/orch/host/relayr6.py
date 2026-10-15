"""R6a host wiring (11A.15.2, 11A.15.5): the ``r6`` and ``link_source`` seams of R5's relay hook.

* :func:`r6` ``(service) -> (EpochKeySource, R6View)``: R6's key store (``hosts/<ws>/membership/``), checked before
  the worker starts (H7: an unsafe store raises :class:`MembershipUnsafe`, alarm ``membership_state_unsafe``), and
  an ``R6View`` that serves nothing until R6b's verified committed list exists (fail closed: R2 then has no members).
* :func:`link_source` ``(service)``: the first card (WXK/CK custody, signed by an R6 ``RelaySigner`` whose ledger is
  ``membership/sign-ledger.json``), ``PK`` and the delegation from the workspace's own genesis (``check_genesis``),
  and the convenience check of 11A.6 before anything is drawn. ``genesis()`` refuses until the person path exists.

One key store per membership directory for the host process's lifetime (its ``flock`` is held once): both seams and
every restart of the worker share it; :func:`close` releases it (unlink, tests). The store is never closed under a
running worker.

JUDGEMENT CALL (R6a): the card is signed by R6's own ``RelaySigner`` over the same WSK with its own ledger. R5's
signer (relay state, ``sign-ledger.json`` there) never signs a card: R1 only PUTs the bytes this source returns. One
ledger therefore covers every card (11A.15.5: "the ledger keeps us from signing two"); R5 MUST NOT sign cards.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from .errors import HostError

__all__ = ["close", "keys_for", "link_source", "membership_path", "r6"]

_LOCK = threading.Lock()
_OPEN: dict[str, Any] = {}


def membership_path(service: Any) -> Path:
    """``<state>/hosts/<ws>/membership``, from the host's trusted state path resolved once (11A.3, R6-4)."""
    return Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "membership"


def _now_ms(service: Any) -> int:
    return int(service.clock() * 1000)


def _previously_linked(service: Any) -> bool:
    """F2: the workspace is linked, or its first card may be at the relay (``link-card.json``): a missing
    ``membership/`` is then H7, never a fresh start. Unreadable or unsafe link state counts as linked (fail closed)."""
    from .custody import read_json

    relay = Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "relay"
    try:
        link = read_json(relay / "link.json", service.host_uid)
        if isinstance(link, dict) and link.get("state") == "linked":
            return True
        return os.path.lexists(relay / "link-card.json")
    except (HostError, OSError, ValueError):
        return True


def keys_for(service: Any) -> Any:
    """The workspace's :class:`orch.relay.r6.keystore.MembershipKeys`, opened once per process."""
    from orch.relay.r6.keystore import MembershipKeys

    path = membership_path(service)
    with _LOCK:
        mk = _OPEN.get(str(path))
        if mk is None:
            mk = MembershipKeys(
                path, service.workspace_id, now_ms=lambda: _now_ms(service),
                previously_linked=_previously_linked(service),
            )  # fmt: skip
            _OPEN[str(path)] = mk
        return mk


def close(service: Any) -> None:
    with _LOCK:
        mk = _OPEN.pop(str(membership_path(service)), None)
    if mk is not None:
        mk.close()


class _NoView:
    """R6b replaces this with the verified committed list (11A.15.2 ``R6View``). Until then: no members."""

    def member_list(self) -> None:
        return None


def _rebuild_gate(service: Any, mk: Any) -> None:
    """The relay gate invariant at start (round 3): a loss of standing the running store could not report (a crash
    between its state change and the durable gate write, a change made while no R6 hook was installed) is found
    here. Every device of the committed list AND of the exposed body (a late 2xx would commit it, R4) whose F1
    standing is not ``ok`` raises the gate; a malformed entry raises it for every device. Runs after the store hook
    is set and before the key store is bound, so a failure here leaves the key store gated (R3). Then (round 5,
    F-1) every durable ``f1_standing`` reason whose device is ``ok`` in the live store is pruned, under the store
    lock: the one lowering path besides a rotation. (Forwarded ``HostPins`` evidence of a device F1 still holds is
    R6b's ``desired()``.)"""
    store = service.store
    with store.locked():  # one store-lock span: no standing change lands between the checks, the raises and the prune
        listed: dict[str, set[str]] = {}
        for dev, person in mk.listed_devices():
            if not dev or not person:
                mk.raise_gate("", "h7")
                continue
            listed.setdefault(dev, set()).add(person)
            # THE standing function (orch.store.store.device_standing_ok), as the listed person's device
            if not store.device_ok("d_" + dev, "p_" + person):
                mk.raise_gate(dev, "f1_standing")

        def standing_ok(dev: str) -> bool:
            """The same function and the same question as the raise above: a listed device is ok only as the device
            of every person it is listed for; an unlisted one by F1 alone."""
            persons = listed.get(dev)
            if not persons:
                return bool(store.device_ok("d_" + dev))
            return all(store.device_ok("d_" + dev, "p_" + p) for p in persons)

        # round 5 (F-1): a durable f1_standing reason for a device that is ok in the live store (an append that
        # failed before its commit point after the gate was written) is dropped
        mk.prune_stale_reasons(standing_ok)


def r6(service: Any) -> tuple[Any, Any]:
    """R5's ``r6`` seam: the key store as R2's ``EpochKeySource`` (one seal lock), and the view. Checked (H7); the
    store hook installed, the gate rebuilt from the store, THEN the key store bound to the live ``service.store``
    (a rebuild failure leaves it gated; a reopened store has no hook: gated until ``r6`` runs again). Without a
    store: refused."""
    from orch.relay.r6.keystore import MembershipUnsafe, R6Gate

    mk = keys_for(service)
    mk.check_start()
    if service.store is None:
        raise MembershipUnsafe("no store to hook")
    R6Gate(mk).install(lambda: service.store, rebuild=lambda: _rebuild_gate(service, mk))
    return mk, _NoView()


def _convenience_block(service: Any) -> str | None:
    """11A.6 under the store lock: ``convenience_events_present`` when ``relay.link`` would be refused."""
    from .convenience import refuse_reader_op

    store = service.store
    if store is None:
        return "workspace_unavailable"
    try:
        with store.locked():
            refuse_reader_op(service, "relay.link", history=True)
    except HostError as exc:
        if exc.code == "sign.convenience_readers":
            return "convenience_events_present"
        raise
    return None


def link_source(service: Any) -> Any:
    """R5's ``link_source`` seam: :class:`orch.relay.r6.firstcard.R6LinkSource` for this workspace. The origin is
    ``relay/link.json``'s (stored verbatim, 11A.2), read with the host's checked read."""
    from orch.custody import FileBackend
    from orch.relay import wire
    from orch.relay.r6.firstcard import R6LinkSource, default_part
    from orch.relay.sign import LinkContext, RelaySigner
    from orch.store import BackendSigner

    from .custody import read_json

    ws = service.workspace_id
    base = Path(os.path.realpath(service.state_dir)) / "hosts" / ws
    link = read_json(base / "relay" / "link.json", service.host_uid)
    if not isinstance(link, dict) or link.get("state") not in ("linking", "linked"):
        raise HostError("relay.link_mismatch", "link.json is missing or in no known state")
    origin = link.get("origin")
    try:
        wire.check_origin(origin)
    except (wire.WireError, TypeError):
        raise HostError("relay.link_mismatch", "link.json names no valid relay origin") from None
    keys_dir = base / "keys"
    backend = FileBackend(keys_dir) if keys_dir.is_dir() else None
    if backend is None or not backend.exists("wsk"):
        raise HostError("workspace.unavailable", "the workspace signing key is not in the host's custody")
    store = service.store
    genesis = store.genesis_event() if store is not None else None
    if genesis is None:
        raise HostError("workspace.unavailable", "the workspace has no genesis")
    mk = keys_for(service)
    mk.check_start()
    signer = RelaySigner(BackendSigner(backend, "wsk"), LinkContext(ws, origin), ledger=mk.ledger, burned=mk.is_burned)
    return R6LinkSource.from_genesis(
        mk, signer, genesis_event=genesis, part=default_part(str(genesis.get("prefix", ""))),
        now_ms=lambda: _now_ms(service), blocked=lambda: _convenience_block(service),
    )  # fmt: skip

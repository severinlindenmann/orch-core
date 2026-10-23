"""R6b host wiring (11A.15.4, "As built in B1"): the membership publisher behind ``relaypair.MEMBERSHIP`` and the real
``R6View`` behind ``relayr6.r6``.

* :func:`membership` ``(service) -> HostMembership``: the object ``relaypair.start_thread`` binds to the pair engine.
  It is :class:`orch.relay.r6.pair.UnavailableMembership` (never ready, every addition
  ``refused(publisher_unavailable)``; FAIL CLOSED) until :meth:`HostMembership.attach` gives it a worker, which
  ``start_thread`` does when it is handed one. With a worker it delegates to
  :class:`orch.relay.r6.membership.MembershipPublisher`, a companion thread of the worker (started after the loop and
  the publisher, stopped and joined with them).
* :func:`view` ``(service, keys) -> CommittedView``: the host's own committed list, re-verified at load.
* :func:`standing`: the eligible devices, ONE worker job under the store lock: F1 standing ``ok`` for the exact pair
  (``device_standing_ok``, the one implementation of 11A.15.2), no forwarded ``HostPins`` evidence for the exact pair,
  a certificate that verifies at the host clock.

One :class:`GrantStore` (a locked ``membership/grants/`` directory) per membership directory for the process lifetime,
like the key store; :func:`close` releases it (unlink, tests).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from .errors import HostError

__all__ = ["HostMembership", "close", "membership", "standing", "view"]

LOG = logging.getLogger("orch.host.relayr6b")
_LOCK = threading.Lock()
_GRANTS: dict[str, Any] = {}


def _grants_for(service: Any) -> Any:
    from orch.relay.r6.membership import GrantStore

    from .relayr6 import membership_path

    path = membership_path(service) / "grants"
    with _LOCK:
        store = _GRANTS.get(str(path))
        if store is None:
            store = _GRANTS[str(path)] = GrantStore(path)
        return store


def close(service: Any) -> None:
    from .relayr6 import membership_path

    with _LOCK:
        store = _GRANTS.pop(str(membership_path(service) / "grants"), None)
    if store is not None:
        store.close()


def _devices(service: Any) -> Any:
    """The pairing engine's :class:`PairStore` (its ``devices/`` directory is locked once per process)."""
    from . import relaypair

    return relaypair.engine_for(service).store


def view(service: Any, keys: Any) -> Any:
    """The real ``R6View`` (11A.15.2): ``keys`` is the workspace's :class:`MembershipKeys`."""
    from orch.relay.r6.view import CommittedView

    from .relayr3 import owner_pk_pub, wsk_pub

    def device(device_id: str) -> Any:
        try:
            return _devices(service).device(device_id)
        except Exception:  # noqa: BLE001 - no pairing store or an unreadable record: the view refuses the list
            return None

    return CommittedView(
        keys, wsk_pub=wsk_pub(service), owner_pk_pub=owner_pk_pub(service), device=device,
        alarm=lambda code, detail: LOG.warning("%s: %s", code, detail),
    )  # fmt: skip


def standing(service: Any, store: Any, owner_pk_pub: bytes, ids: list[str]) -> set[str]:
    """The devices of ``ids`` that may be in a list now. Runs as a worker job (the store lock is taken here)."""
    from orch.identity import certs
    from orch.identity.errors import Refused
    from orch.store.store import device_standing_ok

    live = service.store
    if live is None:
        raise HostError("workspace.unavailable", "the workspace is not open")
    now_ms = int(service.clock() * 1000)
    ok: set[str] = set()
    with live.locked():
        state = live.state
        evidence = {(e["device_id"], e["person_id"]) for e in live.forwarded_evidence()}
        for d in ids:
            rec = store.device(d)
            if not isinstance(rec, dict):
                continue
            person = rec.get("person_id")
            if type(person) is not str or not device_standing_ok(state, "d_" + d, "p_" + person):
                continue
            if (d, person) in evidence:
                continue
            try:
                certs.verify_cert(rec.get("cert"), owner_pk_pub, now_ms)
            except Refused:
                continue
            ok.add(d)
    return ok


class HostMembership:
    """``MembershipQueue`` for the pair engine: fail closed until a worker is attached."""

    def __init__(self, service: Any) -> None:
        self._service = service
        self._events: Any = None
        self._inner: Any = None

    def bind(self, events: Any) -> None:
        self._events = events
        if self._inner is not None:
            self._inner.bind(events)

    def ready(self) -> bool:
        return self._inner is not None and bool(self._inner.ready())

    def post_addition(self, offer_id: str, device_id: str, cert: dict[str, Any]) -> None:
        if self._inner is not None:
            self._inner.post_addition(offer_id, device_id, cert)
        elif self._events is not None:  # no worker: never published (exactly UnavailableMembership)
            self._events.on_refused(offer_id, "publisher_unavailable")

    def post_exclusion(self, offer_id: str, device_id: str, reason: str) -> None:
        if self._inner is not None:
            self._inner.post_exclusion(offer_id, device_id, reason)

    # -- the worker's companion ------------------------------------------------------------------------------------

    def attach(self, worker: Any) -> None:
        """Build the publisher for ``worker`` (its session, connection, alarms and jobs). Runs on the starter's path:
        it waits for no job of the workspace's worker thread."""
        from orch.custody import FileBackend
        from orch.relay.r6.membership import MembershipPublisher
        from orch.store import BackendSigner
        from orch.wsproto.sign import LinkContext, RelaySigner

        from .relayr3 import owner_pk_pub
        from .relayr6 import keys_for

        service = self._service
        ws = service.workspace_id
        link = worker.state.read_json("link.json")
        origin = link.get("origin") if isinstance(link, dict) else None
        if type(origin) is not str or worker.conn is None:
            raise HostError("relay.link_mismatch", "link.json names no origin, or the worker has no connection")
        keys_dir = Path(os.path.realpath(service.state_dir)) / "hosts" / ws / "keys"
        backend = FileBackend(keys_dir) if keys_dir.is_dir() else None
        if backend is None or not backend.exists("wsk"):
            raise HostError("workspace.unavailable", "the workspace signing key is not in the host's custody")
        wsk = BackendSigner(backend, "wsk")
        mk = keys_for(service)
        signer = RelaySigner(wsk, LinkContext(ws, origin), ledger=mk.ledger, burned=mk.is_burned)
        pk = owner_pk_pub(service)
        devices = _devices(service)

        def on_error(exc: BaseException) -> None:
            worker.alarm("relay_membership_stopped", type(exc).__name__)
            worker.stop()

        inner = MembershipPublisher(
            mk, signer, session=worker.session, conn=worker.conn, grants=_grants_for(service), owner_pk_pub=pk,
            wsk_pub=bytes(wsk.public_key), standing=lambda ids: worker.job(lambda: standing(service, devices, pk, ids)),
            device=devices.device, device_ids=devices.device_ids, alarm=worker.alarm, on_error=on_error,
            now_ms=lambda: int(service.clock() * 1000),
        )  # fmt: skip
        if self._events is not None:
            inner.bind(self._events)
        self._inner = inner
        worker.companions.append(inner)


def membership(service: Any) -> HostMembership:
    """``relaypair.MEMBERSHIP`` (the R6b seam): a fresh queue per worker start."""
    return HostMembership(service)

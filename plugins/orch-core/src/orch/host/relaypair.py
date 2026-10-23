"""R6c host wiring (11A.15.6, 11A.15.9A): the pairing engine of a workspace, the host's answers it needs
(:class:`HostPairing`), the ``relay.pair_device`` source (:class:`R6PairDeviceSource`), registration at open and the
pair thread for the relay worker.

* :func:`engine_for` ``(service)``: one :class:`orch.relay.r6.pair.PairEngine` per membership directory for the
  host process's lifetime (its ``pairing/`` and ``devices/`` directories hold an ``flock``). Without a running
  worker it is **dormant**: its relay routes answer ``retry`` and its membership queue is the fail-closed
  :class:`UnavailableMembership`, so step 9 refuses and nothing is published; the records, the step-9 hooks and the
  recovery work. :func:`start_thread` attaches the worker's session and the membership seam and returns the pair
  thread the bridge loop runs.
* :func:`register` ``(service)``: called by :meth:`WorkspaceService.open` **before** ``human.recover`` (the R5
  checklist, 11A.15.19 item 13), so an interrupted ``relay.pair_device`` submit rolls forward through R6's hooks
  instead of failing ``op.unsupported``. It runs R6's own open-time recovery (corrupt records quarantined, intents
  whose event landed rolled forward) before anything is served. Dormant behind the R6 gate: it registers only when
  :func:`orch.relay.worker.r6_ready` is true or a pairing ever ran here (``membership/pairing`` exists), and never
  creates ``membership/`` (its existence makes ``_relay_paired`` true, 11A.6).
* :data:`MEMBERSHIP`: **the R6b seam (orch-47)**, ``fn(service) -> MembershipQueue``. ``None`` (the default until
  R6b lands) means :class:`UnavailableMembership`: no pairing can complete.
* :func:`primary_set`: ``relay.primary_set``'s body (11A.15.1): the primary's certificate verified under the genesis
  ``PK`` (the owner's person, not ``drop:``, not revoked) and the human's cleaned label, written to
  ``membership/primary.json``.

The host codes these refusals carry (11A.15.9, §0.6): ``relay.pair_open``, ``relay.not_linked``,
``relay.no_primary``, ``relay.quota``, ``relay.pair_closed``.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .errors import HostError
from .pairdevice import PairAllocation, event_digest

__all__ = [
    "MEMBERSHIP",
    "api",
    "status",
    "HostPairing",
    "R6PairDeviceSource",
    "close",
    "engine_for",
    "primary_set",
    "register",
    "start_thread",
]

LOG = logging.getLogger("orch.host.relaypair")

#: the R6b seam (orch-47): ``fn(service) -> MembershipQueue``; None until R6b lands (fail closed)
MEMBERSHIP: Callable[[Any], Any] | None = None

PRIMARY_SCOPES = ("look",)  # the primary's certificate: least privilege, never a member (11A.15.1)
MAX_DEVICES = 1024  # the member list's cap (11A.15.4): no offer while the relay-reachable devices reach it
PRIMARY_LABEL_MAX = 80

_LOCK = threading.Lock()
_OPEN: dict[str, Any] = {}


def _membership(service: Any) -> Path:
    from .relayr6 import membership_path

    return membership_path(service)


def _relay_dir(service: Any) -> Path:
    return Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "relay"


def _link(service: Any) -> dict[str, Any] | None:
    from .custody import read_json

    try:
        link = read_json(_relay_dir(service) / "link.json", service.host_uid)
    except (HostError, OSError, ValueError):
        return None
    return link if isinstance(link, dict) else None


def _owner_pk(service: Any) -> bytes:
    from orch.identity import members
    from orch.identity.errors import Refused

    store = service.store
    genesis = store.genesis_event() if store is not None else None
    if genesis is None:
        raise HostError("workspace.unavailable", "the workspace has no genesis")
    try:
        return members.check_genesis(genesis)["owner_pk_pub"]
    except Refused as e:
        raise HostError("workspace.unavailable", f"genesis: {e.code}") from None


def _wsk(service: Any) -> Any:
    from orch.custody import FileBackend
    from orch.store import BackendSigner

    keys_dir = Path(service.state_dir) / "hosts" / service.workspace_id / "keys"
    backend = FileBackend(keys_dir) if keys_dir.is_dir() else None
    if backend is None or not backend.exists("wsk"):
        raise HostError("workspace.unavailable", "the workspace signing key is not in the host's custody")
    return BackendSigner(backend, "wsk")


def _landed(service: Any, intent: Any) -> bool:
    """The allocation intent's event is in the log with the intent's digest (matched by the event, never by "same
    cert, same device")."""
    line = service.store.logged_event("workspace", intent.get("event_id"))
    return line is not None and event_digest(line) == intent.get("event_digest")


# -- the host's answers ---------------------------------------------------------------------------------------------


class HostPairing:
    """:class:`orch.relay.r6.pair.PairHost` over the workspace service. None of these runs under the pairing lock."""

    def __init__(self, service: Any) -> None:
        self.service = service
        #: the relay worker's held-checkpoint source while a worker runs (``None``: the host's own held checkpoint)
        self.checkpoint_source: Callable[[], dict[str, Any] | None] | None = None

    def gate(self, meta: Any, offer_id: str, who: dict[str, str]) -> tuple[Any, Any]:
        from orch.relay.r6.pair import GateRefused

        from .convenience import pair_gate

        try:
            g = pair_gate(self.service, meta, offer_id=offer_id, who=who)
        except HostError as e:
            reason = (e.detail or {}).get("reason", "") if isinstance(e.detail, dict) else ""
            raise GateRefused(e.code, str(reason)) from None
        return g.decl, g.decl_who

    def revoked(self, device_id: str) -> bool:
        """F1 knows the device and it is removed or revoked, or ``HostPins`` holds revocation evidence for it."""
        store = self.service.store
        if store is None:
            return True  # fail closed
        dev = "d_" + device_id
        with store.locked():
            view = store.state.workspace.devices.get(dev)
            if view is not None and (view.removed or view.revoked):
                return True
        from orch.store.pins import HostPins

        pins = HostPins(self.service.state_dir, self.service.workspace_id)
        try:
            return any(r.get("device") in (dev, device_id) for r in pins.revocations())
        except Exception:  # noqa: BLE001 - unreadable evidence: fail closed
            return True
        finally:
            pins.close()

    def primary(self) -> Any:
        """``membership/primary.json`` verified now (11A.15.1, Opus M7), or ``None``."""
        from orch import crypto
        from orch.identity.certs import verify_cert
        from orch.identity.errors import Refused
        from orch.identity.pairing import clean_label
        from orch.relay.r6.pair import PrimaryInfo

        from .relayr6 import keys_for

        try:
            obj = keys_for(self.service).read_primary()
        except Exception:  # noqa: BLE001 - unreadable: no primary (fail closed)
            return None
        if not isinstance(obj, dict) or set(obj) != {"v", "cert", "label", "set_ms"} or obj["v"] != 1:
            return None
        try:
            o = verify_cert(obj["cert"], _owner_pk(self.service), int(self.service.clock() * 1000))
            if o["scopes_max"] != list(PRIMARY_SCOPES) or self.revoked(o["device_id"]):
                return None
            label = obj["label"]
            if type(label) is not str or clean_label(label) != label or not label:
                return None
            return PrimaryInfo(o["device_id"], crypto.unb64u(o["dk_kx_pub"]), label)
        except (Refused, HostError, KeyError, TypeError, ValueError, crypto.EncodingError):
            return None

    def offer_blocked(self) -> Any:
        """Step 1's preconditions under the store lock (11A.15.6): linked, a running worker, no ``membership_quota``
        alarm, the device count below the cap."""
        from orch.relay.r6.pair import PairRefused

        service = self.service
        store = service.store
        if store is None:
            return PairRefused("workspace.unavailable", "the workspace is not open")
        with store.locked():
            link = _link(service)
            if link is None or link.get("state") != "linked":
                return PairRefused("relay.not_linked", "the workspace is not linked to a relay")
            worker = service.relay
            if worker is None or not getattr(worker, "alive", lambda: False)():
                return PairRefused("workspace.unavailable", "the relay worker is not running: orch host relay start")
            engine = _OPEN.get(str(_membership(service)))
            alarms = engine.store.alarms()["alarms"] if engine is not None else {}
            if "membership_quota" in alarms:
                return PairRefused("relay.quota", "the relay's membership quota is reached")
            if engine is not None and len(engine.store.device_ids()) >= MAX_DEVICES:
                return PairRefused("relay.quota", "the workspace has the most relay devices a list can hold")
        return None

    def card_seq(self) -> int | None:
        from .relayr6 import keys_for

        try:
            seq = keys_for(self.service).head()["card"]["card_seq"]
        except Exception:  # noqa: BLE001 - no card: the baseline waits
            return None
        return seq if type(seq) is int and seq >= 1 else None

    def checkpoint(self) -> dict[str, Any] | None:
        """The held checkpoint ``{n, hash}`` (the relay's verified one while a worker runs, else the host's own)."""
        if self.checkpoint_source is not None:
            return self.checkpoint_source()
        from orch import canon
        from orch.store.pins import HostPins

        pins = HostPins(self.service.state_dir, self.service.workspace_id)
        try:
            held = pins.held()
        except Exception:  # noqa: BLE001 - unreadable: no baseline yet
            return None
        finally:
            pins.close()
        if not isinstance(held, dict) or not isinstance(held.get("o"), dict):
            return None
        return {"n": held["o"]["n"], "hash": canon.checkpoint_hash(held["o"])}

    def settle(self, fn: Callable[[Callable[[Any], bool]], Any]) -> Any:
        """``fn(landed)`` under the store lock: no submit is between its precommit and its append meanwhile."""
        store = self.service.store
        if store is None:
            return None
        with store.locked():
            return fn(lambda intent: _landed(self.service, intent))


class _NoRelay:
    """The dormant engine's relay: every route is ``retry`` (nothing leaves while no worker runs)."""

    def _no(self, *a: Any, **k: Any) -> Any:
        from orch.relay.r6.pair import PairRelayError

        raise PairRelayError("retry")

    post_answer = post_cert_request = get_cert_request = put_device = post_baseline = _no


# -- the engine ---------------------------------------------------------------------------------------------------


def engine_for(service: Any) -> Any:
    """The workspace's pairing engine (created once per process). Refused (``relay.not_linked``) without a link:
    ``membership/`` is never created here."""
    from orch.relay.r6.pair import PairContext, PairEngine, UnavailableMembership
    from orch.relay.r6.pairstore import PairStore
    from orch.wsproto.sign import LinkContext, RelaySigner

    from .relayr6 import keys_for

    path = _membership(service)
    with _LOCK:
        engine = _OPEN.get(str(path))
        if engine is not None:
            return engine
        link = _link(service)
        if link is None or link.get("state") not in ("linking", "linked") or not path.is_dir():
            raise HostError("relay.not_linked", "the workspace is not linked to a relay")
        ctx = PairContext(service.workspace_id, link["origin"], _wsk(service).public_key, _owner_pk(service))
        mk = keys_for(service)
        host = HostPairing(service)
        store = PairStore(path)
        holder: dict[str, Any] = {}

        def primary_device() -> str | None:
            p = host.primary()
            return None if p is None else p.device_id

        signer = RelaySigner(
            _wsk(service), LinkContext(service.workspace_id, link["origin"]), ledger=mk.ledger, burned=mk.is_burned,
            owner_person_id=ctx.owner_person_id, primary_device=primary_device,
            pair_live=lambda o: holder["e"].is_live(o), pair_binding=lambda *b: holder["e"].binding_held(*b),
        )  # fmt: skip
        engine = PairEngine(
            ctx, store, signer, host, _NoRelay(), UnavailableMembership(), now_ms=lambda: int(service.clock() * 1000),
            post=lambda fn: None,  # dormant: the tick publishes an allocated offer once a worker runs
        )  # fmt: skip
        holder["e"] = engine
        _OPEN[str(path)] = engine
        return engine


def close(service: Any) -> None:
    with _LOCK:
        engine = _OPEN.pop(str(_membership(service)), None)
    if engine is not None:
        engine.store.close()


def start_thread(
    service: Any, session: Any, conn: Callable[[], Any], checkpoint: Any = None, *, worker: Any = None
) -> Any:
    """The pair thread ``orch-relay-r6p-<id8>`` for the relay worker (started and stopped by the bridge loop): the
    engine attached to the worker's session (its own connection, 11A.7) and to the membership seam. ``None`` when
    the workspace has no ``membership/`` (pair items are then dropped, as before R6c)."""
    from orch.relay.r6.pair import PairThread, UnavailableMembership
    from orch.relay.r6.pairrelay import SessionPairRelay

    try:
        engine = engine_for(service)
    except HostError as e:  # no membership/ (R6's key store not built here): no pairing, pair items are dropped
        LOG.info("relay pairing: no pair thread (%s)", e.code)
        return None
    membership = MEMBERSHIP(service) if MEMBERSHIP is not None else UnavailableMembership()
    attach = getattr(membership, "attach", None)
    if worker is not None and callable(attach):
        attach(worker)  # R6b (B1): the membership thread becomes a companion of the worker, started and stopped with it
    engine.routes = SessionPairRelay(session, service.workspace_id, conn=conn)
    engine.membership = membership
    engine.host.checkpoint_source = checkpoint

    holder: dict[str, Any] = {}

    def dormant() -> None:  # the worker stopped: no stale session or publisher stays attached
        engine.routes, engine.membership = _NoRelay(), UnavailableMembership()
        engine.post = lambda fn: None
        engine.host.checkpoint_source = None
        if getattr(service, "pair_thread", None) is holder.get("t"):
            service.pair_thread = None

    thread = PairThread(engine, name=f"orch-relay-r6p-{service.workspace_id[:8]}", on_stop=dormant)
    holder["t"] = thread
    membership.bind(_EngineEvents(engine))
    service.pair_thread = thread  # the 9A path runs its calls as jobs on it (api)
    return thread


def refuse_if_cut_over(service: Any) -> None:
    """V3 C10 (design M3, I2): a workspace whose direct relay path is closing or closed takes no pairing, no relay link
    and no direct input of any kind: ``relay.cut_over`` first, whatever the arguments."""
    from . import directpath

    if directpath.closed(service.state_dir, service.workspace_id, service.host_uid):
        raise HostError("relay.cut_over", "the direct relay path of this workspace is closed: a hub serves it")


def api(service: Any, name: str, *args: Any, **kw: Any) -> Any:
    """**The internal pairing API (11A.15.9A)** for the control ops and the dashboard wrappers: ``name`` is one of
    :data:`orch.relay.r6.pair.API_METHODS` and nothing else (the settlement paths stay the host's, Opus review of
    #588). It runs as a job on the running pair thread, else on the dormant engine (where ``offer`` refuses: no
    worker). A :class:`PairRefused` becomes the :class:`HostError` with its §0.6 code."""
    from orch.relay.r6.pair import PairRefused, run_api
    from orch.relay.r6.pairstore import PairStoreError

    refuse_if_cut_over(service)  # C10: before anything is parsed or any engine is opened
    thread = getattr(service, "pair_thread", None)
    try:
        if thread is not None and thread.alive:
            return thread.api(name, *args, **kw)
        return run_api(engine_for(service), name, *args, **kw)
    except PairRefused as e:
        raise HostError(e.code, e.message) from None
    except PairStoreError as e:
        raise _host_error(e) from None


class _EngineEvents:
    def __init__(self, engine: Any) -> None:
        self._e = engine

    def on_committed(self, offer_id: str, publication: Any) -> None:
        pub = dict(publication)
        self._e.post(lambda: self._e.membership_committed(offer_id, pub))

    def on_refused(self, offer_id: str, reason: str) -> None:
        self._e.post(lambda: self._e.membership_refused(offer_id, reason))


# -- relay.pair_device (H12c-e's PairDeviceSource) ------------------------------------------------------------------


def _host_error(e: Exception) -> HostError:
    from orch.relay.r6.pair import PairRefused
    from orch.relay.r6.pairstore import PairStoreError

    if isinstance(e, PairRefused):
        return HostError(e.code, e.message)
    if isinstance(e, PairStoreError):
        return HostError("internal", "the pairing record is unreadable")
    return HostError("internal", f"the relay pairing failed ({type(e).__name__})")


class R6PairDeviceSource:
    """``WorkspaceService.pair_device`` (orch.host.pairdevice.PairDeviceSource): R6's step 9."""

    def __init__(self, service: Any) -> None:
        self.service = service

    def _engine(self) -> Any:
        return engine_for(self.service)

    def facts(self, offer_id: str) -> Any:
        from orch.relay.r6.pair import PairRefused
        from orch.relay.r6.pairstore import PairStoreError

        try:
            return self._engine().facts(offer_id)
        except (PairRefused, PairStoreError) as e:
            raise _host_error(e) from None

    def precommit(self, allocation: PairAllocation) -> None:
        from orch.relay.r6.pair import IllegalTransition, PairRefused, StaleRecord
        from orch.relay.r6.pairstore import PairStoreError

        try:
            self._engine().precommit(asdict(allocation), landed=lambda i: _landed(self.service, i))
        except (PairRefused, PairStoreError) as e:
            raise _host_error(e) from None
        except (StaleRecord, IllegalTransition) as e:
            raise HostError("relay.pair_closed", f"the pairing changed: {e}") from None

    def committed(self, allocation: PairAllocation, seq: int) -> None:
        line = self.service.store.logged_event("workspace", allocation.event_id)
        if line is None or event_digest(line) != allocation.event_digest:
            raise HostError("internal", "the device.added line is not the allocation's event")
        self._engine().committed(asdict(allocation))

    def refused(self, allocation: PairAllocation, error: HostError) -> None:
        self._engine().refused(asdict(allocation), error.code)

    def allocate_existing(self, offer_id: str) -> bool:
        """ "Already in F1" (orch-f1 S3, 11A.15.6 step 9): under the store lock, if F1 already holds this exact
        device with this certificate and it is valid, the allocation completes naming the existing event (no second
        ``device.added``); a removed or revoked id is refused. ``False``: not in F1 (the normal step 9)."""
        from orch import canon
        from orch.identity.pairdevice import cert_digest

        engine = self._engine()
        facts = self.facts(offer_id)
        store = self.service.store
        with store.locked():
            view = store.state.workspace.devices.get(facts.device)
            if view is None:
                return False
            if view.person != facts.person:
                raise HostError("relay.pair_closed", "F1 holds this device id for another person")
            if view.removed or view.revoked:
                raise HostError("relay.pair_closed", "this device id was removed or revoked: pair with new keys")
            found = None
            for lg, line in store.merged_events():
                if lg == "workspace" and line.get("type") == "device.added" and line.get("device") == facts.device:
                    found = line
            if found is None or canon.dumps(found.get("cert")) != canon.dumps(facts.cert):
                raise HostError("relay.pair_closed", "F1 holds this device with another certificate")
            alloc = {
                "offer_id": offer_id, "attempt": facts.attempt, "device": facts.device,
                "cert_digest": cert_digest(facts.cert), "scopes": list(facts.scopes), "review_digest": "",
                "event_id": found["id"], "event_digest": event_digest(found), "decl_digest": None,
            }  # fmt: skip
            engine.adopt_existing(offer_id, alloc)
        return True


# -- registration at open -----------------------------------------------------------------------------------------


def register(service: Any) -> None:
    """Set ``service.pair_device`` and run R6's open-time recovery, before ``human.recover`` (dormant behind the R6
    gate; never creates ``membership/``)."""
    from orch.relay import worker as relay_worker
    from orch.relay.r6.pairstore import pairing_present

    path = _membership(service)
    if not (relay_worker.r6_ready() or pairing_present(path)):
        return
    service.pair_device = R6PairDeviceSource(service)
    try:
        engine = engine_for(service)
        rolled = engine.recover(lambda i: _landed(service, i))
    except HostError as e:
        LOG.info("relay pairing: no recovery (%s)", e.code)
        return
    except Exception as e:  # noqa: BLE001 - logged; human.recover still rolls a pending submit forward through the hooks
        LOG.error("relay pairing: open-time recovery failed (%s)", type(e).__name__)
        return
    if rolled:
        LOG.info("relay pairing: %d allocation(s) rolled forward at open", len(rolled))


# -- relay.primary_set (11A.15.1) ---------------------------------------------------------------------------------


def primary_set(service: Any, cert: Any, label: str) -> str:
    """Verify the primary's ``PK``-signed certificate under the genesis ``PK`` (the owner's person, not ``drop:``,
    not revoked), clean the human's label, write ``membership/primary.json``. Returns the primary's device id."""
    from orch.identity.certs import verify_cert
    from orch.identity.errors import Refused
    from orch.identity.pairing import clean_label

    from .relayr6 import keys_for

    if _link(service) is None:
        raise HostError("relay.not_linked", "the workspace is not linked to a relay")
    try:  # under the genesis PK, at the host's clock: signature, keys, ids, not expired
        o = verify_cert(cert, _owner_pk(service), int(service.clock() * 1000))
    except Refused as e:
        raise HostError(
            "invalid.input", f"the primary's certificate does not verify under the owner key ({e.code})"
        ) from None
    if o["scopes_max"] != list(PRIMARY_SCOPES):
        raise HostError("invalid.input", 'the primary\'s certificate has scopes_max ["look"] exactly (11A.15.1)')
    if HostPairing(service).revoked(o["device_id"]):
        raise HostError("invalid.input", "the primary's device is revoked")
    if type(label) is not str:
        raise HostError("invalid.input", "label: text")
    cleaned = clean_label(label)[:PRIMARY_LABEL_MAX]
    if not cleaned.strip():
        raise HostError("invalid.input", "label: printable text")
    keys_for(service).write_primary({"v": 1, "cert": cert, "label": cleaned, "set_ms": int(service.clock() * 1000)})
    return o["device_id"]


# -- status (no secret) -------------------------------------------------------------------------------------------


def status(service: Any) -> dict[str, Any] | None:
    """The pairing part of ``host.status.relay`` (R6c-2 adds it to the status frame): the offers as
    :meth:`PairEngine.list_offers` gives them (never ``S``, a link, the SAS, ``sas_nonce`` or a challenge), the
    pairing alarms and the ``needs`` items. ``None`` while no engine is open in this process."""
    from orch.relay.r6.pairstore import PairStoreError

    engine = _OPEN.get(str(_membership(service)))
    if engine is None:
        return None
    try:
        alarms = engine.store.alarms()
        offers = engine.list_offers()
    except PairStoreError:
        return {"offers": [], "alarms": {"pairing_state_corrupt": {"count": 1}}, "needs": []}
    return {
        "offers": [
            {"offer_id": o.offer_id, "state": o.state, "scopes": list(o.scopes), "expires_ms": o.expires_ms,
             "label": o.label, "device_id": o.device_id, "path": o.path,
             "outstanding_request": o.outstanding_request, "second_key": o.second_key}
            for o in offers
        ],
        "alarms": alarms["alarms"], "needs": alarms["needs"], "dropped": engine.dropped,
    }  # fmt: skip

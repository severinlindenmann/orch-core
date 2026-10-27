"""The host's hub operations (V3 C2a, C2b; hub contract section 9, 10.2, spec 8 and 8a).

The owner's signed record is a person op (``hub.join`` -> ``hub.joined``, ``hub.leave`` -> ``hub.left``, see
:mod:`orch.ops.person_ops`); this module is the half that needs the host's keys and the network:

* ``hub.connect {invite, hub_card}``: after ``hub.joined`` is on the log, sign the workspace's ``hub.binding`` with
  ``WSK`` and send ``join`` (card, owner key, binding) over the hub link.
* ``hub.unbind {}``: after ``hub.left`` is on the log, sign ``hub.unbound`` with ``WSK`` and offer it to the hub; the
  hub being down is not an error (it stays queued in ``hub.json`` and is offered again).
* ``hub.status {}``: the model's hub and the host's link state.
* ``hub.handoff {kind, ticket, ...}``: a read for Mission Control's "approve in the hub" panel (:mod:`.hubhandoff`).

Column P only. ``WK_e`` is touched only in full mode (:class:`_Epochs`: the key store seals it itself to the hub's
``HXK`` and hands out the wrap; a leave from full mode raises the key store's ``rotate`` gate, so the next epoch is
made by the normal membership machinery and never sealed to the hub). Selective mode never reaches it, and its link
refuses ``wk_seal`` (``wrong_mode``).
The workspace is a *dev* workspace only on a dev host that proves it (the same proof as the Simulator gate); every
other workspace refuses a dev hub (hub contract 2.4). The real TLS link is C5: :data:`TRANSPORT` is its seam.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from . import hubsign
from .errors import HostError
from .hubsign import BREAK_GLASS

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = [
    "BREAK_GLASS",
    "HUB_CAPS",
    "HUB_OPS",
    "NETWORK_OPS",
    "HubFacts",
    "execute",
    "facts_for",
    "hub_role",
    "publish_link",
    "publish_route",
    "serve",
    "signing_mode",
    "workspace_dev",
]

HUB_OPS: dict[str, int] = {
    "hub.connect": 1, "hub.link": 1, "hub.unbind": 1, "hub.status": 1, "hub.refresh": 1, "hub.handoff": 1,
    "hub.history": 1, "keys.rotate": 1, "hub.switch": 1,
}  # fmt: skip
NETWORK_OPS = frozenset({"hub.connect", "hub.link", "hub.unbind", "hub.refresh", "hub.switch"})
HUB_CAPS = ["hub/1"]
_MU = threading.Lock()

#: C5's seam: ``(service, hub_id) -> HubTransport``, the link to the hub the workspace pinned.
#: ``None``: no link yet, so the hub is unreachable.
TRANSPORT: Any = None
#: the workspace's current v2 directory card (hub contract 9.5), or ``None``; tests replace it
CARD_SOURCE: Any = None

_SCHEMAS: dict[str, dict[str, Any]] = {
    "hub.link": {  # run-9 feedback 6: a read; fetch the hub card named by a connect link and check it against the link
        "type": "object",
        "properties": {"link": {"type": "string", "minLength": 8, "maxLength": 1024}},
        "required": ["link"],
        "additionalProperties": False,
    },
    "hub.connect": {
        "type": "object",
        "properties": {
            "invite": {"type": "string", "minLength": 1, "maxLength": 512},
            "hub_card": {"type": "object"},
            "addr": {"type": "string", "minLength": 3, "maxLength": 300},
        },
        "required": ["hub_card"],  # the invite is needed by a join only (not by a mode switch or a re-seal)
        "additionalProperties": False,
    },
    "hub.refresh": {  # fetch and pin the newest group snapshot; ``hub_card``: a newer hub card (rotated op cert)
        "type": "object",
        "properties": {"hub_card": {"type": "object"}},
        "additionalProperties": False,
    },
    "hub.unbind": {"type": "object", "properties": {}, "additionalProperties": False},
    "hub.switch": {  # H1 (27 Oct): one-time switch tickets and the rail list over the hub link; see _switch
        "type": "object",
        "properties": {
            "kind": {"enum": ["ticket", "redeem", "rail"]},
            "origin": {"type": "string", "pattern": "^https?://[A-Za-z0-9.\\[\\]:-]{1,200}$"},
            "return_to": {"type": "string", "maxLength": 256},
            "ticket_kind": {"enum": ["open", "signout"]},
            "ticket": {"type": "string", "minLength": 8, "maxLength": 128, "pattern": "^[A-Za-z0-9._-]+$"},
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
    "hub.status": {"type": "object", "properties": {}, "additionalProperties": False},
    "keys.rotate": {"type": "object", "properties": {}, "additionalProperties": False},  # C8: owner, column P only
    "hub.history": {  # C8 M5: what a hub did here; ``since`` starts the third list (default: the last 7 days)
        "type": "object",
        "properties": {
            "hub_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "since": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"},
        },
        "additionalProperties": False,
    },
    "hub.handoff": {  # V3 C11: a read; see host/hubhandoff.py
        "type": "object",
        "properties": {
            "kind": {"enum": ["gate", "verdict", "question", "status", "grant", "addon"]},
            "ticket": {"type": "string", "minLength": 1, "maxLength": 64},
            "gate": {"enum": ["requirements", "plan", "code", "verify"]},
            "question": {"type": "string", "minLength": 1, "maxLength": 64},
            # kind grant (C10): the exact terms Mission Control's "Start agent" waits for; cancel drops the wait
            "scope": {"enum": ["all", "workable"]},
            "verbs": {"const": "agent"},
            "hours": {"type": "integer", "minimum": 1, "maximum": 168},
            "label": {"type": "string", "minLength": 1, "maxLength": 200},
            "cancel": {"const": True},
            # kind addon (FB15): the prepared hand-off request (op addon.grant) the hub link is for
            "request": {"type": "string", "pattern": "^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$"},
        },
        "required": ["kind"],
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


@dataclass(frozen=True)
class HubFacts:
    """What ``hub.join`` checks the hub card against: the host's own clock and whether this is a dev workspace."""

    now_ms: int
    dev_mode: bool


def dev_host(service: WorkspaceService) -> bool:
    """Is this a dev host (full, not pending)? The ONE host-level source of the dev state: the store replays dev hub
    material (and a hub proof's ``dev`` marker) only for it (C3b 15.3, #1198 C9), and :func:`workspace_dev` adds the
    per-workspace sandbox proof on top."""
    dev = getattr(service, "dev", None)
    return dev is not None and not getattr(dev, "pending", False)


def workspace_dev(service: WorkspaceService) -> bool:
    """Is this a dev workspace? Only a dev host (``--dev-sim-convenience``) that proves the workspace is its sandbox
    says yes; every doubt is no, so a real workspace refuses a dev hub (fail closed). Runs on the worker thread."""
    from . import devmode

    dev = getattr(service, "dev", None)
    store = getattr(service, "store", None)
    if not dev_host(service) or store is None:
        return False
    try:
        with store.locked():
            view = devmode.service_view(service)
            return (
                view is not None and devmode.sandbox_reason(dev, service.workspace_id, service.entry.root, view) is None
            )
    except Exception:  # noqa: BLE001 - any doubt: not a dev workspace
        return False


def facts_for(service: WorkspaceService) -> HubFacts:
    return HubFacts(int(service.clock() * 1000), workspace_dev(service))


# -- files ---------------------------------------------------------------------------------------------------------


def _dir(service: WorkspaceService) -> Path:
    return Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "hub"


def _load_state(service: WorkspaceService) -> Any:
    from orch.hubclient import HubRefused, HubState

    from .custody import read_json

    doc = read_json(_dir(service) / "hub.json", service.host_uid, limit=1 << 20)
    if doc is None:
        return None
    try:
        return HubState.from_json(doc)
    except HubRefused:
        raise HostError("hub.state_mismatch", "hub.json is not readable: the host refuses to guess") from None


def _save_state(service: WorkspaceService, state: Any) -> None:
    with _MU:
        _write_state(service, state)


def _write_state(service: WorkspaceService, state: Any) -> None:
    """The write itself; the caller holds ``_MU``."""
    import json

    from orch.store.fsio import write_atomic

    from .custody import ensure_private_dir

    d = _dir(service)
    for level in (d.parent, d):
        ensure_private_dir(level, service.host_uid)
    write_atomic(
        d / "hub.json", json.dumps(state.to_json(), sort_keys=True, separators=(",", ":")).encode(), mode=0o600
    )


def _save_snapshot(service: WorkspaceService, fetched: Any) -> None:
    """Commit what a snapshot refresh learned, without trusting the state it started from (the fetch ran on the network,
    outside any lock). Under ``_MU`` hub.json is read again and checked: still the same bound binding of the same hub,
    else nothing is written (a leave or unbind that ran meanwhile stays). The pin only moves up (a racing higher
    version is never rolled back; the same version with other bytes is ``snapshot_conflict``); the revoked set only
    grows; a newer card is adopted only if it is not older than the one now on disk."""
    from orch.hubclient import HubRefused, wire

    with _MU:
        cur = _load_state(service)
        if (
            cur is None
            or cur.status != "bound"
            or cur.hub_id != fetched.hub_id
            or cur.binding_seq != fetched.binding_seq
        ):
            raise HubRefused("not_joined")
        changed = False
        new = fetched.snapshot
        if new is not None:
            held = None if cur.snapshot is None else cur.snapshot["o"]
            if held is None or new["o"]["version"] > held["version"]:
                cur.snapshot, changed = new, True
            elif new["o"]["version"] == held["version"] and wire.cj(new["o"]) != wire.cj(held):
                raise HubRefused("snapshot_conflict")
        revoked = sorted({*cur.revoked_op_certs, *fetched.revoked_op_certs})
        if revoked != sorted(cur.revoked_op_certs):
            cur.revoked_op_certs, changed = revoked, True
        for cid, obj in fetched.revoked_op_objs.items():  # the signed objects a hub.pinned line carries (15.2)
            if cid not in cur.revoked_op_objs:
                cur.revoked_op_objs[cid], changed = obj, True
        if fetched.card is not None and cur.card != fetched.card:
            on_disk, mine = cur.card, fetched.card
            order = lambda c: (c["op_cert"]["o"]["op_seq"], c["o"]["card_seq"])  # noqa: E731
            if on_disk is None or order(mine) > order(on_disk):
                cur.card, cur.hk_op_pub, cur.tls_spki, changed = mine, fetched.hk_op_pub, fetched.tls_spki, True
            elif order(mine) == order(on_disk):  # same place, other bytes: keep the disk, say so
                raise HubRefused("stale_card")
        if changed:
            _write_state(service, cur)


def _wsk_sign(service: WorkspaceService) -> Any:
    from orch.custody import FileBackend
    from orch.store import BackendSigner

    keys_dir = Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "keys"
    backend = FileBackend(keys_dir) if keys_dir.is_dir() else None
    if backend is None or not backend.exists("wsk"):
        raise HostError("workspace.unavailable", "the workspace signing key is not in the host's custody")
    signer = BackendSigner(backend, "wsk")
    return signer.public_key, signer.sign


def workspace_card(service: WorkspaceService) -> dict[str, Any] | None:
    """The workspace's v2 directory card (hub contract 9.5): its own card (:func:`local_card`). ``None`` only if it
    cannot be had."""
    if CARD_SOURCE is not None:
        return CARD_SOURCE(service)  # type: ignore[no-any-return]
    try:
        return local_card(service)
    except (HostError, OSError, ValueError):
        return None


def local_card(service: WorkspaceService) -> dict[str, Any]:
    """The workspace's card: built once with ``card_o`` (signed by ``WSK``), kept in ``hub/card.json`` with its
    ``WXK``/``CK`` in ``hub/card-keys.json`` (0600, the host's own directory), and returned unchanged on every later
    call."""
    import json
    import secrets

    from orch import crypto
    from orch.hubclient import localcard
    from orch.identity import wscard
    from orch.identity.errors import Refused
    from orch.store.fsio import write_atomic

    from . import hubkeys
    from .custody import ensure_private_dir, read_json

    d = _dir(service)
    pk = hubkeys.owner_pk_pub(service)
    with _MU:
        stored = read_json(d / "card.json", service.host_uid, limit=1 << 20)
        if stored is not None:
            try:
                wscard.verify_card(dict(stored), pk)
            except (Refused, TypeError, ValueError):
                raise HostError(
                    "hub.state_mismatch", "hub/card.json does not verify: the host refuses to guess"
                ) from None
            return stored  # type: ignore[no-any-return]
        genesis = service.store.genesis_event()
        wsk_pub, sign = _wsk_sign(service)
        keys = read_json(d / "card-keys.json", service.host_uid, limit=1 << 16)
        if keys is None:
            keys = {
                "v": 1,
                "wxk": crypto.b64u(crypto.private_scalar(crypto.generate_private_key())),
                "ck": crypto.b64u(secrets.token_bytes(32)),
            }
            for level in (d.parent, d):
                ensure_private_dir(level, service.host_uid)
            write_atomic(d / "card-keys.json", json.dumps(keys, sort_keys=True).encode(), mode=0o600)
        wxk = crypto.private_key_from_scalar(int.from_bytes(crypto.unb64u(keys["wxk"], 32), "big"))
        card = localcard.build_card(
            delegation=genesis["delegation"], owner_pk_pub=pk, workspace_id=service.workspace_id, wsk_pub=wsk_pub,
            sign_wsk=sign, wxk_pub=crypto.public_bytes(wxk), wxk_version=1, ck=crypto.unb64u(keys["ck"], 32),
            card_seq=1, issued_ms=int(service.clock() * 1000), part=wscard_part(service),
        )  # fmt: skip
        write_atomic(d / "card.json", json.dumps(card, sort_keys=True).encode(), mode=0o600)
        return card


def wscard_part(service: WorkspaceService) -> dict[str, Any]:
    from orch.identity.epochs import default_part

    return default_part(str(service.workspace_id)[:8])


class _NoLink:
    """No hub link in this host yet (C5): every send is unreachable."""

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionRefusedError("no hub link")


def _transport(service: WorkspaceService, hub_id: str, link: dict[str, Any] | None = None) -> Any:
    """The seam first (tests, C5's replacements); else a TLS link to ``link["addr"]`` pinned to the card's SPKI hash;
    without an address or pin the hub is simply unreachable."""
    if TRANSPORT is not None:
        return TRANSPORT(service, hub_id)
    if not link or not link.get("addr") or not link.get("spki"):
        return _NoLink()
    from orch.hubclient.tlsadapter import TlsTransport

    return TlsTransport(
        addr=link["addr"], tls_spki_sha256=link["spki"], hub_id=hub_id, workspace_id=service.workspace_id,
        hk_op_pub=link["hk_op"], wsk_pub=link["wsk_pub"], sign=link["sign"], mode=link.get("mode", "selective"),
        now_ms=lambda: int(service.clock() * 1000),
    )  # fmt: skip


def _addr(args: dict[str, Any], state: Any, dev: bool) -> str | None:
    """Where to dial: in a dev workspace ``ORCH_HUB_ADDR`` of the host process wins; else the ``addr`` of this call;
    else the one kept in ``hub.json``. The address is not signed and not trusted: the card's TLS pin decides."""
    env = os.environ.get("ORCH_HUB_ADDR") if dev else None
    return env or args.get("addr") or (None if state is None else state.addr)


def hub_role(service: WorkspaceService, user_id: str) -> str | None:
    """The hub role (``read`` | ``edit`` | ``admin``) of a hub user id from the pinned group snapshot, or ``None``
    (no snapshot, no role, or anything that does not verify now). Worker thread. Phase 3 (V3-D3): for hub-side reads,
    routes and shared spaces only; it never authorises a signature."""
    from orch.hubclient import snapshot

    try:
        state = _load_state(service)
    except HostError:
        return None
    facts = facts_for(service)
    return snapshot.hub_role(state, user_id, now_ms=facts.now_ms, dev_mode=facts.dev_mode)


# -- who may sign here (V3 C11, D78/D81) ---------------------------------------------------------------------------


def signing_mode(service: WorkspaceService) -> str:
    """``standalone`` | ``selective`` | ``full`` | ``unknown``: how this workspace may sign (V3 C11, L2-C11-2).

    **The signed log decides.** The workspace model (a replay of the log) says whether a hub is joined and its mode;
    no ``hub.joined``, or a later ``hub.left``, is ``standalone`` whatever ``hub.json`` says, so a corrupt cache never
    bricks a workspace that never joined. ``hub.json`` can only make the answer **stricter**: a full join whose
    ``hub.json`` is missing, unreadable, or names another hub, binding or mode is ``unknown``, and a selective join
    whose ``hub.json`` cannot be read, or already says ``full`` (the file is ahead of the log), is ``unknown`` too.
    ``unknown`` is treated as ``full`` by every caller (:data:`~orch.host.hubsign.HUB_ONLY`). A selective join that has
    no ``hub.json`` yet (``hub.connect`` not run) stays ``selective``: nothing was handed to the hub.

    Reads the log fresh (``store.refresh()``); safe under the store lock. Never touches the network."""
    store = service.store
    try:
        store.refresh()
        model = dict(store.state.workspace.hub)
    except Exception:  # noqa: BLE001 - the log cannot be read just now: fail closed
        return "unknown"
    if model.get("invalid"):
        return "unknown"  # the log holds an owner hub event the model refused (#1198 C9): never standalone
    if not model or model.get("status") != "joined":
        return "standalone"
    mode = model.get("mode")
    if mode not in ("selective", "full"):
        return "unknown"
    try:
        state = _load_state(service)
    except Exception:  # noqa: BLE001 - HostError (not readable) and any custody error: fail closed
        return "unknown"
    if state is None:
        return "selective" if mode == "selective" else "unknown"
    same = state.hub_id == model.get("hub_id") and state.binding_seq == model.get("binding_seq")
    if mode == "full":
        return (
            "full" if same and state.mode == "full" and state.status in ("bound", "joining", "switching") else "unknown"
        )
    if (
        state.mode == "full"
        and state.status in ("bound", "joining", "switching")
        and state.hub_id == model.get("hub_id")
    ):
        return "unknown"  # hub.json is ahead of the log (a rollback or a replay): never looser than the file
    return "selective"


# -- publish through the hub (V3 C7) ---------------------------------------------------------------------------------


def publish_route(service: WorkspaceService) -> dict[str, Any] | None:
    """Where a publish request of this workspace goes, decided from the signed log and the host's hub state (worker
    thread): ``None`` is the direct path, a dict is the hub (``publish_link`` opens it).

    * The log has no ``hub.joined``, or its newest hub record is ``hub.left``: the workspace is not in hub mode, so
      the direct path stays (until the H6 switch). The old binding is not consulted: leaving ends it.
    * The log says joined: the hub is the **only** path. If ``hub.json`` is missing, is not the joined hub's bound
      binding (same ``hub_id`` and ``binding_seq``, selective), or cannot be read, the request is refused
      (``hub.not_joined`` / ``hub.state_mismatch``); it never falls back to direct, which would bypass the hub."""
    from orch.crypto.encoding import unb64u

    model = dict(service.store.state.workspace.hub)
    if not model or model.get("status") != "joined":
        return None
    state = _load_state(service)
    if (
        state is None
        or state.status != "bound"
        or state.hub_id != model.get("hub_id")
        or state.binding_seq != model.get("binding_seq")
    ):
        raise HostError(
            "hub.not_joined", "this workspace has joined a hub but is not connected to it yet: orch hub connect"
        )
    if state.mode != "selective" or model.get("mode") != "selective":
        raise HostError("hub.state_mismatch", "only selective hub mode publishes today")
    dev = bool(state.binding["o"]["dev"])
    return {
        "hub_id": state.hub_id,
        "hk_op": unb64u(state.hk_op_pub, 65),
        "addr": _addr({}, state, dev),
        "spki": state.tls_spki,
        "mode": state.mode,
    }


def publish_link(service: WorkspaceService, route: dict[str, Any]) -> tuple[Any, Any, bytes, Any]:
    """``(HubLink, transport, wsk_pub, sign)`` for a :func:`publish_route`; the caller closes the transport."""
    from orch.hubclient import HubLink

    wsk_pub, sign = _wsk_sign(service)
    link = {"addr": route["addr"], "spki": route["spki"], "hk_op": route["hk_op"], "wsk_pub": wsk_pub, "sign": sign,
            "mode": route["mode"]}  # fmt: skip
    transport = _transport(service, route["hub_id"], link)
    hub_link = HubLink(
        transport, hub_id=route["hub_id"], workspace_id=service.workspace_id, hk_op_pub=route["hk_op"], sign=sign,
        mode=route["mode"], now_ms=lambda: int(service.clock() * 1000),
    )  # fmt: skip
    return hub_link, transport, wsk_pub, sign


# -- the epoch keys (full mode, C2b) ---------------------------------------------------------------------------------


class _Epochs:
    """:class:`orch.hubclient.EpochKeys` over the workspace's membership key store (R6a). ``WK_e`` never leaves it."""

    def __init__(
        self,
        mk: Any,
        local_owner: str,
        sealed_epoch: Callable[[], int | None] | None = None,
    ) -> None:
        self._mk = mk
        #: the epoch now sealed to the bound full hub (re-read each call); ``view_key`` answers for that one only
        self._sealed_epoch = sealed_epoch
        #: the owner whose list a new local epoch (``MembershipKeys.advance_local``) is made for
        self._local_owner = local_owner

    def ensure_epoch(self) -> None:
        """The genesis epoch of a relay-less workspace, at its first full join (no-op when one exists)."""
        if self._mk.current_epoch() < 1:
            self._mk.advance_local(self._local_owner)

    def current_epoch(self) -> int:
        return int(self._mk.current_epoch())

    def seal_to_hub(self, epoch: int, hxk_pub: bytes, hub_id: str) -> bytes:
        from orch.hubclient import HubRefused
        from orch.identity.epochs import CustodyRefused

        if epoch != self.current_epoch() or epoch < 1:
            raise HubRefused("no_epoch")  # only the committed epoch is ever sealed
        try:
            return bytes(self._mk.sealed_wk(epoch, hxk_pub, hub_id))
        except CustodyRefused:
            raise HubRefused("no_epoch") from None

    def view_key(self, epoch: int) -> bytes:
        """``K_view(epoch)`` for ``log_sync`` (hub contract 10.7): derived inside the key store, so only the derived
        key leaves it. Only the epoch now sealed to the bound full hub (L2-C14-2: never one the hub was not meant to
        open, such as the epoch a rotation after a leave made); a missing, burned or unreadable key is ``no_epoch``."""
        from orch.hubclient import HubRefused
        from orch.identity.epochs import CustodyRefused, MembershipUnsafe

        try:
            sealed = None if self._sealed_epoch is None else self._sealed_epoch()
        except Exception:  # noqa: BLE001 - unreadable hub state: fail closed
            sealed = None
        if sealed is None or sealed != epoch:
            raise HubRefused("no_epoch")
        try:
            return bytes(self._mk.view_key(epoch))
        except (CustodyRefused, MembershipUnsafe):
            raise HubRefused("no_epoch") from None

    def rotate(self) -> None:
        self._mk.advance_local(self._local_owner)


def epochs_for(service: WorkspaceService, *, create: bool = False) -> Any:
    """The key store of a workspace that has one (``membership/head.json`` exists), else ``None``: asking must not
    create the key store. ``create`` (a full join only) lets a workspace that never had a relay open one: it then
    makes its own genesis epoch locally (:meth:`_Epochs.ensure_epoch`). A relay-linked workspace never does."""
    from orch import crypto

    from . import hubkeys

    exists = (hubkeys.membership_path(service) / "head.json").is_file()
    if not exists and not create:
        return None
    try:
        mk = hubkeys.keys_for(service)
        if create and not exists:
            _mark_hub_only(service)
        owner = crypto.person_id(hubkeys.owner_pk_pub(service)).hex()
        return _Epochs(mk, owner, lambda: _sealed_epoch(service))
    except Exception:  # noqa: BLE001 - an unsafe or unreadable key store: no epochs (fail closed)
        return None


def _mark_hub_only(service: Any) -> None:
    """A key store made by a full join where no relay link ever existed is not a relay (B15): mark it, so
    ``readerrecs.relay_level`` does not read it as "relay paired" and One click works after the leave."""
    from pathlib import Path

    from orch.store.readerrecs import HUB_ONLY_MARK

    base = Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id
    if (base / "relay").exists():
        return
    mark = base / "membership" / HUB_ONLY_MARK
    try:
        mark.touch(exist_ok=True)
    except OSError:
        pass


def _sealed_epoch(service: WorkspaceService) -> int | None:
    """The epoch sealed to the bound full hub, else ``None`` (read from hub.json every time)."""
    with _MU:
        st = _load_state(service)
    if st is None or st.status != "bound" or st.mode != "full" or st.sealed is None or st.rotate_after is not None:
        return None
    return int(st.sealed["epoch"])


class _NoEpochs:
    def current_epoch(self) -> int:
        return 0

    def seal_to_hub(self, epoch: int, hxk_pub: bytes, hub_id: str) -> bytes:
        from orch.hubclient import HubRefused

        raise HubRefused("no_epoch")

    def rotate(self) -> None:
        return None


# -- checks --------------------------------------------------------------------------------------------------------


def _validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


def _owner(call: Call) -> str:
    """Column P only (``op.not_allowed`` for an agent) and the person must be a workspace owner."""
    if call.column.name != "P" or call.viewer is None:
        raise HostError("op.not_allowed", "a person does this; an agent never joins or leaves a hub")
    member = call.service.store.state.workspace.members.get(call.viewer)
    if member is None or member.role != "owner":
        raise HostError("role.denied", "joining or leaving a hub is for workspace owners")
    return call.viewer


def _refused(code: str) -> HostError:
    if code == "hub_unreachable":
        return HostError("hub.unreachable", "the hub could not be reached; nothing was lost", retry_after_ms=2000)
    if code == "hub_address_changed":
        return HostError(
            "hub.refused",
            "the hub's address changed: make a new link in the hub console",
            hint="make a new link in the hub console (Admin > Workspaces > Connect a workspace)",
        )
    if code == "no_epoch":
        return HostError("hub.no_epoch", "this workspace has no epoch key to give the hub (link a relay first)")
    if code == "rotation_pending":
        return HostError(
            "hub.rotation_pending", "the workspace key of the hub you left is not replaced yet; try again shortly"
        )
    if code in ("proof_expired", "proof_invalid", "proof_missing"):
        # #1198 C9: the owner proof of the join lives 10 minutes. The log already says joined, but the hub never took
        # the binding; readers refuse a second join while bound, so the way out is a leave, then a new join.
        return HostError(
            "hub.refused",
            f"the hub is refused: {code}: the proof of your backup key behind this join is too old or not valid, "
            "so the hub did not take it",
            hint="leave and join again with your backup key: orch hub leave, then orch hub join",
            fix=["orch", "hub", "leave"],
        )
    return HostError("hub.refused", f"the hub is refused: {code}")


def _audit(call: Call, event: str, **more: Any) -> None:
    from . import securitylog

    s = call.service
    securitylog.record(
        s.state_dir, s.host_uid, s.clock(), event, workspace_id=s.workspace_id, principal=call.principal, **more
    )


def _log_sync_status(service: WorkspaceService) -> dict[str, Any] | None:
    """The ``log_sync`` sender's state for ``hub.status`` (``None``: no sender), including a log it stopped syncing
    because a line is bigger than a page (``stuck``: log -> seq of that line)."""
    from . import logsyncops

    st = logsyncops.status(service)
    if st is None:
        return None
    return {"running": st["running"], "last_error": st["last_error"], "stuck": st["stuck"]}


def _reseal_needed(service: WorkspaceService, state: Any) -> bool:
    """A bound full hub whose newest sealed epoch is older than the committed one (a routine rotation happened): it
    cannot read new content until ``orch hub connect`` seals the new epoch to it."""
    if state is None or state.mode != "full" or state.status != "bound":
        return False
    epochs = epochs_for(service)
    if epochs is None:
        return False
    return int(epochs.current_epoch()) > (state.sealed["epoch"] if state.sealed else 0)


def _rotation(service: WorkspaceService, state: Any) -> str:
    """``none`` | ``pending`` (a leave from full mode owes a new epoch) | ``done`` is never kept: it clears itself."""
    if state is None or state.rotate_after is None:
        return "none"
    epochs = epochs_for(service)
    return "none" if epochs is not None and epochs.current_epoch() > state.rotate_after else "pending"


# -- the worker half -----------------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    from orch.crypto.encoding import b64u, unb64u
    from orch.hubclient import HubRefused, wire
    from orch.hubclient.tlsadapter import parse_addr

    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    _validate(op, args)
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    if (call.frame.get("opts") or {}).get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    if op in NETWORK_OPS and service.state == "degraded":
        raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    service.store.refresh()
    _owner(call)
    if op == "keys.rotate":  # C8: no hub, no relay, no event: the owner asks the key store for a new epoch
        from . import breakglass

        out = breakglass.rotate(call, epochs_for(service))
        _audit(call, "keys.rotate")
        return out
    if op == "hub.link":
        return {"plan": {"op": "hub.link", "link": args["link"], "dev": workspace_dev(service)}}
    model = dict(service.store.state.workspace.hub)
    state = _load_state(service)
    if op == "hub.history":
        from . import breakglass

        hub_id = args.get("hub_id") or model.get("hub_id")
        if not isinstance(hub_id, str):
            raise HostError("hub.not_joined", "this workspace never joined a hub")
        return ok_result(breakglass.history(service, hub_id, args.get("since")))
    if op == "hub.handoff":
        from .hubhandoff import handoff

        return handoff(call, args, state)
    if op == "hub.switch":
        if state is None or state.status != "bound":
            raise HostError("hub.not_joined", "this workspace is not connected to a hub")
        need = {"ticket": ("origin", "return_to", "ticket_kind"), "redeem": ("ticket",), "rail": ("origin",)}[
            args["kind"]
        ]
        if set(args) - {"kind"} != set(need):
            raise HostError("invalid.input", f"hub.switch {args['kind']} takes exactly: {', '.join(need)}")
        return {"plan": {"op": op, "args": args, "state": state, "dev": workspace_dev(service)}}
    if op == "hub.status":
        from orch.hubclient import snapshot

        from . import hublinkprobe

        binding = None if state is None else state.status
        up = None
        if binding == "bound":
            up = hublinkprobe.reachable(
                f"{service.state_dir}:{service.workspace_id}", _addr({}, state, workspace_dev(service))
            )
        return ok_result(
            {
                "snapshot": snapshot.pinned_view(state),
                "joined": bool(model) and model.get("status") == "joined",
                "hub_id": model.get("hub_id"),
                "mode": model.get("mode"),
                "signing": signing_mode(
                    service
                ),  # V3 C11: standalone | selective | full | unknown (full takes no local key)
                "binding_seq": model.get("binding_seq"),
                "member": bool(model.get("member")),  # full mode: the hub is a member of kind hub
                "sealed_epoch": None if state is None or not state.sealed else state.sealed["epoch"],
                "rotation": _rotation(service, state),
                "reseal_needed": _reseal_needed(service, state),
                "link": hublinkprobe.link_view(
                    binding, up
                ),  # V3 D78: "unreachable" when bound but the hub does not answer
                "binding": binding,
                "hub_card": None if state is None else state.card,  # public; the dashboard's one-click mode switch
                "reachable": up,
                **hublinkprobe.peer_view(service),  # peer_outbox, link_error
                "log_sync": _log_sync_status(service),
                "unbound_pending": state is not None and bool(state.outbox),
                "owed": [] if state is None else sorted(state.outbox),
            }
        )
    plan: dict[str, Any] = {"op": op, "model": model, "state": state}
    if op == "hub.refresh":
        if (
            state is None
            or state.status != "bound"
            or state.hub_id != model.get("hub_id")
            or model.get("status") != "joined"
        ):
            raise HostError("hub.not_joined", "this workspace is not connected to a hub: orch hub connect")
        plan.update(dev=workspace_dev(service), hub_card=args.get("hub_card"))
        return {"plan": plan}
    if op == "hub.unbind":
        if state is not None and (state.outbox or state.rotate_after is not None or state.publish_owed):
            return {"plan": plan}  # a signed unbound or a rotation is still owed, whatever the model says now
        if not model or model.get("status") != "left":
            raise HostError("hub.not_joined", "sign `orch hub leave` first: the log has no hub.left")
        return {"plan": plan}
    if not model or model.get("status") != "joined":
        raise HostError("hub.not_joined", "sign `orch hub join` first: the log has no hub.joined")
    if model.get("mode") == "full":
        hubsign.refuse_full_while_click_on(service)  # V3 C11: a full hub never runs beside click-to-sign
    dev = workspace_dev(service)
    now = int(service.clock() * 1000)
    try:
        verified = wire.verify_card(
            args["hub_card"], now_ms=now, dev_mode=dev, pinned_root=unb64u(model["hk_root_pub"], 65)
        )
        if "addr" in args:
            parse_addr(args["addr"])
    except HubRefused as exc:
        raise _refused(exc.code) from None
    v2 = workspace_card(service)
    if v2 is None:
        raise HostError("hub.no_card", "this workspace's directory card could not be had")
    from . import hubkeys

    kind = "join"
    if "invite" not in args:  # no invite: a mode switch, or a re-seal of a bound full hub (never a new join)
        same = state is not None and state.hub_id == model["hub_id"] and model["mode"] == "full"
        if same and state.status == "bound" and state.mode == "full" and state.binding_seq == model["binding_seq"]:
            kind = "seal"
        elif same and (
            (state.status == "bound" and state.mode == "selective" and state.binding_seq < model["binding_seq"])
            or (state.status == "switching" and state.binding_seq == model["binding_seq"])
        ):
            kind = "switch"
        else:
            raise HostError("invalid.input", "hub.connect needs the invite code for a new join")
    plan.update(
        kind=kind,
        invite=args.get("invite"),
        hxk=verified["card"].get("hxk_pub"),
        card=args["hub_card"],
        v2=v2,
        dev=dev,
        addr=_addr(args, state, dev),
        spki=verified["card"]["tls_spki_sha256"],
        hk_op=verified["hk_op_pub"],
        pk_pub=b64u(hubkeys.owner_pk_pub(service)),
    )
    return {"plan": plan}


# -- the network half ----------------------------------------------------------------------------------------------


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    if "plan" not in planned:
        return planned
    return await asyncio.to_thread(_run, call, planned["plan"])


def _run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch.hubclient import HubRefused, flush_outbox, leave

    from .workspace import ok_result

    service = call.service
    if plan["op"] == "hub.link":
        from orch.hubclient import connectlink

        try:
            return ok_result(
                connectlink.resolve(plan["link"], now_ms=int(service.clock() * 1000), dev_mode=plan["dev"])
            )
        except HubRefused as exc:
            raise _refused(exc.code) from None
    wsk_pub, sign = _wsk_sign(service)
    if plan["op"] == "hub.switch":
        try:
            return ok_result(_switch(service, plan, wsk_pub, sign))
        except HubRefused as exc:
            raise _refused(exc.code) from None
    now = lambda: int(service.clock() * 1000)  # noqa: E731
    save = lambda s: _save_state(service, s)  # noqa: E731
    model, state = plan["model"], plan["state"]
    _quiesce_logsync(service, plan)
    try:
        if plan["op"] == "hub.connect":
            aliases = [{"person_id": p, "user_id": u} for p, u in sorted(model.get("aliases", {}).items())]
            link = {
                "addr": plan["addr"],
                "spki": plan["spki"],
                "hk_op": plan["hk_op"],
                "wsk_pub": wsk_pub,
                "sign": sign,
                "mode": model["mode"],  # the link carries wk_seal only in full mode
            }
            transport = _transport(service, model["hub_id"], link)
            try:
                r = _connect(state, plan, model, service, wsk_pub, sign, now, save, transport, aliases)
            finally:
                getattr(transport, "close", lambda: None)()
            _stop_peers(service)  # a (re)binding: ws->ws restarts on the new binding at its next use
            _audit(call, "hub.connect", hub_id=r.hub_id, binding_seq=r.binding_seq, mode=r.mode, kind=plan["kind"])
            out = {"hub_id": r.hub_id, "status": r.status, "binding_seq": r.binding_seq, "mode": r.mode,
                   "sealed_epoch": None if not r.sealed else r.sealed["epoch"]}  # fmt: skip
            out["snapshot_version"] = _first_snapshot(call, plan, r, wsk_pub, sign, now, save)
            return ok_result(out)
        if plan["op"] == "hub.refresh":
            return ok_result(_refresh(call, plan, state, wsk_pub, sign, now, save))
        out = _unbind(call, plan, state, sign, wsk_pub, now, save, leave, flush_outbox)
        _stop_peers(service)  # unbound: the ws->ws link and receive thread end here
        return ok_result(out)
    except HubRefused as exc:
        _audit(call, "hub.refused", op=plan["op"], code=exc.code)
        raise _refused(exc.code) from None


_SWITCH_OPS = {"ticket": "switch_ticket", "redeem": "switch_redeem", "rail": "rail_get"}


def _switch(service: Any, plan: dict[str, Any], wsk_pub: bytes, sign: Any) -> dict[str, Any]:
    """``hub.switch`` (H1, 27 Oct): the hub decides the person (the owner alias of this workspace), the host relays.
    ``ticket``: a one-time ticket to open (or sign out of) the hub console; ``redeem``: the hub answers for a ticket the
    hub console made for THIS workspace (single use, 30 s, audience = this authenticated link); ``rail``: the workspace
    list. Nothing here is logged, signed by a person or kept."""
    a = plan["args"]
    hub_link, transport = _snapshot_link(service, plan["state"], wsk_pub, sign, plan["dev"])
    try:
        if a["kind"] == "ticket":
            r = hub_link.call(
                "switch_ticket", {"origin": a["origin"], "return_to": a["return_to"], "kind": a["ticket_kind"]}
            )
        elif a["kind"] == "redeem":
            r = hub_link.call("switch_redeem", {"ticket": a["ticket"]})
        else:
            r = hub_link.call("rail_get", {"origin": a["origin"]})
    finally:
        getattr(transport, "close", lambda: None)()
    return r


def _quiesce_logsync(service: Any, plan: dict[str, Any]) -> None:
    """The ONE rule (L2-C14-1): every hub op calls this before it sends. The log_sync sender is stopped, and waited for
    (bounded), for everything that can change the hub, binding or mode it was started for: ``hub.unbind`` and every
    ``hub.connect`` except a plain re-seal of the same binding. ``hub.refresh`` changes none of them."""
    from . import logsyncops

    if plan["op"] == "hub.unbind" or (plan["op"] == "hub.connect" and plan.get("kind") != "seal"):
        logsyncops.stop(service, wait=logsyncops.STOP_WAIT_S)


def _stop_peers(service: Any) -> None:
    from . import logsyncops, peerops

    peerops.stop(service)
    logsyncops.start(service)  # V3 C14: eligible (full, bound, sealed) -> a pass now; else the sender ends


def _snapshot_link(service: Any, state: Any, wsk_pub: bytes, sign: Any, dev: bool) -> tuple[Any, Any]:
    """``(HubLink, transport)`` for the bound hub of ``state`` (the caller closes the transport)."""
    from orch.crypto.encoding import unb64u
    from orch.hubclient import HubLink

    hk_op = unb64u(state.hk_op_pub, 65)
    link = {"addr": _addr({}, state, dev), "spki": state.tls_spki, "hk_op": hk_op, "wsk_pub": wsk_pub, "sign": sign,
            "mode": state.mode}  # fmt: skip
    transport = _transport(service, state.hub_id, link)
    hub_link = HubLink(
        transport, hub_id=state.hub_id, workspace_id=service.workspace_id, hk_op_pub=hk_op, sign=sign,
        mode=state.mode, now_ms=lambda: int(service.clock() * 1000),
    )  # fmt: skip
    return hub_link, transport


def _refresh(
    call: Call, plan: dict[str, Any], state: Any, wsk_pub: bytes, sign: Any, now: Any, save: Any
) -> dict[str, Any]:
    from orch.hubclient import snapshot

    service = call.service
    adopted = False
    if plan.get("hub_card") is not None:  # a rotated card first: the link below dials its pin and trusts its op key
        adopted = snapshot.adopt_card(state, plan["hub_card"], now_ms=now(), dev_mode=plan["dev"])
    hub_link, transport = _snapshot_link(service, state, wsk_pub, sign, plan["dev"])
    try:
        changed = snapshot.refresh(
            state=state, link=hub_link, workspace_id=service.workspace_id, dev_mode=plan["dev"], now_ms=now,
            save=lambda fetched: _save_snapshot(service, fetched), card_adopted=adopted,
        )  # fmt: skip
    finally:
        getattr(transport, "close", lambda: None)()
    view = snapshot.pinned_view(_load_state(service))  # what is on disk now, not the copy this call started from
    _audit(call, "hub.snapshot", hub_id=state.hub_id, version=(view or {}).get("version"), changed=changed)
    return {"hub_id": state.hub_id, "changed": changed, "snapshot": view}


def _first_snapshot(
    call: Call, plan: dict[str, Any], state: Any, wsk_pub: bytes, sign: Any, now: Any, save: Any
) -> Any:
    """After a join, fetch the first snapshot. The join already happened: a hub that is down or sends a snapshot that
    does not verify leaves no pin (audited), it does not undo the connect."""
    from orch.hubclient import HubRefused

    try:
        return _refresh(call, {"dev": plan["dev"]}, state, wsk_pub, sign, now, save)["snapshot"]["version"]
    except HubRefused as exc:
        _audit(call, "hub.snapshot_refused", hub_id=state.hub_id, code=exc.code)
        return None


def _transport_for(service: Any, wsk_pub: bytes, sign: Any) -> Any:
    """``(hub_id, outbox entry) -> transport``: always the link to THAT hub (its own address and pin)."""
    from orch.crypto.encoding import unb64u

    def make(hub_id: str, e: dict[str, Any]) -> Any:
        link = {"addr": e["addr"],  # the entry's own address wins: ORCH_HUB_ADDR is for the hub being joined only
                 "spki": e["tls_spki"],
                "hk_op": unb64u(e["hk_op_pub"], 65), "wsk_pub": wsk_pub, "sign": sign, "mode": e["mode"]}  # fmt: skip
        return _transport(service, hub_id, link)

    return make


def _connect(
    state: Any,
    plan: dict[str, Any],
    model: dict[str, Any],
    service: Any,
    wsk_pub: bytes,
    sign: Any,
    now: Any,
    save: Any,
    transport: Any,
    aliases: list[dict[str, str]],
) -> Any:
    """Selective: the C2a join. Full: join + seal, mode switch + seal, or a repeat of the seal. Returns the state."""
    from orch.hubclient import join_full, seal_to_hub, switch_full

    if model["mode"] == "selective":
        # an earlier FULL state is retired inside the join (rotation owed); selective itself never touches epochs
        old_epochs = epochs_for(service) if state is not None and state.mode == "full" else None
        return _join(state, plan, model, service, wsk_pub, sign, now, save, transport, aliases, old_epochs).state
    epochs = epochs_for(service, create=True)
    if epochs is None:
        raise _refused("no_epoch")
    try:
        epochs.ensure_epoch()  # relay-less: the workspace's own first epoch; relay-linked without one: no_epoch below
    except Exception:  # noqa: BLE001 - the key store refused (unsafe, linked, exposed): nothing to give the hub
        raise _refused("no_epoch") from None
    if plan["kind"] == "seal":
        seal_to_hub(
            state=state, hub_card=plan["card"], workspace_id=service.workspace_id, sign_wsk=sign, dev_mode=plan["dev"],
            now_ms=now, transport=transport, save=save, epochs=epochs,
        )  # fmt: skip
        return state
    if plan["kind"] == "switch":
        return switch_full(
            state=state, hub_card=plan["card"], workspace_id=service.workspace_id, wsk_pub=wsk_pub, sign_wsk=sign,
            dev_mode=plan["dev"], now_ms=now, transport=transport, save=save, binding_seq=model["binding_seq"],
            epochs=epochs, owner_proof=model.get("owner_proof"),
        )  # fmt: skip
    from orch.crypto.encoding import unb64u

    return join_full(
        state=state, invite=plan["invite"], hub_card=plan["card"], v2_card=plan["v2"],
        pk_pub=unb64u(plan["pk_pub"], 65), workspace_id=service.workspace_id, wsk_pub=wsk_pub, sign_wsk=sign,
        dev_mode=plan["dev"], now_ms=now, transport=transport, addr=plan["addr"],
        record_joined=lambda _payload: None, save=save, aliases=aliases, binding_seq=model["binding_seq"],
        old_transport=_transport_for(service, wsk_pub, sign), epochs=epochs, owner_proof=model.get("owner_proof"),
    ).state  # fmt: skip


def _join(
    state: Any,
    plan: dict[str, Any],
    model: dict[str, Any],
    service: Any,
    wsk_pub: bytes,
    sign: Any,
    now: Any,
    save: Any,
    transport: Any,
    aliases: list[dict[str, str]],
    epochs: Any = None,
) -> Any:
    from orch.crypto.encoding import unb64u
    from orch.hubclient import join_selective

    return join_selective(
        state=state, invite=plan["invite"], hub_card=plan["card"], v2_card=plan["v2"],
        pk_pub=unb64u(plan["pk_pub"], 65), workspace_id=service.workspace_id, wsk_pub=wsk_pub, sign_wsk=sign,
        dev_mode=plan["dev"], now_ms=now, transport=transport, addr=plan["addr"],
        record_joined=lambda _payload: None,  # hub.joined is already on the log (checked in serve)
        save=save, aliases=aliases, mode=model["mode"], binding_seq=model["binding_seq"],
        old_transport=_transport_for(service, wsk_pub, sign), epochs=epochs, owner_proof=model.get("owner_proof"),
    )  # fmt: skip


def _clear_relay_mark(service: Any) -> None:
    """A completed hub leave (no rotation owed) removes the ``membership/`` store a full join made in a workspace that
    never had a relay, and its stored level: ``readerrecs.relay_level`` read it as "relay paired" and refused every
    convenience signature (B12). A workspace with a relay link, or a former one, keeps all of it."""
    import shutil
    from pathlib import Path

    from orch.store.readerrecs import clear_relay_level

    from . import hubkeys

    base = Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id
    relay = base / "relay"
    try:
        names = os.listdir(relay)
    except FileNotFoundError:
        names = []
    except OSError:
        return  # cannot tell: a relay may exist
    if "link.json" in names or any(n.startswith("unlinked-") and n.endswith(".json") for n in names):
        return
    hubkeys.close(service)
    shutil.rmtree(base / "membership", ignore_errors=True)
    clear_relay_level(service.state_dir, service.workspace_id)


def _unbind(
    call: Call, plan: dict[str, Any], state: Any, sign: Any, wsk_pub: Any, now: Any, save: Any, leave: Any, flush: Any
) -> dict[str, Any]:
    service, model = call.service, plan["model"]
    if state is None:  # never connected: there is no binding anywhere to end
        return {"hub_id": model["hub_id"], "unbound": False, "delivered": True, "reason": "never_connected", "owed": []}
    tf = _transport_for(service, wsk_pub, sign)
    epochs = epochs_for(service) if state.mode == "full" or state.rotate_after is not None else None
    from orch.hubclient import flush_publish

    from . import breakglass

    put = breakglass.publish_put(service)
    if model.get("status") == "left" and state.status != "unbound":
        leave(
            state=state, workspace_id=service.workspace_id, sign_wsk=sign,
            dev_mode=bool(state.binding["o"]["dev"]),  # the unbound keeps the binding's own dev marker
            now_ms=now, transport_for=tf, record_left=lambda _p: None, save=save, epochs=epochs,
            publish_put=put,
        )  # fmt: skip
    else:
        from orch.hubclient import retire_full_state, settle_rotation

        flush_publish(state=state, put=put, save=save)  # publish first, then the hub

        if state.status == "bound" and state.mode == "full" and model.get("binding_seq", 0) > state.binding_seq:
            retire_full_state(state, epochs, save, "unbind")  # the log moved on to a newer binding: this one ended
        flush(state=state, workspace_id=service.workspace_id, sign_wsk=sign, now_ms=now, transport_for=tf, save=save)
        if state.rotate_after is not None:  # a full hub was left: ask again, record when the new epoch is there
            settle_rotation(state=state, epochs=epochs, save=save, request=True)
    owed = state.rotate_after is not None
    if model.get("status") == "left" and not owed:
        _clear_relay_mark(service)  # B12: the full join's key store is no relay; One click must work again
    out = {
        # not "unbound" while a key rotation is owed: the leave is not finished until the hub's key is replaced
        "hub_id": state.hub_id, "unbound": not owed, "binding_ended": True, "delivered": not state.outbox,
        "owed": sorted(state.outbox), "rotation": "pending" if owed else "none",
        "publish_told": not state.publish_owed,
    }  # fmt: skip
    if model.get("status") == "left":  # M5: the review comes with the leave, hub-made policy and grants first
        out["review"] = breakglass.history(service, state.hub_id)
    _audit(call, "hub.unbind", hub_id=state.hub_id, delivered=out["delivered"])
    return out

"""The host's hub operations (V3 C2a; hub contract section 9, spec 8 and 8a). Selective mode only.

The owner's signed record is a person op (``hub.join`` -> ``hub.joined``, ``hub.leave`` -> ``hub.left``, see
:mod:`orch.ops.person_ops`); this module is the half that needs the host's keys and the network:

* ``hub.connect {invite, hub_card}``: after ``hub.joined`` is on the log, sign the workspace's ``hub.binding`` with
  ``WSK`` and send ``join`` (card, owner key, binding) over the hub link.
* ``hub.unbind {}``: after ``hub.left`` is on the log, sign ``hub.unbound`` with ``WSK`` and offer it to the hub; the
  hub being down is not an error (it stays queued in ``hub.json`` and is offered again).
* ``hub.status {}``: the model's hub and the host's link state.

Column P only. Nothing here handles ``WK_e`` or adds a member; the link refuses ``wk_seal`` in selective mode.
The workspace is a *dev* workspace only on a dev host that proves it (the same proof as the Simulator gate); every
other workspace refuses a dev hub (hub contract 2.4). The real TLS link is C5: :data:`TRANSPORT` is its seam.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["HUB_CAPS", "HUB_OPS", "NETWORK_OPS", "HubFacts", "execute", "facts_for", "serve", "workspace_dev"]

HUB_OPS: dict[str, int] = {"hub.connect": 1, "hub.unbind": 1, "hub.status": 1}
NETWORK_OPS = frozenset({"hub.connect", "hub.unbind"})
HUB_CAPS = ["hub/1"]
_MU = threading.Lock()

#: C5's seam: ``(service, hub_id) -> HubTransport``, the link to the hub the workspace pinned.
#: ``None``: no link yet, so the hub is unreachable.
TRANSPORT: Any = None
#: the workspace's current v2 directory card (hub contract 9.5), or ``None``; tests replace it
CARD_SOURCE: Any = None

_SCHEMAS: dict[str, dict[str, Any]] = {
    "hub.connect": {
        "type": "object",
        "properties": {"invite": {"type": "string", "minLength": 1, "maxLength": 512}, "hub_card": {"type": "object"}},
        "required": ["invite", "hub_card"],
        "additionalProperties": False,
    },
    "hub.unbind": {"type": "object", "properties": {}, "additionalProperties": False},
    "hub.status": {"type": "object", "properties": {}, "additionalProperties": False},
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


@dataclass(frozen=True)
class HubFacts:
    """What ``hub.join`` checks the hub card against: the host's own clock and whether this is a dev workspace."""

    now_ms: int
    dev_mode: bool


def workspace_dev(service: WorkspaceService) -> bool:
    """Is this a dev workspace? Only a dev host (``--dev-sim-convenience``) that proves the workspace is its sandbox
    says yes; every doubt is no, so a real workspace refuses a dev hub (fail closed). Runs on the worker thread."""
    from . import devmode

    dev = getattr(service, "dev", None)
    store = getattr(service, "store", None)
    if dev is None or store is None:
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
    from orch.hub import HubRefused, HubState

    from .custody import read_json

    doc = read_json(_dir(service) / "hub.json", service.host_uid, limit=1 << 20)
    if doc is None:
        return None
    try:
        return HubState.from_json(doc)
    except HubRefused:
        raise HostError("hub.state_mismatch", "hub.json is not readable: the host refuses to guess") from None


def _save_state(service: WorkspaceService, state: Any) -> None:
    import json

    from orch.store.fsio import write_atomic

    from .custody import ensure_private_dir

    with _MU:
        d = _dir(service)
        for level in (d.parent, d):
            ensure_private_dir(level, service.host_uid)
        write_atomic(
            d / "hub.json", json.dumps(state.to_json(), sort_keys=True, separators=(",", ":")).encode(), mode=0o600
        )


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
    """The workspace's newest v2 directory card (the relay card of the first link, R3/R6), or ``None``."""
    if CARD_SOURCE is not None:
        return CARD_SOURCE(service)  # type: ignore[no-any-return]
    try:
        from . import relayr6

        mk = relayr6.keys_for(service)
        seq = mk.head()["card"]["card_seq"]
        return mk._read_card(seq) if seq else None  # noqa: SLF001 - the one reader of the stored card
    except Exception:  # noqa: BLE001 - no relay state: no card
        return None


class _NoLink:
    """No hub link in this host yet (C5): every send is unreachable."""

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionRefusedError("no hub link")


def _transport(service: WorkspaceService, hub_id: str) -> Any:
    return TRANSPORT(service, hub_id) if TRANSPORT is not None else _NoLink()


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
    return HostError("hub.refused", f"the hub is refused: {code}")


def _audit(call: Call, event: str, **more: Any) -> None:
    from . import securitylog

    s = call.service
    securitylog.record(
        s.state_dir, s.host_uid, s.clock(), event, workspace_id=s.workspace_id, principal=call.principal, **more
    )


# -- the worker half -----------------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    from orch.crypto.encoding import b64u, unb64u
    from orch.hub import HubRefused, wire

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
    model = dict(service.store.state.workspace.hub)
    state = _load_state(service)
    if op == "hub.status":
        return ok_result(
            {
                "joined": bool(model) and model.get("status") == "joined",
                "hub_id": model.get("hub_id"),
                "mode": model.get("mode"),
                "binding_seq": model.get("binding_seq"),
                "link": None if state is None else state.status,
                "unbound_pending": state is not None and state.pending_unbound is not None,
            }
        )
    plan: dict[str, Any] = {"op": op, "model": model, "state": state}
    if op == "hub.unbind":
        if not model or model.get("status") != "left":
            raise HostError("hub.not_joined", "sign `orch hub leave` first: the log has no hub.left")
        return {"plan": plan}
    if not model or model.get("status") != "joined":
        raise HostError("hub.not_joined", "sign `orch hub join` first: the log has no hub.joined")
    dev = workspace_dev(service)
    now = int(service.clock() * 1000)
    try:
        wire.verify_card(args["hub_card"], now_ms=now, dev_mode=dev, pinned_root=unb64u(model["hk_root_pub"], 65))
    except HubRefused as exc:
        raise _refused(exc.code) from None
    v2 = workspace_card(service)
    if v2 is None:
        raise HostError("hub.no_card", "this workspace has no directory card yet (link a relay first)")
    from . import relayr3

    plan.update(
        invite=args["invite"],
        card=args["hub_card"],
        v2=v2,
        dev=dev,
        pk_pub=b64u(relayr3.owner_pk_pub(service)),
    )
    return {"plan": plan}


# -- the network half ----------------------------------------------------------------------------------------------


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    if "plan" not in planned:
        return planned
    return await asyncio.to_thread(_run, call, planned["plan"])


def _run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch.crypto.encoding import unb64u
    from orch.hub import HubRefused, flush_unbound, join_selective, leave

    from .workspace import ok_result

    service = call.service
    wsk_pub, sign = _wsk_sign(service)
    now = lambda: int(service.clock() * 1000)  # noqa: E731
    save = lambda s: _save_state(service, s)  # noqa: E731
    model, state = plan["model"], plan["state"]
    try:
        if plan["op"] == "hub.connect":
            aliases = [{"person_id": p, "user_id": u} for p, u in sorted(model.get("aliases", {}).items())]
            r = join_selective(
                state=state, invite=plan["invite"], hub_card=plan["card"], v2_card=plan["v2"],
                pk_pub=unb64u(plan["pk_pub"], 65), workspace_id=service.workspace_id, wsk_pub=wsk_pub, sign_wsk=sign,
                dev_mode=plan["dev"], now_ms=now, transport=_transport(service, model["hub_id"]),
                record_joined=lambda _payload: None,  # hub.joined is already on the log (checked in serve)
                save=save, aliases=aliases, mode=model["mode"], binding_seq=model["binding_seq"],
            )  # fmt: skip
            _audit(call, "hub.connect", hub_id=r.state.hub_id, binding_seq=r.state.binding_seq)
            return ok_result({"hub_id": r.state.hub_id, "status": r.state.status, "binding_seq": r.state.binding_seq})
        return ok_result(_unbind(call, plan, state, sign, now, save, leave, flush_unbound))
    except HubRefused as exc:
        _audit(call, "hub.refused", op=plan["op"], code=exc.code)
        raise _refused(exc.code) from None


def _unbind(
    call: Call, plan: dict[str, Any], state: Any, sign: Any, now: Any, save: Any, leave: Any, flush: Any
) -> dict[str, Any]:
    from orch.hub import HubRefused

    service, model = call.service, plan["model"]
    if state is None:  # never connected: there is no binding anywhere to end
        return {"hub_id": model["hub_id"], "unbound": False, "delivered": True, "reason": "never_connected"}
    t = _transport(service, state.hub_id)
    dev = bool(state.binding["o"]["dev"])  # the unbound keeps the binding's own dev marker
    try:
        r = leave(
            state=state, workspace_id=service.workspace_id, sign_wsk=sign, dev_mode=dev, now_ms=now,
            transport=t, record_left=lambda _p: None, save=save,
        )  # fmt: skip
        out = {"hub_id": state.hub_id, "unbound": True, "delivered": r.delivered}
    except HubRefused as exc:
        if exc.code != "not_joined":
            raise
        done = flush(state=state, workspace_id=service.workspace_id, sign_wsk=sign, now_ms=now, transport=t, save=save)
        out = {"hub_id": state.hub_id, "unbound": True, "delivered": done}
    _audit(call, "hub.unbind", hub_id=state.hub_id, delivered=out["delivered"])
    return out

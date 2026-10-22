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
        "properties": {
            "invite": {"type": "string", "minLength": 1, "maxLength": 512},
            "hub_card": {"type": "object"},
            "addr": {"type": "string", "minLength": 3, "maxLength": 300},
        },
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
    """The workspace's v2 directory card (hub contract 9.5): the relay's card when the workspace is linked to one
    (R3/R6), else its own relay-less card (:func:`local_card`). ``None`` only if neither can be had."""
    if CARD_SOURCE is not None:
        return CARD_SOURCE(service)  # type: ignore[no-any-return]
    try:
        from . import relayr6

        mk = relayr6.keys_for(service)
        seq = mk.head()["card"]["card_seq"]
        if seq:
            return mk._read_card(seq)  # type: ignore[no-any-return]  # noqa: SLF001 - the one reader of the stored card
    except Exception:  # noqa: BLE001 - no relay state: build our own
        pass
    try:
        return local_card(service)
    except (HostError, OSError, ValueError):
        return None


def local_card(service: WorkspaceService) -> dict[str, Any]:
    """The relay-less card: built once with the relay path's own builder (``card_o``, signed by ``WSK``), kept in
    ``hub/card.json`` with its ``WXK``/``CK`` in ``hub/card-keys.json`` (0600, the host's own directory), and returned
    unchanged on every later call. The relay path never sees these files, so a later relay link is unaffected."""
    import json
    import secrets

    from orch import crypto
    from orch.hubclient import localcard
    from orch.identity import wscard
    from orch.identity.errors import Refused
    from orch.store.fsio import write_atomic

    from . import relayr3
    from .custody import ensure_private_dir, read_json

    d = _dir(service)
    pk = relayr3.owner_pk_pub(service)
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
    from orch.relay.r6.firstcard import default_part

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
        hk_op_pub=link["hk_op"], wsk_pub=link["wsk_pub"], sign=link["sign"],
        now_ms=lambda: int(service.clock() * 1000),
    )  # fmt: skip


def _addr(args: dict[str, Any], state: Any, dev: bool) -> str | None:
    """Where to dial: in a dev workspace ``ORCH_HUB_ADDR`` of the host process wins; else the ``addr`` of this call;
    else the one kept in ``hub.json``. The address is not signed and not trusted: the card's TLS pin decides."""
    env = os.environ.get("ORCH_HUB_ADDR") if dev else None
    return env or args.get("addr") or (None if state is None else state.addr)


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
    from . import relayr3

    plan.update(
        invite=args["invite"],
        card=args["hub_card"],
        v2=v2,
        dev=dev,
        addr=_addr(args, state, dev),
        spki=verified["card"]["tls_spki_sha256"],
        hk_op=verified["hk_op_pub"],
        pk_pub=b64u(relayr3.owner_pk_pub(service)),
    )
    return {"plan": plan}


# -- the network half ----------------------------------------------------------------------------------------------


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    if "plan" not in planned:
        return planned
    return await asyncio.to_thread(_run, call, planned["plan"])


def _run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch.hubclient import HubRefused, flush_unbound, leave

    from .workspace import ok_result

    service = call.service
    wsk_pub, sign = _wsk_sign(service)
    now = lambda: int(service.clock() * 1000)  # noqa: E731
    save = lambda s: _save_state(service, s)  # noqa: E731
    model, state = plan["model"], plan["state"]
    try:
        if plan["op"] == "hub.connect":
            aliases = [{"person_id": p, "user_id": u} for p, u in sorted(model.get("aliases", {}).items())]
            link = {
                "addr": plan["addr"],
                "spki": plan["spki"],
                "hk_op": plan["hk_op"],
                "wsk_pub": wsk_pub,
                "sign": sign,
            }
            transport = _transport(service, model["hub_id"], link)
            try:
                r = _join(state, plan, model, service, wsk_pub, sign, now, save, transport, aliases)
            finally:
                getattr(transport, "close", lambda: None)()
            _audit(call, "hub.connect", hub_id=r.state.hub_id, binding_seq=r.state.binding_seq)
            return ok_result({"hub_id": r.state.hub_id, "status": r.state.status, "binding_seq": r.state.binding_seq})
        return ok_result(_unbind(call, plan, state, sign, wsk_pub, now, save, leave, flush_unbound))
    except HubRefused as exc:
        _audit(call, "hub.refused", op=plan["op"], code=exc.code)
        raise _refused(exc.code) from None


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
) -> Any:
    from orch.crypto.encoding import unb64u
    from orch.hubclient import join_selective

    return join_selective(
        state=state, invite=plan["invite"], hub_card=plan["card"], v2_card=plan["v2"],
        pk_pub=unb64u(plan["pk_pub"], 65), workspace_id=service.workspace_id, wsk_pub=wsk_pub, sign_wsk=sign,
        dev_mode=plan["dev"], now_ms=now, transport=transport, addr=plan["addr"],
        record_joined=lambda _payload: None,  # hub.joined is already on the log (checked in serve)
        save=save, aliases=aliases, mode=model["mode"], binding_seq=model["binding_seq"],
    )  # fmt: skip


def _unbind(
    call: Call, plan: dict[str, Any], state: Any, sign: Any, wsk_pub: Any, now: Any, save: Any, leave: Any, flush: Any
) -> dict[str, Any]:

    service, model = call.service, plan["model"]
    if state is None:  # never connected: there is no binding anywhere to end
        return {"hub_id": model["hub_id"], "unbound": False, "delivered": True, "reason": "never_connected"}
    from orch.crypto.encoding import unb64u

    dev = bool(state.binding["o"]["dev"])  # the unbound keeps the binding's own dev marker
    link = {"addr": _addr({}, state, dev), "spki": state.tls_spki, "hk_op": unb64u(state.hk_op_pub, 65),
            "wsk_pub": wsk_pub, "sign": sign}  # fmt: skip
    t = _transport(service, state.hub_id, link)
    try:
        return _unbind_over(call, plan, state, sign, now, save, leave, flush, dev, t)
    finally:
        getattr(t, "close", lambda: None)()


def _unbind_over(
    call: Call, plan: Any, state: Any, sign: Any, now: Any, save: Any, leave: Any, flush: Any, dev: bool, t: Any
) -> dict[str, Any]:
    from orch.hubclient import HubRefused

    service = call.service
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

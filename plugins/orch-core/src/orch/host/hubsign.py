"""Who may sign locally once a hub is joined in full mode (V3 C11, orch-core#879; spec D78, D81, §8, §8a).

In **full** mode (and ``unknown``, which is treated as full) the hub's role gate decides every human verb and a channel
signs it; no local key does. The rule is enforced at the single choke point, :func:`orch.host.human.append_signed`,
under the store lock and on the log state the append sees (:func:`refusal`); the early checks in ``human.prepare`` and
``human.submit`` only give friendlier errors. Standalone and selective are untouched (D79, §8a).

The one exception is **break-glass**: leaving the hub when the hub is down or hostile. :data:`BREAK_GLASS` is the ONE
constant that names it, by event type (never by op name). C8 (break-glass repin and rotate) adds its types here;
neither C8 nor C10 may add a local-sign path that does not go through ``append_signed``. D79: this module imports no
hub code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = [
    "BREAK_GLASS",
    "HUB_ONLY",
    "break_glass_allowed",
    "log_hub_only",
    "hub_only_error",
    "refuse_full_while_click_on",
    "refusal",
]

#: signing modes in which no local signature is accepted (``unknown`` fails closed, as full)
HUB_ONLY = frozenset({"full", "unknown"})

#: event type -> the key that may sign it in break-glass: ``"owner"`` (the owner's passphrase device key, or the
#: recovery key ``PK``) or ``"recovery"`` (``PK`` only: a repin moves the root of trust). Only an owner, over the
#: person path (never the relay), never ``auth: convenience``. ``hub.joined`` is NOT here: a second join in full mode
#: would act as a repin without ``PK``. C8 adds its repin (``"recovery"``) and rotate (``"owner"``) types.
BREAK_GLASS: dict[str, str] = {"hub.left": "owner"}

_ALLOWED_AUTH = (None, "passphrase")


def hub_only_error(reason: str = "hub") -> HostError:
    """``sign.hub_only``: this workspace is in full hub mode, so approvals are signed on the phone or in the hub."""
    return HostError(
        "sign.hub_only",
        "this workspace is joined to a hub in full mode: approve on your phone or in the hub, not with a local key",
        hint="approve in the hub or on your phone; to leave the hub (break-glass): orch hub leave",
        fix=["orch", "hub", "status"],
        detail={"reason": reason},
    )


def _is_recovery_signature(ws: Any, ev: dict[str, Any]) -> bool:
    """The recovery key ``PK`` signed (and no device key): the actor's device is not a device of the roster and the
    event carries the recovery certificate (the shape ``authorize_person`` calls recovery). C8 may tighten this."""
    dev = ev["actor"].get("device")
    return dev not in ws.devices and isinstance(ev.get("cert"), dict)


def break_glass_allowed(ws: Any, ev: dict[str, Any], signed_by: tuple[str, str] | None) -> bool:
    """May ``ev`` land although the workspace is hub-only? All of: the type is in :data:`BREAK_GLASS`; it did not come
    over the relay; the actor is a person, a workspace owner, with no hub ``via``; ``auth`` is the passphrase (never
    ``convenience``, ``hub`` or a phone's); the signature is the owner's own device key or the recovery key, and the
    recovery key alone for a ``"recovery"`` type."""
    need = BREAK_GLASS.get(str(ev.get("type")))
    actor = ev.get("actor")
    if (
        need is None
        or signed_by is not None
        or not isinstance(actor, dict)
        or actor.get("kind") != "person"
        or "via" in actor
        or ev.get("auth") not in _ALLOWED_AUTH
    ):
        return False
    member = ws.members.get(actor.get("id"))
    if member is None or member.role != "owner":
        return False
    if _is_recovery_signature(ws, ev):
        return True
    dev = ws.devices.get(actor.get("device"))
    return need == "owner" and dev is not None and dev.person == actor["id"]


def log_hub_only(ws: Any) -> bool:
    """Does the signed log alone put the workspace in full hub mode (joined, full or an unreadable mode)? The part of
    :func:`~orch.host.hubops.signing_mode` that needs no ``hub.json``: what direct mode (no host, no host state) can
    see, so it can only be looser than the host's answer, never different in kind."""
    model = dict(ws.hub)
    return bool(model) and model.get("status") == "joined" and model.get("mode") != "selective"


def refusal(
    service: WorkspaceService, events: list[dict[str, Any]], signed_by: tuple[str, str] | None
) -> HostError | None:
    """``sign.hub_only`` for the first event of ``events`` (the ones not in the log yet) that a hub-only workspace may
    not take from a local signer, else ``None``. The caller holds the store lock; the mode is read from the log state
    it sees. Fails closed: a mode that cannot be read is ``unknown``."""
    if not events:
        return None
    from . import hubops

    if hubops.signing_mode(service) not in HUB_ONLY:
        return None
    ws = service.store.state.workspace
    for ev in events:
        if not break_glass_allowed(ws, ev, signed_by):
            return hub_only_error()
    return None


def refuse_full_while_click_on(service: Any) -> None:
    """A full join, a ``selective -> full`` switch and a full ``hub.connect`` first need click-to-sign OFF
    (``orch host convenience off``): in full mode the click signer is refused at the append anyway, and a flag left on
    would only look like a way to sign. Raises ``sign.hub_only`` (reason ``hub``) while the flag is on."""
    from . import convenience

    if convenience.enabled(service.state_dir, service.workspace_id, service.host_uid):
        raise HostError(
            "sign.hub_only",
            "full hub mode takes no click-to-sign: switch it off first (orch host convenience off), then join",
            hint="orch host convenience off --workspace W, then join the hub again",
            fix=["orch", "host", "convenience", "off", "--workspace", str(service.workspace_id)],
            detail={"reason": "hub"},
        )

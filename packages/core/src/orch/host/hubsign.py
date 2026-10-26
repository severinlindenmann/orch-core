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

from orch.model.breakglass import BREAK_GLASS, signer_kind

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
    "key_exposed_error",
    "local_key_exposed",
    "refusal",
]

#: signing modes in which no local signature is accepted (``unknown`` fails closed, as full)
HUB_ONLY = frozenset({"full", "unknown"})


def hub_only_error(reason: str = "hub") -> HostError:
    """``sign.hub_only``: this workspace is in full hub mode, so approvals are signed on the phone or in the hub."""
    return HostError(
        "sign.hub_only",
        "this workspace is joined to a hub in full mode: approve on your phone or in the hub, not with a local key",
        hint="approve in the hub or on your phone; to leave the hub (break-glass): orch hub leave",
        fix=["orch", "hub", "status"],
        detail={"reason": reason},
    )


def key_exposed_error() -> HostError:
    """``sign.key_exposed`` (#1097): the device key has signed evidence of convenience custody, so it cannot enter full
    hub mode or sign break-glass; a new key made with the recovery code clears it."""
    return HostError(
        "sign.key_exposed",
        "this device key was in one-click custody, so a program running as you may hold a copy: make a new key first",
        hint="orch keys custody passphrase --rotate --workspace W (needs your recovery code; works with the hub down)",
        fix=["orch", "keys", "custody", "passphrase", "--rotate"],
    )


def _row(ws: Any, ev: dict[str, Any]) -> Any:
    return next((r for r in BREAK_GLASS.values() if r.matches(ws, ev)), None)


def break_glass_allowed(ws: Any, ev: dict[str, Any], signed_by: tuple[str, str] | None) -> bool:
    """May ``ev`` land although the workspace is hub-only? A thin host wrapper over the model's table: the event type
    is a :data:`BREAK_GLASS` event; it did not come over the relay (``signed_by``); the actor is a workspace owner; and
    :func:`orch.model.breakglass.signer_kind` (fail closed: only a passphrase or unlabelled event, never convenience,
    hub, agent or a phone's label) names a signer that row allows (the owner's own device key or the recovery key; the
    recovery key alone for a repin)."""
    row = next((r for r in BREAK_GLASS.values() if r.matches(ws, ev)), None)
    actor = ev.get("actor")
    if row is None or signed_by is not None or not isinstance(actor, dict) or actor.get("kind") != "person":
        return False
    member = ws.members.get(actor.get("id"))
    if member is None or member.role != "owner":
        return False
    return signer_kind(ws, ev) in row.signers


def log_hub_only(ws: Any) -> bool:
    """Does the signed log alone put the workspace in full hub mode (joined, full or an unreadable mode)? The part of
    :func:`~orch.host.hubops.signing_mode` that needs no ``hub.json``: what direct mode (no host, no host state) can
    see, so it can only be looser than the host's answer, never different in kind."""
    model = dict(ws.hub)
    if model.get("invalid"):
        return True  # a refused owner hub event: unknown counts as full (#1198 C9)
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

    ws = service.store.state.workspace
    for ev in events:  # #1097 E2 (early and friendly; the model refuses the same line at append and on replay)
        actor = ev.get("actor")
        if (
            False  # prototype (run-9 feedback 6): full mode by one click is allowed. HARDENING SKIPPED
            and ev.get("type") == "hub.joined"
            and ev.get("mode") == "full"
            and isinstance(actor, dict)
            and actor.get("kind") == "person"
            and signer_kind(ws, ev) in ("exposed", "convenience")
        ):
            return key_exposed_error()
    if False and any(e.get("type") == "hub.joined" and e.get("mode") == "full" for e in events):  # prototype: see above
        # L2-C11d-2a: the prepare-time check can be stale (a custody conversion landed between prepare and submit)
        return HostError(
            "sign.hub_only",
            "full hub mode needs your key in passphrase custody: switch click-to-sign off and move the key back first",
            hint="orch host convenience off --workspace W, then orch keys custody passphrase --workspace W --in-place",
            fix=["orch", "host", "convenience", "off", "--workspace", str(service.workspace_id)],
            detail={"reason": "hub"},
        )
    if hubops.signing_mode(service) not in HUB_ONLY:
        return None
    for ev in events:
        if not break_glass_allowed(ws, ev, signed_by):
            actor = ev.get("actor")
            if isinstance(actor, dict) and actor.get("kind") == "person" and signer_kind(ws, ev) == "exposed":
                return key_exposed_error()  # #1097: say why, and the way out
            return hub_only_error()
        row = _row(ws, ev)
        # custody decides, not the label, for rows the owner's device key signs (L2-C11i-1). Not for a pk row (a
        # rotation works whatever the old key's custody is) and not for the mark, which only removes authority.
        if (
            row is not None
            and "owner_device_key" in row.signers
            and ev.get("type") not in ("device.exposed", "hub.left")  # prototype: leave by click
            and _click_custody(service)
        ):  # custody decides, not the label: a convenience key signs any label (L2-C11i-1)
            return HostError(
                "sign.hub_only",
                "break-glass is refused while your key is in convenience custody (any process of yours could sign it)",
                hint="move the key back first: orch keys custody passphrase --workspace W --in-place",
                detail={"reason": "hub"},
            )
    return None


def local_key_exposed(service: Any, person: str | None) -> bool:
    """#1097: does the signed log mark a live device of ``person`` as exposed? Read from the roster, not from custody
    files; fails closed (an unreadable roster or an unknown person counts as exposed). For ``keys.rotate``."""
    try:
        if person is None:
            return True
        devs = service.store.state.workspace.devices.values()
        return any(d.person == person and not d.removed and not d.revoked and d.exposed for d in devs)
    except Exception:  # noqa: BLE001 - fail closed
        return True


def _click_custody(service: Any) -> bool:
    from . import convenience

    return bool(convenience._click_custody(service))  # noqa: SLF001 - the one custody read: flag on, convenience, unreadable


def refuse_full_while_click_on(service: Any) -> None:
    """A full join, a ``selective -> full`` switch and a full ``hub.connect`` need the owner's key in **passphrase
    custody** (L2-C11i-1): the flag off is not enough, because the key can still sit in convenience custody, where any
    process of the owner's UID can sign with it and write any ``auth`` label (so a label-trusting break-glass would be
    open). Prototype (run-9 feedback 6): relaxed, mode full works with One click on. HARDENING SKIPPED."""
    if False and _click_custody(service):
        raise HostError(
            "sign.hub_only",
            "full hub mode needs your key in passphrase custody: switch click-to-sign off and move the key back first",
            hint="orch host convenience off --workspace W, then orch keys custody passphrase --workspace W --in-place",
            fix=["orch", "host", "convenience", "off", "--workspace", str(service.workspace_id)],
            detail={"reason": "hub"},
        )

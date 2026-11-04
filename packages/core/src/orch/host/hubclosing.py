"""The host side of the hub closing notice (protocol 9.7, #1463).

A hub that is being removed tells each bound workspace (push ``closing_notice``, pull ``closing_get``). The host
verifies the notice with :func:`orch.hubclient.closing.verify_closing` against the card it holds, its own workspace
and its CURRENT binding, and only then records the four members in ``hub.json`` (``HubState.closing``). While that
record stands and its ``binding_seq`` is the current binding's, the host serves no company skills, tools or secrets
and takes no new company grants. It never leaves, never deletes a file and does nothing at ``ends_ms``: the record
is cleared by a leave, an unbind, a rebind (a new binding), or a ``closing_get`` answer ``null`` of the same hub over
its pinned link (a restored hub). Everything shown to the owner is core's own text; nothing of the notice is shown."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = ["active", "apply", "clear", "on_closing_notice", "pull", "pump", "view"]

_LOG = logging.getLogger("orch.host.hubclosing")


def active(state: Any) -> dict[str, Any] | None:
    """The record, when it counts: the binding is bound and the record is of THIS binding. Otherwise ``None``."""
    c = getattr(state, "closing", None)
    if c is None or state.status != "bound" or c["binding_seq"] != state.binding_seq:
        return None
    return c  # type: ignore[no-any-return]


def view(state: Any) -> dict[str, int] | None:
    """For ``hub.status``: ``{since_ms, ends_ms}`` or ``None``."""
    c = active(state)
    return None if c is None else {"since_ms": c["issued_ms"], "ends_ms": c["ends_ms"]}


def _audit_refused(service: WorkspaceService, code: str) -> None:
    from . import securitylog

    securitylog.record(
        service.state_dir, service.host_uid, service.clock(), "hub.closing_refused",
        workspace_id=service.workspace_id, code=code,
    )  # fmt: skip


def _stop_serving(service: WorkspaceService) -> None:
    """The opened company secrets go first, then the capabilities a running server holds (best effort)."""
    from . import orgmcp, orgsecrets

    orgsecrets.drop_cache(service)
    try:
        orgmcp.revoke_workspace(service, service.workspace_id)
    except Exception:  # noqa: BLE001 - a proxy that is not there holds nothing
        _LOG.info("no capabilities to revoke")


def apply(service: WorkspaceService, signed: Any) -> str | None:
    """Verify ``signed`` against the CURRENT state on disk and record it. ``None`` when recorded, else the refusal
    code (audited as ``hub.closing_refused`` with the code only)."""
    from orch.hubclient import HubRefused, snapshot
    from orch.hubclient.closing import verify_closing

    from . import hubops

    now = int(service.clock() * 1000)
    with hubops._MU:  # noqa: SLF001
        state = hubops._load_state(service)  # noqa: SLF001
        if state is None or state.status != "bound":
            code: str | None = "not_joined"
        else:
            try:
                dev = bool(state.binding["o"]["dev"])
                held = snapshot._held(state, now, dev)  # noqa: SLF001
                rec = verify_closing(
                    signed, held, workspace_id=service.workspace_id, binding_seq=state.binding_seq, now_ms=now,
                    dev_mode=dev, revoked_op_certs=frozenset(state.revoked_op_certs),
                )  # fmt: skip
            except HubRefused as exc:
                code = exc.code
            except (KeyError, TypeError):
                code = "malformed"
            else:
                if state.closing != rec:
                    state.closing = rec
                    hubops._write_state(service, state)  # noqa: SLF001
                code = None
    if code is not None:
        _audit_refused(service, code)
        return code
    _stop_serving(service)
    return None


def clear(service: WorkspaceService, hub_id: str) -> bool:
    """A restored hub says it is not closing: drop the record. Only the record of that hub."""
    from . import hubops

    with hubops._MU:  # noqa: SLF001
        state = hubops._load_state(service)  # noqa: SLF001
        if state is None or state.hub_id != hub_id or state.closing is None:
            return False
        state.closing = None
        hubops._write_state(service, state)  # noqa: SLF001
        return True


def on_closing_notice(service: WorkspaceService, signed: Any) -> str | None:
    """A pushed ``closing_notice``: ``None`` to acknowledge with ``{}``, else the code for ``{"error": code}``."""
    return apply(service, signed)


def pull(service: WorkspaceService, state: Any, link: Any) -> str | None:
    """Inside a link session of the bound hub: ask ``closing_get`` (exact reply ``{"closing": notice | null}``) and
    act on it. Never raises: a hub that does not know the op, a broken link or an odd reply changes nothing. Returns
    the refusal code of a notice that did not verify, else ``None``."""
    try:
        reply = link.call("closing_get", {})
    except Exception as exc:  # noqa: BLE001 - an older hub (unknown_op) or a dead link: keep what is held
        _LOG.info("closing_get not used (%s)", getattr(exc, "code", type(exc).__name__))
        return None
    if type(reply) is not dict or set(reply) != {"closing"} or type(reply["closing"]) not in (dict, type(None)):
        _LOG.info("closing_get: reply not used (malformed)")
        return None
    if reply["closing"] is None:
        clear(service, state.hub_id)
        return None
    return apply(service, reply["closing"])


def pump(service: WorkspaceService, link: Any, wait: float = 0.0) -> bool:
    """Take one pushed ``closing_notice`` off ``link`` (if any), verify and record it, acknowledge it with ``{}`` or
    ``{"error": code}``. ``True`` when one was handled."""
    from . import hubops

    notice = link.next_closing_notice(wait)
    if notice is None:
        return False
    link.ack_closing_notice(hubops.on_closing_notice(service, notice))
    return True

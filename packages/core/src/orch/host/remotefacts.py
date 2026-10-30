"""What ``remote.set`` and ``remote.device_confirm`` are prepared from (R2, #1590): the host's own facts, never the
request's. The clock, whether this is a dev workspace, the hub mode the model holds, whether agents run as their own OS
user (#1198 H1: tier B), and, for a device confirmation, the pending enrollment the host holds in memory.

The pending enrollments belong to ``host/remote/enrollment.py`` (T2a); this module only names the one seam they plug
into (:func:`set_pending_source`). Until it is set there is nothing to confirm (``remote.no_pending``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["RemoteFacts", "facts_for", "set_pending_source"]

#: ``(service, pending_id) -> {name, presence_kind, transport_pub, presence_pub, code_id} | None`` (b64u public keys)
PendingSource = Callable[[Any, str], "dict[str, Any] | None"]
_pending_source: PendingSource | None = None


def set_pending_source(source: PendingSource | None) -> None:
    """Plug in the enrollment module's lookup of a pending device (T2a). ``None`` unplugs it."""
    global _pending_source  # noqa: PLW0603
    _pending_source = source


@dataclass(frozen=True)
class RemoteFacts:
    now_ms: int
    dev_mode: bool
    hub_mode: str  # none | selective | full: the model's own record, an unreadable hub step counts as full
    isolated: bool  # agents run as their own OS user (tier B)
    pending: dict[str, Any] | None = None  # the pending device a confirmation names, or None


def facts_for(service: Any, pending_id: str | None = None) -> RemoteFacts:
    from orch.model.remote import hub_mode_of

    from . import hubops, orgstate

    ws = service.store.state._core.ws
    pending = None
    if pending_id is not None and _pending_source is not None:
        pending = _pending_source(service, pending_id)
    return RemoteFacts(
        int(service.clock() * 1000),
        bool(hubops.workspace_dev(service)),
        hub_mode_of(ws),
        orgstate.isolated(service),
        pending,
    )

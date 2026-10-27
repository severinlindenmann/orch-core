"""Agent and recovery helpers for hub positions (A1, design ~/orch-dev/state/a1-hub-optional-design.md, D83).

Local signing is valid in EVERY hub mode: the workspace roster is the authority for a local signature, the hub is an
additional signer (``auth: "hub"``), never the only one. So there is no break-glass table and no full-mode refusal of a
local person event any more (the old D78/D81 rule, ``sign.hub_only``, is gone).

What stays here: the two recovery shapes the model must recognise (:func:`is_recovery`, :func:`is_rotation`). Agents
are judged by their grant in every mode (``authz.authorize_agent``); there is no hub-specific agent gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .types import WsCore, ts

__all__ = ["is_recovery", "is_rotation"]


def is_recovery(ws: WsCore, e: Mapping[str, Any]) -> bool:
    """The D50 recovery shape, the ONE predicate (``authz.authorize_person`` uses it): a
    ``device.added`` signed by the new device itself, which the roster does not know, for a person with no valid device
    at ``at``. The line then carries the recovery certificate (``PK``)."""
    from .authz import person_has_valid_device

    a = e.get("actor", {})
    if e.get("type") != "device.added" or e.get("device") != a.get("device") or a.get("device") in ws.devices:
        return False
    if not isinstance(e.get("cert"), Mapping):
        return False
    return not person_has_valid_device(ws, a.get("id"), ts(e["at"]))


def is_rotation(ws: WsCore, e: Mapping[str, Any]) -> bool:
    """The #1097 rotation shape, the ONE predicate (``authz.authorize_person`` uses it): a
    ``device.rotated`` signed by the new device itself, which the roster does not know. The line then carries the
    certificate (``cert``) and the fresh rotation signature (``rotation``), both by the person key."""
    a = e.get("actor", {})
    return (
        e.get("type") == "device.rotated"
        and e.get("device") == a.get("device")
        and a.get("device") not in ws.devices
        and isinstance(e.get("cert"), Mapping)
    )


# ``other`` is deliberately not a member of SIGNER_KINDS: nothing allows it.

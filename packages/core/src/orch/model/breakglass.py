"""Agent and recovery helpers for hub positions (A1, design ~/orch-dev/state/a1-hub-optional-design.md, D83).

Local signing is valid in EVERY hub mode: the workspace roster is the authority for a local signature, the hub is an
additional signer (``auth: "hub"``), never the only one. So there is no break-glass table and no full-mode refusal of a
local person event any more (the old D78/D81 rule, ``sign.hub_only``, is gone).

What stays here: the two recovery shapes the model must recognise (:func:`is_recovery`, :func:`is_rotation`) and the
agent gate (:func:`agent_refusal`, I6), which holds in full mode, as on develop.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .codes import Code, Refusal
from .types import WsCore, ts

__all__ = ["agent_refusal", "human_only_types", "is_recovery", "is_rotation"]


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


_AGENT_ONLY_TYPES: frozenset[str] | None = None


def human_only_types() -> frozenset[str]:
    """Event types only a person's operation emits (``emits.HUMAN_ONLY`` minus what any agent operation emits)."""
    global _AGENT_ONLY_TYPES
    if _AGENT_ONLY_TYPES is None:
        from .emits import EMITS_V1, HUMAN_ONLY

        human = frozenset().union(*(EMITS_V1[o] for o in HUMAN_ONLY if o in EMITS_V1))
        agent = frozenset().union(*(t for o, t in EMITS_V1.items() if o not in HUMAN_ONLY))
        _AGENT_ONLY_TYPES = human - agent
    return _AGENT_ONLY_TYPES


def agent_refusal(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """I6 (A1), exactly as on develop: in a joined FULL-mode position (``invalid`` counts as full, fail closed) an agent
    line of a human-only type (a decision, a close, a grant) is refused, however its grant reads. Making this
    mode-independent needs a separate pass over which types are truly human-only (selective-mode agents may write some
    of them; the F1 vectors have such lines)."""
    h = ws.hub
    if not h or not (h.get("invalid") or (h.get("status") == "joined" and h.get("mode") == "full")):
        return None
    if e["type"] in human_only_types():
        return Refusal(
            Code.EVENT_BAD_ACTOR, f"{e['type']} is a human act: an agent cannot sign it in a full-mode workspace"
        )
    return None

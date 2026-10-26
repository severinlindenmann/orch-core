"""Signing a person event through a custody backend: the one path that joins ``canon`` (the bytes), ``custody`` (the
key) and the event's ``auth``.

It refuses to sign unless the backend is person-capable (not the file tier, ticket-format §5.3/§12 O2) and the event's
``auth`` equals the backend's own ``auth``, so an event can't claim a factor the key doesn't enforce.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orch import canon, crypto
from orch.custody import AUTH_VALUES, Backend

from .errors import Refused

__all__ = ["sign_person_event"]


def sign_person_event(
    backend: Backend,
    key_id: str,
    workspace_id: str,
    log: str,
    event: Mapping[str, Any],
    *,
    action: str,
) -> str:
    """The b64u ``sig`` of a person ``event`` (``log`` is the ticket uid or ``"workspace"``). The caller sets
    ``event["sig"]`` itself; ``event["auth"]`` must already be present and equal ``backend.auth``."""
    if not backend.person_capable or backend.auth is None:
        raise Refused("identity.file_tier", f"the {backend.name} backend never signs person events")
    if backend.auth not in AUTH_VALUES or event.get("auth") != backend.auth:
        raise Refused("identity.auth_mismatch", f"event auth {event.get('auth')!r} but the key is {backend.auth!r}")
    payload = canon.person_signing_bytes(workspace_id, log, event)
    return crypto.b64u(backend.sign(key_id, payload, action=action))

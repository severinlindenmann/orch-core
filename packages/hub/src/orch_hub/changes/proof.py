"""What an owner signs to approve an access change (condition 7).

The digest binds ONE approval to ONE change and ONE workspace::

    digest = SHA-256( "orch/v2/hub-access-change|" || cj({
        "contract": 1, "kind": "orch.access_change.v1", "change_id", "workspace_id", "body",
        "base_snapshot_version", "expires_ms", "delta" }) )

``cj`` is the contract's canonical JSON (the fields are one object, so no field can slide into another). The owner's
CHANNEL key signs ``"orch/v2/hub-channel-proof|" || digest`` and the hub checks that with
:func:`~orch_hub.custodial.engine.check_channel_proof`, the same check a custodial approval gets (current certificate,
factor at least ``password+totp``, signature). The console recomputes the digest from the ``ctx`` it is shown, so what is
shown is what is signed.
"""

from __future__ import annotations

from typing import Any

from ..contract import suite as S
from ..contract.canon import CanonError, cj
from ..contract.hub import Refused
from ..contract.labels import ACCESS_CHANGE_KIND, ACCESS_CHANGE_LABEL

MIN_FACTOR = "password+totp"


def context(
    change_id: str,
    workspace_id: str,
    body: dict[str, Any],
    base_snapshot_version: int,
    expires_ms: int,
    delta: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "contract": 1,
        "kind": ACCESS_CHANGE_KIND,
        "change_id": change_id,
        "workspace_id": workspace_id,
        "body": body,
        "delta": delta,  # who gains or is raised in THIS workspace: what the owner actually approves
        "base_snapshot_version": base_snapshot_version,
        "expires_ms": expires_ms,
    }


def digest(ctx: dict[str, Any]) -> bytes:
    try:
        return S.sha256(ACCESS_CHANGE_LABEL.encode("ascii") + cj(ctx))
    except CanonError:
        raise Refused("malformed") from None

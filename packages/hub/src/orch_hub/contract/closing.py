"""The removal contract (protocol.md §9.7, #1463): the ``hub.closing`` notice object and the removal proof digest.

Pure functions, no I/O, no clock, no keys. The notice is signed by ``HK_op`` through ``keystore.OpSigner.sign_object``
(kind ``hub.closing``, label ``orch/v2/hub-closing|``); a host verifies it with :func:`orch_hub.contract.hub.verify_closing`.
The digest is what an admin's channel key signs (inside ``orch/v2/hub-channel-proof|``) to start the removal: a console
label like the access-change one, so no workspace ever reads it."""

from __future__ import annotations

from typing import Any

from . import hub as H
from . import suite as S
from .canon import CanonError, cj
from .labels import HUB_REMOVE_KIND, HUB_REMOVE_LABEL

NOTICE_LIFE_MS = H.CLOSING_MAX_MS


def closing_object(
    *,
    hub_id: str,
    op_cert_id: str,
    workspace_id: str,
    binding_seq: int,
    removal_id: str,
    issued_ms: int,
    dev: bool,
    life_ms: int = NOTICE_LIFE_MS,
) -> dict[str, Any]:
    """The unsigned ``hub.closing`` object (§9.7); ``ends_ms = issued_ms + life_ms`` (7 days at most)."""
    if not 0 < life_ms <= NOTICE_LIFE_MS:
        raise H.Refused("malformed")
    return {
        "v": 2,
        "suite": S.SUITE_ID,
        "kind": "hub.closing",
        "hv": H.HV,
        "hub_id": hub_id,
        "op_cert_id": op_cert_id,
        "workspace_id": workspace_id,
        "binding_seq": binding_seq,
        "removal_id": removal_id,
        "issued_ms": issued_ms,
        "ends_ms": issued_ms + life_ms,
        "dev": dev,
    }


def remove_context(hub_id: str, nonce: str, exp_ms: int, user_id: str) -> dict[str, Any]:
    """What the page shows and recomputes (§9.7 "The removal proof"): the single object the digest binds."""
    return {
        "contract": 1,
        "kind": HUB_REMOVE_KIND,
        "hub_id": hub_id,
        "nonce": nonce,
        "exp_ms": exp_ms,
        "user_id": user_id,
    }


def remove_digest(ctx: dict[str, Any]) -> bytes:
    """``SHA-256("orch/v2/hub-remove|" || cj(ctx))``; the channel key signs ``"orch/v2/hub-channel-proof|" || digest``."""
    try:
        return S.sha256(HUB_REMOVE_LABEL.encode("ascii") + cj(ctx))
    except CanonError:
        raise H.Refused("malformed") from None

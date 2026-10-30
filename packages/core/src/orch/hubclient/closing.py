"""The ``hub.closing`` notice on the host (orch-hub ``contracts/hub/protocol.md`` 9.7, #1463).

``verify_closing`` is a pure function: it checks one signed notice against the card the host holds and the host's own
CURRENT binding, and returns the small closed result the host would record. It stores nothing, changes nothing and
never leaves a hub; what a host DOES with a verified notice (a banner, no company content, the owner's own Leave) is
host behaviour that comes in a later change. Written from the contract text; the ``cases`` of
``contracts/hub/vectors_hub_closing.json`` are replayed in ``tests/hub/test_closing_vectors.py``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import wire
from .errors import HubRefused

__all__ = ["CLOSING_MAX_MS", "SIG_CLOSING", "verify_closing"]

SIG_CLOSING = wire.SIG_CLOSING
#: protocol 9.7: a notice lives at most 7 days from its issue
CLOSING_MAX_MS = 7 * 24 * 3600 * 1000


def verify_closing(
    signed: Any,
    held: Mapping[str, Any],
    *,
    workspace_id: str,
    binding_seq: int,
    now_ms: int,
    dev_mode: bool,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """``held`` is the result of :func:`wire.verify_card`; ``workspace_id`` and ``binding_seq`` are the host's own and
    its CURRENT binding's. Refusals, in order: ``revoked``, ``malformed``, ``wrong_suite``, ``bad_signature``,
    ``other_hub``, ``stale_op_cert``, ``dev_refused`` / ``dev_mismatch``, ``other_workspace``, ``stale_binding`` (any
    other ``binding_seq``), ``cert_invalid`` (issued over 300 s ahead, or ``ends_ms`` not in
    ``(issued_ms, issued_ms + 7 d]``), ``cert_expired`` (``now >= ends_ms``).

    Returns ``{removal_id, issued_ms, ends_ms, binding_seq}``: the only members a host records."""
    o = wire._under_card(signed, "hub.closing", SIG_CLOSING, held, dev_mode, revoked_op_certs)  # noqa: SLF001
    if o["workspace_id"] != workspace_id:
        raise HubRefused("other_workspace")
    if type(binding_seq) is not int or o["binding_seq"] != binding_seq:
        raise HubRefused("stale_binding")
    if (
        o["issued_ms"] > now_ms + wire.CLOCK_SKEW_MS
        or not o["issued_ms"] < o["ends_ms"] <= o["issued_ms"] + CLOSING_MAX_MS
    ):
        raise HubRefused("cert_invalid")
    if now_ms >= o["ends_ms"]:
        raise HubRefused("cert_expired")
    return {k: o[k] for k in ("removal_id", "issued_ms", "ends_ms", "binding_seq")}

"""The reconciliation builder of ``relay.submit`` (11A.4, R2; review M1 of round 2).

Registered ONCE, at import, with :func:`orch.host.outcomes.result_builder`; :mod:`orch.host.outcomes` imports this
module at its end, so the builder is in place before any ``reconcile_all`` of any workspace. It keeps no state: the
workspace is the record's own ``namespace`` (the outcome store's workspace id), so one process serving several
workspaces rebuilds each record under its own workspace, never under the last one registered.

Crash reconciliation matches event identity (orch-52 final, O6): a logged event with the planned id but other signed
content or another actor is ``event.duplicate_id`` (stored ``failed``), never a false ``committed``.
"""

from __future__ import annotations

from typing import Any

from orch import canon

from .errors import HostError
from .outcomes import result_builder

__all__ = ["rebuild_relay_submit", "same_event"]


def same_event(workspace_id: str, log: str, a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Event identity (orch-52 final): the digest of the signed content (the event without ``sig`` and the host-stamped
    fields, F1 §5.3) and the actor; never the signature bytes (ECDSA signatures are randomised, high-s accepted)."""
    try:
        return a.get("actor") == b.get("actor") and canon.person_signing_bytes(
            workspace_id, log, a
        ) == canon.person_signing_bytes(workspace_id, log, b)
    except Exception:  # noqa: BLE001 - unbuildable bytes are not the same event
        return False


@result_builder("relay.submit")
def rebuild_relay_submit(record: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """The result of a reconciled ``relay.submit`` record: every logged event must be the submitted one (signed content
    and actor, under the record's own workspace), else ``event.duplicate_id``."""
    from orch.relay.submit import result

    workspace_id = record.get("namespace")
    if type(workspace_id) is not str:
        raise HostError("internal", "a relay.submit record without its workspace")
    args = record.get("args_normalised") or {}
    log = str(args.get("log", ""))
    submitted = {e.get("id"): e for e in args.get("events") or [] if isinstance(e, dict)}
    for e in events:
        mine = submitted.get(e.get("id"))
        if mine is None or not same_event(workspace_id, log, e, mine):
            raise HostError("event.duplicate_id", "an event with this id and other content is in the log")
    return result(log, [int(e["seq"]) for e in events])

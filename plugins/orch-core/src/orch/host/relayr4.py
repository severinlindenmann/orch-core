"""R4 host wiring (11A.9 "Unpublished evidence"): the ``unpublished`` seam of R5's relay hook, and the owner-visible
text for the held ledger that ``host.status.relay`` and ``orch host status`` show.

* :func:`unpublished` ``(service, worker) -> UnpublishedConsumer``: the consumer on the worker's session and state
  directory; every store question goes through ``worker.job`` (``Store.note_forwarded_revocation``,
  ``Store.pair_standings``, ``Store.forwarded_evidence``, each on the workspace's worker thread under the store
  lock); ``notices`` is the bridge loop's last poll (read-only); ``stronger`` (the only reason comparison R4 uses) is
  built here on ``orch.identity.certs.revoke_strength``.
* :func:`held_attention` turns the ledger (``relay/unpublished.json``) into person-visible text with what to do, per
  row from the STORE's standing of the pair (never from a relay field), honest about rows that have no owner action.
  SEAM (#572): when host-originated ``needs`` items exist, a held pair becomes one and this text moves there.
  SEAM (flip): the host's commit hook calls ``UnpublishedConsumer.wake()`` after a commit that can revoke
  (``device.revoked``, ``member.removed``, a restore); until then the aux turn re-judges every ``JUDGE_EVERY_S``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

__all__ = [
    "FIX_OTHER", "FIX_REBUILDING", "FIX_REVOKE", "FIX_STALE", "FIX_STRONGER", "FIX_UNKNOWN", "FIX_UNVERIFIABLE",
    "held_attention", "held_pairs_at", "ledger_at", "stronger", "unpublished",
]  # fmt: skip

#: what the owner does about a ledger row (F1 §5.3: only the person path publishes a revocation; the host never
#: appends device.revoked or a restore by itself). Chosen from the STORE's standing of the pair, never a relay field;
#: a second event of equal strength is never asked for (relay obligation 5).
FIX_REVOKE = (
    "an owner signs a device.revoked event for that device, with the reason named, on an owner device (F1 §5.3); "
    "the host never does it by itself (issue #572 tracks a needs item for it)"
)
FIX_STRONGER = (
    "the workspace holds a weaker revocation of that device: an owner signs a device.revoked event with the stronger "
    "reason named (it replaces the weaker one, F1 §5.3); the relay may keep listing the item until the next restore, "
    "which is harmless"
)
FIX_OTHER = (
    "cannot be settled from this host: the workspace holds only other persons' evidence for that device (the model "
    "refuses this person's revocation); it is shown so it is never hidden and settles only if the workspace state "
    "changes, for example a restore that carries this person's revocation"
)
FIX_UNKNOWN = (
    "the device is not known to this workspace: there is nothing to sign until it is added (the row then asks for a "
    "device.revoked); it is shown so it is never hidden"
)
FIX_UNVERIFIABLE = (
    "nothing to sign: the relay serves items this host cannot verify; the notice clears after two clean complete "
    "scans and a week without a new one; check the relay (orch host relay status)"
)
FIX_REBUILDING = "wait: the held list is being rebuilt by a full read from the relay; it is complete when this clears"
FIX_STALE = "check the relay link (orch host relay status): the unpublished scans keep failing or are older than 2 days"
FIX_OVERFLOW = (
    "the held ledger, one person's share of it or its non-actionable budget is full: further pairs are counted, not "
    "listed; rows clear only when the workspace state settles them (an owner signs the stronger device.revoked, or a "
    "restore carries the revocation); there is no reset command yet"
)


def stronger(new: str, old: str) -> bool:
    """Whether reason ``new`` is strictly stronger than ``old`` (``revoke_strength``: compromised > lost = retired);
    a reason outside the enum counts as the strongest, so it can never lower a row. The ONE comparison R4's ledger
    uses (injected into it: ``orch.relay`` cannot import ``orch.identity``)."""
    from orch.identity.certs import REVOKE_REASONS, revoke_strength

    def rank(reason: str) -> int:
        return revoke_strength(reason) if reason in REVOKE_REASONS else revoke_strength("compromised") + 1

    return rank(new) > rank(old)


def unpublished(service: Any, worker: Any) -> Any:
    """R5's ``unpublished`` seam."""
    from orch.relay.unpublished import UnpublishedConsumer

    store_job = worker.job

    def on_store(call: Any) -> Any:
        def job() -> Any:
            store = service.store
            if store is None:
                raise RuntimeError("the workspace store is unavailable")  # no result: no ack, read again later
            return call(store)

        return store_job(job)

    def hold(item: Any) -> str:
        return on_store(lambda store: store.note_forwarded_revocation(item))

    def judge(pairs: list[tuple[str, str, str]]) -> list[str]:
        return on_store(lambda store: store.pair_standings(pairs))

    def evidence() -> list[dict[str, str]]:
        return on_store(lambda store: store.forwarded_evidence())

    def notices() -> Any:
        loop = worker.loop
        return loop.notices if loop is not None else None

    return UnpublishedConsumer(
        workspace_id=service.workspace_id, session=worker.session, state=worker.state, hold=hold, judge=judge,
        evidence=evidence, stronger=stronger, notices=notices, alarm=worker.alarm,
        now_ms=lambda: int(service.clock() * 1000),
    )  # fmt: skip


def _doc_at(state_dir: Path, workspace_id: str, host_uid: int) -> Any:
    from orch.relay.unpublished import FILE

    from .custody import read_json

    path = Path(os.path.realpath(state_dir)) / "hosts" / workspace_id / "relay" / FILE
    try:
        return read_json(path, host_uid)
    except Exception:  # noqa: BLE001 - a status read never fails on it (unsafe or unreadable: nothing shown)
        return None


def held_pairs_at(state_dir: Path, workspace_id: str, host_uid: int) -> list[dict[str, Any]]:
    """The held rows in ``<state>/hosts/<ws>/relay/unpublished.json`` (checked read), ``[]`` when none."""
    from orch.relay.unpublished import held_pairs

    return held_pairs(_doc_at(state_dir, workspace_id, host_uid))


def ledger_at(state_dir: Path, workspace_id: str, host_uid: int, now_ms: int | None = None) -> dict[str, Any]:
    """The whole ledger view (rows, unverifiable, overflow, rebuilding, stale, age): what status shows."""
    from orch.relay.unpublished import ledger_view

    return ledger_view(
        _doc_at(state_dir, workspace_id, host_uid), int(time.time() * 1000) if now_ms is None else now_ms
    )


_ROW_FIX = {"revoke": FIX_REVOKE, "stronger": FIX_STRONGER, "other": FIX_OTHER, "unknown": FIX_UNKNOWN}


def held_attention(
    prefix: str, pairs: list[dict[str, Any]], ledger: dict[str, Any] | None = None
) -> dict[str, str] | None:
    """``{what, fix}`` for a person when the ledger is not clean (held rows, unverifiable items, an overflow, a
    rebuild, a stale scan), else ``None``. The fix texts follow each row's STORE standing; rows that have no owner
    action say so."""
    ledger = ledger or {}
    unv, overflow = int(ledger.get("unverifiable") or 0), int(ledger.get("overflow") or 0)
    rebuilding, stale = bool(ledger.get("rebuilding")), bool(ledger.get("stale"))
    if not (pairs or unv or overflow or rebuilding or stale):
        return None
    parts: list[str] = []
    fixes: list[str] = []
    if pairs:
        last = pairs[-1]
        n = len(pairs)
        parts.append(
            f"the relay holds {n} revocation{'s' if n != 1 else ''} this workspace has not published or settled "
            f"(latest: device d_{last['device_id']} of person p_{last['person_id']}, reason {last['need']})"
        )
        fixes += [_ROW_FIX[s] for s in _ROW_FIX if any(p.get("state") == s for p in pairs)]
    if unv:
        parts.append(f"{unv} item{'s' if unv != 1 else ''} the relay serves could not be verified")
        fixes.append(FIX_UNVERIFIABLE)
    if overflow:
        who = ledger.get("overflow_persons") or []
        parts.append(
            f"the held list overflowed (up to {overflow} pair{'s' if overflow != 1 else ''} not listed"
            + (f"; persons {', '.join('p_' + p for p in who)}" if who else "")
            + "): there may be obligations this host cannot show"
        )
        fixes.append(FIX_OVERFLOW)
    if rebuilding:
        parts.append("the held list is being rebuilt")
        fixes.append(FIX_REBUILDING)
    if stale:
        age = ledger.get("age_s")
        parts.append("the unpublished scan is stale" + (f" (last complete scan {age // 3600} h ago)" if age else ""))
        fixes.append(FIX_STALE)
    # an overflow means obligations may exist that are not listed: never claim "nothing to do" while one is open
    actionable = overflow > 0 or any(p.get("state") in ("revoke", "stronger") for p in pairs)
    return {"what": f"{prefix}: " + "; ".join(parts), "fix": "; and ".join(fixes), "actionable": actionable}

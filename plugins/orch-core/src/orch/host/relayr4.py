"""R4 host wiring (11A.9 "Unpublished evidence"): the ``unpublished`` seam of R5's relay hook, and the owner-visible
text for held evidence that ``host.status.relay`` and ``orch host status`` show.

* :func:`unpublished` ``(service, worker) -> UnpublishedConsumer``: the consumer on the worker's session and state
  directory; every item is held through ``worker.job`` (``Store.note_forwarded_revocation`` on the workspace's
  worker thread, under the store lock); ``notices`` is the bridge loop's last poll (read-only).
* :func:`held_attention` turns ``relay/unpublished.json``'s ``held`` pairs into person-visible text with what to do.
  SEAM (#572): when host-originated ``needs`` items exist, a held pair becomes one ("publish this revocation") and
  this text moves there.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = ["FIX", "held_attention", "held_pairs_at", "unpublished"]

#: what the owner does about held evidence (F1 §5.3: only the person path publishes a revocation; the host never
#: appends device.revoked or a restore by itself)
FIX = (
    "publish the revocation: an owner signs a device.revoked event for that device on an owner device (F1 §5.3), or "
    "carries it in the next restore; the host never does it by itself (issue #572 tracks a needs item for it)"
)


def unpublished(service: Any, worker: Any) -> Any:
    """R5's ``unpublished`` seam."""
    from orch.relay.unpublished import UnpublishedConsumer

    store_job = worker.job

    def hold(item: Any) -> str:
        def note() -> str:
            store = service.store
            if store is None:
                raise RuntimeError("the workspace store is unavailable")  # no result: no ack, read again later
            return store.note_forwarded_revocation(item)

        return store_job(note)

    def notices() -> Any:
        loop = worker.loop
        return loop.notices if loop is not None else None

    return UnpublishedConsumer(
        workspace_id=service.workspace_id, session=worker.session, state=worker.state, hold=hold, notices=notices,
        alarm=worker.alarm, now_ms=lambda: int(service.clock() * 1000),
    )  # fmt: skip


def held_pairs_at(state_dir: Path, workspace_id: str, host_uid: int) -> list[dict[str, Any]]:
    """The held pairs in ``<state>/hosts/<ws>/relay/unpublished.json`` (checked read), ``[]`` when none."""
    from orch.relay.unpublished import FILE, held_pairs

    from .custody import read_json

    path = Path(os.path.realpath(state_dir)) / "hosts" / workspace_id / "relay" / FILE
    try:
        return held_pairs(read_json(path, host_uid))
    except Exception:  # noqa: BLE001 - a status read never fails on it (unsafe or unreadable: nothing shown)
        return []


def held_attention(prefix: str, pairs: list[dict[str, Any]]) -> dict[str, str] | None:
    """``{what, fix}`` for a person when the relay holds evidence this workspace has not published, else ``None``."""
    if not pairs:
        return None
    last = pairs[-1]
    n = len(pairs)
    what = (
        f"{prefix}: the relay holds {n} revocation{'s' if n != 1 else ''} this workspace has not published "
        f"(latest: device d_{last['device_id']} of person p_{last['person_id']}, reason {last['reason']}); "
        "the device is already refused on the relay path"
    )
    return {"what": what, "fix": FIX}

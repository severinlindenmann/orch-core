"""H13a host wiring (11A.16.4 "Lifecycle", "As built in B4"): the ``push`` seam of R5's relay hook.

:func:`push` ``(service, worker, keys, signer, view) -> PushThread`: the push thread ``orch-relay-push-<id8>`` as a
companion of the worker (started after the loop and the publisher, so only once the first link reached ``linked`` and
the ``R6_READY`` gate is open; stopped and joined with them; woken by ``Service._relay_wake`` on every commit).

* ``store``: the open workspace store; the planner, the sealing step and the dispatch step run as worker jobs
  (``worker.job``), the thread itself never takes the store lock;
* ``keys``, ``signer``, ``view``: the worker's own ``BridgeKeys``, ``RelaySigner`` and ``R6View``, so a push is sealed
  through the same seal lock as every response (no raw key here);
* ``standing``: ``device_standing_ok``, the one F1 standing function (``orch.relay`` may not import its module);
* the outbox ``outbound.sqlite`` is opened on the worker's state directory with the directory's instance id: a copied
  or restored directory starts a new fence and sends nothing old (11A.16.3 "Restore");
* ``on_error``: a fence or session error ends the thread and stops the worker (as for the publisher); the intents stay
  for the next start of this state directory.
"""

from __future__ import annotations

from typing import Any

from .errors import HostError

__all__ = ["push"]


def push(service: Any, worker: Any, keys: Any, signer: Any, view: Any) -> Any:
    """R5's ``push`` seam (``SEAMS.push``)."""
    from orch.relay.outbound import OutboundStore, OutboxVersionError
    from orch.relay.push import PushThread
    from orch.store.store import device_standing_ok
    from orch.wsproto.state import FileHolderStore

    store = service.store
    if store is None:
        raise HostError("workspace.unavailable", "the workspace is not open")
    if worker.conn is None:
        raise HostError("relay.link_mismatch", "the worker has no connection factory")

    def now_ms() -> int:
        return int(service.clock() * 1000)

    def on_error(exc: BaseException) -> None:
        worker.alarm("relay_push_stopped", type(exc).__name__)
        worker.stop()

    try:
        ob = OutboundStore(worker.state, instance_id=FileHolderStore(worker.state).instance_id(), now_ms=now_ms())
    except OutboxVersionError as e:
        worker.alarm("push_outbox_schema_newer", str(e))
        raise
    if ob.restored:
        worker.alarm("push_outbox_restarted", "the state directory is another instance: no old push is sent")
    from .phonehints import open_after

    return PushThread(
        store, ob=ob, job=worker.job, session=worker.session, keys=keys, signer=signer, view=view,
        hints=lambda n: open_after(service, n), standing=device_standing_ok, alarm=worker.alarm,
        conn=worker.conn, on_error=on_error, now_ms=now_ms,
    )  # fmt: skip

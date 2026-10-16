"""R3 host wiring (11A.5 "Publisher (R3)", "As built in B1"): the ``publisher`` seam of R5's relay hook.

:func:`publisher` ``(service, worker) -> Publisher`` builds :class:`orch.relay.publish.Publisher` for one worker:

* ``store``: the open workspace store (a worker is built only on an open workspace); every call of the publisher goes
  through ``worker.job`` (the workspace's worker thread), never held across networking, and the factory itself waits
  for no job (it runs under ``relay_lock``, on the open path on the worker thread);
* ``session``, ``state``, ``alarm``: the worker's own (ONE session and state directory per workspace);
* ``conn``: ``worker.conn``, the link's connection factory (the publisher thread's own connection, 11A.7);
* ``owner_pk_pub``: the owner's ``PK`` from the workspace's own genesis (``members.check_genesis``), never from the
  relay; ``wsk_pub``: the public key of ``keys/wsk`` in the host's custody (as ``start_worker`` reads it);
* ``max_publication_bytes``: ``link.json``'s (the relay's ``C`` as stored at the reservation);
* ``relay_level``: the level the login observer stores (``readerrecs.relay_level``; a failed read counts as 0);
* ``on_diverged``: an alarm only (``chain.diverged`` is already alarmed by R3 and publication is held); no store API
  to degrade the host exists yet (follow-up, 11A.5 "As built in R3" gap);
* ``on_error``: a fence or session error ends the publisher; the worker stops (non-blocking, idempotent).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import HostError

__all__ = ["owner_pk_pub", "publisher", "wsk_pub"]


def wsk_pub(service: Any) -> bytes:
    from orch.custody import FileBackend
    from orch.store import BackendSigner

    keys_dir = Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "keys"
    backend = FileBackend(keys_dir) if keys_dir.is_dir() else None
    if backend is None or not backend.exists("wsk"):
        raise HostError("workspace.unavailable", "the workspace signing key is not in the host's custody")
    return bytes(BackendSigner(backend, "wsk").public_key)


def owner_pk_pub(service: Any) -> bytes:
    from orch.identity import members
    from orch.identity.errors import Refused

    store = service.store
    genesis = store.genesis_event() if store is not None else None
    if genesis is None:
        raise HostError("workspace.unavailable", "the workspace has no genesis")
    try:
        return bytes(members.check_genesis(genesis)["owner_pk_pub"])
    except Refused as exc:
        raise HostError("workspace.unavailable", f"the workspace genesis does not verify: {exc.code}") from None


def _max_publication_bytes(worker: Any) -> int:
    link = worker.state.read_json("link.json")
    value = link.get("max_publication_bytes") if isinstance(link, dict) else None
    if type(value) is not int or value < 1:
        raise HostError("relay.link_mismatch", "link.json names no max_publication_bytes")
    return value


def publisher(service: Any, worker: Any) -> Any:
    """R5's ``publisher`` seam (``SEAMS.publisher``)."""
    from orch.relay.publish import Publisher
    from orch.store import readerrecs

    store = service.store
    if store is None:
        raise HostError("workspace.unavailable", "the workspace is not open")
    if worker.conn is None:
        raise HostError("relay.link_mismatch", "the worker has no connection factory")
    ws, state_dir, uid = service.workspace_id, service.state_dir, service.host_uid

    def relay_level() -> int | None:
        return readerrecs.relay_level(state_dir, ws, uid)

    def on_diverged(_checkpoint: Any) -> None:
        worker.alarm("chain.diverged", "the relay holds a checkpoint this workspace's logs do not contain")

    def on_error(exc: BaseException) -> None:
        worker.alarm("relay_publisher_stopped", type(exc).__name__)
        worker.stop()

    return Publisher(
        store, job=worker.job, session=worker.session, owner_pk_pub=owner_pk_pub(service), wsk_pub=wsk_pub(service),
        state=worker.state, max_publication_bytes=_max_publication_bytes(worker), alarm=worker.alarm,
        conn=worker.conn, on_diverged=on_diverged, on_error=on_error, relay_level=relay_level,
        now_ms=lambda: int(service.clock() * 1000),
    )  # fmt: skip

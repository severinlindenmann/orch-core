"""The workspace's own key material for the hub path: the owner's ``PK`` and the ``WSK`` public key (from the
workspace's own genesis and custody, never from a relay) and the epoch key store (``membership/``), opened once per
process. (Moved out of the deleted relay wiring, V3-D36.)"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from .errors import HostError

__all__ = ["close", "keys_for", "membership_path", "owner_pk_pub", "wsk_pub"]

_LOCK = threading.Lock()
_OPEN: dict[str, Any] = {}


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


def membership_path(service: Any) -> Path:
    """``<state>/hosts/<ws>/membership``, from the host's trusted state path resolved once (11A.3, R6-4)."""
    return Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "membership"


def _now_ms(service: Any) -> int:
    return int(service.clock() * 1000)


def keys_for(service: Any) -> Any:
    """The workspace's :class:`orch.identity.epochs.MembershipKeys`, opened once per process."""
    from orch.identity.epochs import MembershipKeys

    path = membership_path(service)
    with _LOCK:
        mk = _OPEN.get(str(path))
        if mk is None:
            mk = MembershipKeys(path, service.workspace_id, now_ms=lambda: _now_ms(service), previously_linked=False)
            _OPEN[str(path)] = mk
        return mk


def close(service: Any) -> None:
    with _LOCK:
        mk = _OPEN.pop(str(membership_path(service)), None)
    if mk is not None:
        mk.close()

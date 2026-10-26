"""The hub's workspace view (protocol.md §10.7, V3-D15): ``log_sync`` receive, a durable sealed view store, D6's
``WorkspaceView`` and the needs-you source for D13, wired by :func:`build_view_services` (``serve`` calls it)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .build import StoreWorkspaceView
from .needs import StoreViewReader, ViewReader
from .service import ViewService
from .store import ViewStore

VIEW_FILE = "workspace-view.db"


@dataclass
class ViewServices:
    store: ViewStore
    service: ViewService  # -> LinkServer(views=...)
    workspace_view: StoreWorkspaceView  # -> CustodialEngine(view=...)
    reader: StoreViewReader  # -> WebDeps(view=...)

    def close(self) -> None:
        self.store.close()


def build_view_services(
    state: Path,
    *,
    wk_seals: Any,
    registry: Any,
    access: Any,
    mode_of: Callable[[str], str | None] | None = None,
) -> ViewServices:
    """``wk_seals``: the ``WkSeals`` instance; ``registry``: the link ``Registry`` (mode, owner alias); ``access``:
    ``Access`` (``role_of(user, ws)``). ``mode_of`` defaults to ``registry.mode``."""
    store = ViewStore(str(state / VIEW_FILE))
    service = ViewService(store, wk_seals)
    return ViewServices(
        store,
        service,
        StoreWorkspaceView(service, registry.owner_alias),
        StoreViewReader(
            service,
            mode_of=mode_of or registry.mode,
            role_of=access.role_of,
            owner_alias=registry.owner_alias,
        ),
    )


__all__ = ["ViewReader", "ViewService", "ViewServices", "ViewStore", "build_view_services"]

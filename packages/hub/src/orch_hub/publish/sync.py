"""How the hub gets its card and a workspace's binding to orch-publish before it forwards (contracts/hub/protocol.md §11.2).

orch-publish has no HTTP endpoint for this yet (F2). :class:`PublishSync` is the seam; the production implementation
arrives with those endpoints, tests use an in-process one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class PublishSyncError(Exception):
    """orch-publish refused the object (``code`` is its refusal code) or could not be reached (``unavailable``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PublishSync(Protocol):
    def put_hub_card(self, card: Mapping[str, Any]) -> None:
        """The hub's current signed ``hub.card`` (with its op cert), as ``accept_hub_card``."""

    def put_op_revoked(self, revoked: Mapping[str, Any]) -> None:
        """A root-signed ``hub.op_revoked`` of the hub, as ``accept_op_revoked``."""

    def put_card(self, workspace_id: str, card: Mapping[str, Any], pk_pub: str) -> None:
        """A workspace's v2 directory card with the owner's ``PK`` (b64u), as ``card_put`` carried it. orch-publish
        verifies it itself and admits (or refuses) the workspace."""

    def put_binding(self, workspace_id: str, obj: Mapping[str, Any]) -> None:
        """A WSK-signed ``hub.binding`` or ``hub.unbound`` of the workspace, as ``accept_binding`` / ``accept_unbound``."""

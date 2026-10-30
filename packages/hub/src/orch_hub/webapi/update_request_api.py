"""HUB-WS-04 (#1385): a hub admin asks the owners of older workspaces to update. A mixin of
:class:`~orch_hub.webapi.api.WebApi` plus its route.

The request carries no text: the target is the hub's own version. It is stored per workspace and handed to the
workspace host on its next ``status_put`` reply (pull, no new transport); the host shows a plain notice to its human
owner. A second press changes nothing (the first time is kept). Display only, never an authority."""

from __future__ import annotations

from typing import Any

from .. import __version__
from ..versions import version_state


class UpdateRequestHandlers:
    def _h_a_update_request(self: Any, c: Any, p: dict, q: dict, b: dict | None):
        now = self.d.now_ms()
        reg = self.d.registry
        asked = 0
        for ws in reg.pins():
            st = reg.status(ws)
            if version_state(st["version"], __version__, "0") != "older":
                continue
            reg.ask_update(ws, __version__, now, c.user_id)
            asked += 1
        return 200, {"asked": asked, "target_version": __version__, "at_ms": now}


def update_request_routes(r: Any) -> tuple[Any, ...]:
    return (
        r(
            "POST",
            "/admin/workspaces/update-request",
            "admin",
            "a_update_request",
            "Ask the owners of older workspaces to update orch (the request carries only the version)",
        ),
    )

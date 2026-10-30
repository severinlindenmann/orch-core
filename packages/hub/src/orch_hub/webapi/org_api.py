"""Company content admin routes (M3): the skills a hub admin writes and offers to workspaces (#1430). A mixin of
:class:`~orch_hub.webapi.api.WebApi` (it only uses ``self.d`` and ``self._record``); the rules live in
``access/orgskills.py`` and ``contract/org_bundles.py``. Every act writes an activity record that names the skill, never
its text."""

from __future__ import annotations

from typing import Any

from ..access import AccessError
from ..access.mcpprobe import mcp_probes
from ..access.orgmcp import org_mcp
from ..access.orgskills import org_skills
from ..contract.org_bundles import SKILL_TEXT_MAX
from ..view.skills_attention import needs_attention


def _need(b: dict | None, **types: type) -> dict:
    if b is None or any(type(b.get(k)) is not t for k, t in types.items()):
        raise AccessError("malformed")
    return b


class OrgHandlers:
    d: Any
    _record: Any

    def _h_a_skills(self, c: Any, p: dict, q: dict, b: dict | None):
        reg = self.d.registry
        workspaces = []
        for ws in sorted(reg.pins()):
            if reg.mode(ws) is None:
                continue
            card = (reg.card(ws) or {}).get("name")
            workspaces.append({"workspace_id": ws, "name": card if type(card) is str and card else None})
        return 200, {"skills": org_skills(self.d.access).list(), "workspaces": workspaces, "max_chars": SKILL_TEXT_MAX}

    def _h_a_skill_put(self, c: Any, p: dict, q: dict, b: dict | None):
        b = _need(b, text=str)
        out = org_skills(self.d.access).put(p["name"], b["text"], actor=c.user_id)
        self._record("skill.saved", actor_id=c.user_id, detail={"skill": p["name"]})
        return 200, out

    def _h_a_skill_delete(self, c: Any, p: dict, q: dict, b: dict | None):
        org_skills(self.d.access).delete(p["name"], actor=c.user_id)
        self._record("skill.removed", actor_id=c.user_id, detail={"skill": p["name"]})
        return 200, {"ok": True}

    def _h_a_skill_offer(self, c: Any, p: dict, q: dict, b: dict | None):
        b = _need(b, workspaces=list, shared=str)
        out = org_skills(self.d.access).set_share(p["name"], b["shared"], b["workspaces"], actor=c.user_id)
        self._record("skill.offered", actor_id=c.user_id, detail={"skill": p["name"], "count": len(out["offered_to"])})
        return 200, out

    def _h_a_attention(self, c: Any, p: dict, q: dict, b: dict | None):
        """#1617 step 1: what needs an admin's attention in skills, tool servers and secrets, computed from hub data."""
        _, out = self._h_a_secrets(c, p, q, b)  # type: ignore[attr-defined]
        return 200, {"items": needs_attention(out["secrets"], out["workspaces"], self._now_ms())}  # type: ignore[attr-defined]

    # --- remote MCP servers (#1435) -------------------------------------------------------------------------------------

    def _h_a_mcp(self, c: Any, p: dict, q: dict, b: dict | None):
        reg = self.d.registry
        workspaces = []
        for ws in sorted(reg.pins()):
            if reg.mode(ws) is None:
                continue
            card = (reg.card(ws) or {}).get("name")
            workspaces.append({"workspace_id": ws, "name": card if type(card) is str and card else None})
        servers = org_mcp(self.d.access).list()
        probes = mcp_probes(self.d.access)
        probes.refresh_stale(servers)  # in the background; the page asks again a moment later
        for s in servers:
            s["status"] = probes.status(s["name"], s["url"])
        return 200, {"servers": servers, "workspaces": workspaces}

    def _h_a_mcp_check(self, c: Any, p: dict, q: dict, b: dict | None):
        """The "Check now" button: the hub asks the stored address once (no credential), at most once per 30 s per server."""
        store = org_mcp(self.d.access)
        server = next((s for s in store.list() if s["name"] == p["name"]), None)
        if server is None:
            raise AccessError("unknown_server")
        out = mcp_probes(self.d.access).check_now(server["name"], server["url"])
        return 200, out

    def _h_a_mcp_put(self, c: Any, p: dict, q: dict, b: dict | None):
        b = _need(b, url=str, auth=str, description=str)
        secret = b.get("secret")
        if secret is not None and type(secret) is not str:
            raise AccessError("malformed")
        out = org_mcp(self.d.access).put(p["name"], b["url"], b["auth"], secret, b["description"], actor=c.user_id)
        mcp_probes(self.d.access).clear_status(p["name"])  # a new address: the old answer is not about it
        self._record("mcp.saved", actor_id=c.user_id, detail={"server": p["name"]})
        return 200, out

    def _h_a_mcp_delete(self, c: Any, p: dict, q: dict, b: dict | None):
        org_mcp(self.d.access).delete(p["name"], actor=c.user_id)
        mcp_probes(self.d.access).clear_status(p["name"])
        self._record("mcp.removed", actor_id=c.user_id, detail={"server": p["name"]})
        return 200, {"ok": True}

    def _h_a_mcp_offer(self, c: Any, p: dict, q: dict, b: dict | None):
        b = _need(b, workspaces=list, shared=str)
        out = org_mcp(self.d.access).set_share(p["name"], b["shared"], b["workspaces"], actor=c.user_id)
        self._record("mcp.offered", actor_id=c.user_id, detail={"server": p["name"], "count": len(out["offered_to"])})
        return 200, out

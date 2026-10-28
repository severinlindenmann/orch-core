"""Company content admin routes (M3): the skills a hub admin writes and offers to workspaces (#1430). A mixin of
:class:`~orch_hub.webapi.api.WebApi` (it only uses ``self.d`` and ``self._record``); the rules live in
``access/orgskills.py`` and ``contract/org_bundles.py``. Every act writes an activity record that names the skill, never
its text."""

from __future__ import annotations

from typing import Any

from ..access import AccessError
from ..access.orgskills import org_skills
from ..contract.org_bundles import SKILL_TEXT_MAX


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
        b = _need(b, workspaces=list)
        ids = org_skills(self.d.access).set_offers(p["name"], b["workspaces"], actor=c.user_id)
        self._record("skill.offered", actor_id=c.user_id, detail={"skill": p["name"], "count": len(ids)})
        return 200, {"offered_to": ids}

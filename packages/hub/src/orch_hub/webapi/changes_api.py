"""The web handlers of access changes (M2-8a): the owner's page (``/me/changes``), the admin's list, cancel and override,
and the guards the admin grant / membership routes run through ``Changes``. A mixin of :class:`~orch_hub.webapi.api.WebApi`
(it only uses ``self.d`` and ``self._limit``), so the rule itself lives in ``orch_hub.changes`` and nowhere else.

Scoping: every ``/me/changes`` handler takes the person from the SESSION (``c.user_id``) and nothing else; there is no user
id parameter. A change that is not the session person's to decide answers ``unknown_change``, as one that does not exist.
"""

from __future__ import annotations

from typing import Any

from ..changes import ChangeError, Changes

PREPARE_LIMIT = (10, 60_000)  # per person: ten prepares a minute, as the signing routes
APPROVE_LIMIT = (20, 60_000)


def _body(b: dict | None) -> dict:
    return b if b is not None else {}


def _text(b: dict, key: str, *, optional: bool = False) -> str | None:
    v = b.get(key)
    if v is None and optional:
        return None
    if type(v) is not str:
        raise ChangeError("malformed")
    return v


def confirmed(b: dict | None) -> bool:
    """``confirm: true`` in the body (the last-admin warning was read); anything but a boolean is malformed."""
    v = _body(b).get("confirm", False)
    if type(v) is not bool:
        raise ChangeError("malformed")
    return v


class ChangeHandlers:
    d: Any
    _limit: Any

    def _chg(self) -> Changes:
        if self.d.changes is None:  # never apply an access change without the rule behind it
            raise ChangeError("unavailable")
        return self.d.changes

    # --- the admin's grant and membership routes ---------------------------------------------------------------------

    def _increase_answer(self, got: dict | None):
        """``None``: it was applied. A pending change: 202 and who it waits for; nothing was written to access."""
        if got is None:
            return 200, {"ok": True}
        return 202, {"change_id": got["change_id"], "waiting_for": got["waiting_for"]}

    # --- the owner's page (session only) ------------------------------------------------------------------------------

    def _h_my_changes(self, c: Any, p: dict, q: dict, b: dict | None):
        return 200, self._chg().for_owner(c.user_id)

    def _h_change_prepare(self, c: Any, p: dict, q: dict, b: dict | None):
        self._limit.hit("change-prepare", c.user_id, PREPARE_LIMIT)
        return 200, self._chg().prepare_approval(p["change_id"], c.user_id, _body(b).get("workspace_id"))

    def _h_change_approve(self, c: Any, p: dict, q: dict, b: dict | None):
        b = _body(b)
        self._limit.hit("change-approve", c.user_id, APPROVE_LIMIT)
        got = self._chg().approve(
            p["change_id"], c.user_id, _text(b, "channel_id"), _text(b, "proof"), b.get("workspace_id")
        )
        return 200, {"ok": True, **got}

    def _h_change_reject(self, c: Any, p: dict, q: dict, b: dict | None):
        self._chg().reject(p["change_id"], c.user_id, _body(b).get("workspace_id"))
        return 200, {"ok": True}

    # --- the admin's list, override and cancel (hub-admin, checked in the pipeline) ----------------------------------

    def _h_a_changes(self, c: Any, p: dict, q: dict, b: dict | None):
        return 200, {"changes": self._chg().for_admin()}

    def _h_a_change_override(self, c: Any, p: dict, q: dict, b: dict | None):
        self._chg().override(p["change_id"], c.user_id, _body(b).get("reason"))
        return 200, {"ok": True}

    def _h_a_change_cancel(self, c: Any, p: dict, q: dict, b: dict | None):
        self._chg().cancel(p["change_id"], c.user_id)
        return 200, {"ok": True}

    # --- connections between workspaces -------------------------------------------------------------------------------

    def _h_connection_set(self, c: Any, p: dict, q: dict, b: dict | None):
        """``{from, to, allow}``: allow = ask (202, waits for an approver of the receiving workspace); not allow = close now."""
        b = _body(b)
        allow = b.get("allow")
        if type(allow) is not bool:
            raise ChangeError("malformed")
        chg = self._chg()
        if allow:
            got = chg.propose_connection(c.user_id, b.get("from"), b.get("to"))
            return 202, {"change_id": got["change_id"], "waiting_for": got["waiting_for"]}
        return 200, {"ok": True, "removed": chg.deny_connection(c.user_id, b.get("from"), b.get("to"))}

    def _h_my_connection_traffic(self, c: Any, p: dict, q: dict, b: dict | None):
        return 200, {"traffic": self._chg().traffic_for(c.user_id)}

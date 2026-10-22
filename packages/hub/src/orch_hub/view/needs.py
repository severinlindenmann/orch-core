"""The needs-you source for D13's person home (``orch_hub.webapi``, orch-hub PR #29, branch ``v3-d13-webapi``).

:class:`ViewReader` below is a MATCHING PROTOCOL defined here because PR #29 is not merged yet (it must stay identical
to ``webapi.api.ViewReader``): ``needs_you(workspace_id, user_id) -> list[row]`` and ``item(workspace_id, user_id,
item_id) -> row | None``. A row has ``item_id`` (32 hex), ``kind``, ``type``, ``key``, ``title``, ``deadline``, ``log``,
``hash``; ``item`` adds ``content``.

What is listed: the OPEN questions of tickets in a FULL-mode workspace that are addressed to this user (their ``u_`` id,
the workspace owner's person id when the hub aliased the owner to this user, or a ticket role that resolves to one of
those). Gates and verdicts are not listed: their hash needs the body text, which hv 0 does not sync. Workspace text
leaves only through :func:`~orch_hub.contract.phase4.render_text` (L2P4-2d). Any failure reads as "nothing"
(the Protocol has no error channel); the hub never guesses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..contract import phase4 as P4
from ..contract.hub import Refused
from . import project as PR
from .service import Keyring, ViewService

_ITEM_ROLES = ("read", "edit", "admin")


class ViewReader(Protocol):
    def needs_you(self, workspace_id: str, user_id: str) -> list[dict[str, Any]]: ...

    def item(self, workspace_id: str, user_id: str, item_id: str) -> dict[str, Any] | None: ...


def _one_line(s: Any) -> str:
    t = P4.render_text(s if type(s) is str else "")
    return t.split("\n", 1)[0][:200]


class StoreViewReader:
    def __init__(
        self,
        service: ViewService,
        *,
        mode_of: Callable[[str], str | None],
        role_of: Callable[[str, str], str | None],
        owner_alias: Callable[[str], str | None],
    ) -> None:
        self._svc, self._mode_of, self._role_of, self._owner_alias = service, mode_of, role_of, owner_alias

    def needs_you(self, workspace_id: str, user_id: str) -> list[dict[str, Any]]:
        return [r for r, _ in self._scan(workspace_id, user_id)]

    def item(self, workspace_id: str, user_id: str, item_id: str) -> dict[str, Any] | None:
        for row, content in self._scan(workspace_id, user_id):
            if row["item_id"] == item_id:
                return {**row, "content": content}
        return None

    def _scan(self, ws: str, user_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        if self._mode_of(ws) != "full" or self._role_of(user_id, ws) not in _ITEM_ROLES:
            return []
        keys = Keyring(self._svc.seals, ws)
        try:
            with self._svc.store.lock:
                return self._scan_open(ws, user_id, keys)
        except Refused, KeyError, TypeError, ValueError:
            return []
        finally:
            keys.close()

    def _scan_open(self, ws: str, user_id: str, keys: Keyring) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        w = PR.project_workspace(self._svc.lines(ws, "workspace", keys))
        me = PR.identities(w, user_id, self._owner_alias(ws))
        out = []
        for log in self._svc.logs(ws):
            if log == "workspace":
                continue
            t = PR.project_ticket(self._svc.lines(ws, log, keys), ws, log)
            if t.closed or not PR.visible_to(t, me):
                continue
            for q in t.questions.values():
                if q.answered or not _addressed(t, q.obj.get("to"), me):
                    continue
                row = {
                    "item_id": q.qid,
                    "kind": "question",
                    "type": "question",
                    "key": _one_line(t.key),
                    "title": _one_line(q.obj.get("text")),
                    "deadline": None,
                    "log": log,
                    "hash": q.hash,
                }
                content = {
                    "ticket_title": _one_line(t.title),
                    "question": q.id,
                    "text": P4.render_text(q.obj.get("text", "")),
                    "why": P4.render_text(q.obj.get("why", "")),
                    "options": [
                        {k: P4.render_text(v) for k, v in o.items() if type(v) is str}
                        for o in q.obj.get("options") or []
                    ],
                    "recommended": _recommended(q.obj),
                    "blocking": q.obj.get("blocking") is True,
                }
                out.append((row, content))
        out.sort(key=lambda rc: (rc[0]["key"], rc[0]["item_id"]))
        return out


def _addressed(t: PR.Ticket, to: Any, me: set[str]) -> bool:
    if type(to) is not str:
        return False
    if to in PR.TICKET_ROLES:
        return bool(me & set(t.people[to]))
    return to in me


def _recommended(q: dict[str, Any]) -> str | None:
    """Only a string that is one of the question's own option keys (then rendered); anything else is None."""
    r = q.get("recommended")
    keys = {o.get("key") for o in q.get("options") or [] if type(o) is dict}
    return P4.render_text(r) if type(r) is str and r in keys else None

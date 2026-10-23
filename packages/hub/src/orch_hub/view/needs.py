"""The needs-you source for D13's person home (``orch_hub.webapi``, orch-hub PR #29, branch ``v3-d13-webapi``).

:class:`ViewReader` below is a MATCHING PROTOCOL defined here because PR #29 is not merged yet (it must stay identical
to ``webapi.api.ViewReader``): ``needs_you(workspace_id, user_id) -> list[row]`` and ``item(workspace_id, user_id,
item_id) -> row | None``. A row has ``item_id`` (32 hex), ``kind``, ``type``, ``key``, ``title``, ``deadline``, ``log``,
``hash``; ``item`` adds ``content``.

D15b-3: GATE and VERDICT rows come from the host's ticket projection P (§10.7), only for tickets that pass the one
visibility rule, only while P describes the held head and the hub's own ``roster_v`` (a stale P lists nothing), and not
for a hash this person already decided. Each row carries the hash it was listed with (``hash``) and the channel's
action sends that hash, never one re-read at the tap, so a moved hash is ``event_changed``. P has no eligibility
(D59), so the host may still refuse a listed decision (backlog issue).

What is listed: the OPEN questions of tickets in a FULL-mode workspace that are addressed to this user (their ``u_`` id,
the workspace owner's person id when the hub aliased the owner to this user, or a ticket role that resolves to one of
those). Workspace text
leaves only through :func:`~orch_hub.contract.phase4.render_text` (L2P4-2d). Any failure reads as "nothing"
(the Protocol has no error channel); the hub never guesses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..contract import phase4 as P4
from ..contract import suite as S
from ..contract.hub import Refused
from . import project as PR
from .service import Keyring, ViewService

_ITEM_ROLES = ("read", "edit", "admin")


class ViewReader(Protocol):
    def needs_you(self, workspace_id: str, user_id: str) -> list[dict[str, Any]]: ...

    def item(self, workspace_id: str, user_id: str, item_id: str) -> dict[str, Any] | None: ...


def _short(h: Any) -> str:
    """Protocol 8.4 short form of a content hash (12 lower-case hex in groups of 4); "" when it is not a hash."""
    try:
        return P4.short_hash(h)
    except Refused:
        return ""


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
        except Exception:  # noqa: BLE001 - the Protocol has no error channel: any failure reads as nothing
            return []
        finally:
            keys.close()

    def _scan_open(self, ws: str, user_id: str, keys: Keyring) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        w = PR.project_workspace(self._svc.lines(ws, "workspace", keys))
        me = PR.identities(w, user_id, self._owner_alias(ws))
        out: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for log in self._svc.logs(ws):
            if log == "workspace":
                continue
            lines = self._svc.lines(ws, log, keys)
            t = PR.project_ticket(lines, ws, log)
            if t.closed or not PR.visible_to(t, me):
                continue
            out += self._decision_rows(ws, log, t, lines, me, w.roster_v, keys)
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
                        {k: P4.render_text(o[k]) for k in ("key", "label") if type(o.get(k)) is str}
                        for o in q.obj.get("options") or []
                    ],
                    "recommended": _recommended(q.obj),
                    "blocking": q.obj.get("blocking") is True,
                    "short_hash": _short(q.hash),
                }
                out.append((row, content))
        out.sort(key=lambda rc: (rc[0]["key"], rc[0]["item_id"]))
        return out

    def _decision_rows(
        self, ws: str, log: str, t: PR.Ticket, lines: list[dict[str, Any]], me: set[str], roster_v: int, keys: Keyring
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Gate and verdict rows of one visible ticket from its held projection P (D15b-3)."""
        proj = self._svc.proj(ws, log, keys)
        heads = self._svc.store.heads(ws, log)
        if proj is None or proj.get("seq") != len(heads) or proj.get("head") != heads[-1]:
            return []
        if proj.get("roster_v") != roster_v:
            return []
        mine = {
            (e.get("gate", "verify") if e["type"] != "verdict.given" else "verify", e.get("hash"))
            for e in lines
            if e.get("type") in ("gate.approved", "gate.changes_requested", "verdict.given")
            and (e.get("actor") or {}).get("id") in me
        }
        rows = []
        for gate, g in sorted(proj["gates"].items()):
            if gate == "verify":
                if proj["status"] != "testing":
                    continue
                kind = "verdict"
            elif proj["status"] in ("done", "closed"):
                continue
            else:
                kind = "gate"
            if (gate, g["hash"]) in mine:
                continue
            item_id = S.sha256(
                ("orch-hub|needs-" + kind + "|" + ws + "|" + log + "|" + gate + "|" + g["hash"]).encode()
            )[:16].hex()
            row = {
                "item_id": item_id,
                "kind": kind,
                "type": kind,
                "key": _one_line(t.key),
                "title": _one_line(t.title),
                "deadline": None,
                "log": log,
                "hash": g["hash"],  # the hash the row was listed with: the action sends exactly this
            }
            content = {
                "ticket_title": _one_line(t.title),
                "gate": gate,
                "short_hash": _short(g["hash"]),
                "gate_gen": g["gen"],
                "sources": [
                    {"repo": P4.render_text(e["repo"]), "sha": P4.render_text(e["sha"][:8])}
                    for e in g.get("source_sha") or []
                ],
            }
            rows.append((row, content))
        return rows


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

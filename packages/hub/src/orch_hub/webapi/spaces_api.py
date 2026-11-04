"""Shared spaces, the console's part (M3, #1443 #1445): create a space, add and remove a member, read one space's
history. A mixin of :class:`~orch_hub.webapi.api.WebApi` (it only uses ``self.d``, ``self._spaces`` and ``self._record``)
plus its route table ``SPACE_ROUTES``.

The console signs nothing, puts nothing and restores nothing: it calls the hub's space service, which signs ``hub.space``
with HK_op as it always did. Who may ask is the service's rule (a hub admin, or an admin of every member). Every POST
passes the same CSRF and origin guard as the rest of the console. History is READ ONLY: the hub opens the items it stores
(option B); the author's ``by`` is what the workspace wrote, so it is shown through a fixed table and never trusted.
Getting an older version back is a host action (``space.restore``), never a route here (test: the route table)."""

from __future__ import annotations

import re
from typing import Any

from ..contract import phase4 as P4
from ..contract.canon import CanonError, unb64u
from ..spaces.service import SpaceError

TYPES = ("wiki", "files", "board")
NAME_MAX, PURPOSE_MAX = 64, 200
TEXT_MAX = 4096  # bytes of one version's text shown; more is cut
NAMES_MAX, VERSIONS_MAX = 100, 30  # per history answer: names, and versions of one name
_BY = re.compile(r"(person|agent):[A-Za-z0-9_-]{1,64}")
_ID16 = re.compile(r"[0-9a-f]{32}")
_FIELDS_CREATE = frozenset({"type", "name", "purpose", "members", "understood"})


def _bad(what: str = "") -> SpaceError:
    return SpaceError("malformed", what)


def by_kind(by: Any) -> str:
    """The fixed table for an item's ``by``: ``person``, ``agent`` or ``unknown``. The id is never shown or returned."""
    m = _BY.fullmatch(by) if type(by) is str else None
    return m[1] if m else "unknown"


def _text_of(raw: bytes) -> str | None:
    """UTF-8 text without control characters (other than tab, LF, CR), else None."""
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return s if not any(ord(c) < 32 and c not in "\t\n\r" or ord(c) == 127 for c in s) else None


def content_of(body: dict[str, Any]) -> dict[str, Any]:
    """What the console may show of one item: ``{"text", "cut"}`` (at most 4 KB of UTF-8 text) or ``{"file": size}``.
    Never a link, never markup: the page prints it with textContent."""
    data = body.get("data")
    try:
        raw = unb64u(data) if type(data) is str else None
    except CanonError:
        raw = None
    if raw is None:
        return {"file": 0}
    txt = _text_of(raw)
    if txt is None:
        return {"file": len(raw)}
    cut = len(raw) > TEXT_MAX
    if cut:
        txt = raw[:TEXT_MAX].decode("utf-8", "ignore")
    return {"text": P4.render_text(txt), "cut": cut}


class SpaceHandlers:
    d: Any
    _spaces: Any
    _record: Any

    def _ws_name(self, ws: str) -> str | None:
        n = (self.d.registry.card(ws) or {}).get("name")
        return P4.render_text(n) if type(n) is str and n else None

    @staticmethod
    def _ids(x: Any, limit: int) -> list[str]:
        if (
            type(x) is not list
            or not x
            or len(x) > limit
            or any(type(w) is not str or not _ID16.fullmatch(w) for w in x)
        ):
            raise _bad("members")
        return sorted(set(x))

    def _h_a_space_create(self, c: Any, p: dict, q: dict, b: dict | None):
        if b is None or not {"type", "name", "members", "understood"} <= set(b) <= _FIELDS_CREATE:
            raise _bad()
        if b["understood"] is not True:  # the tick: "The hub and its admins can read this space."
            raise _bad("understood")
        kind, name, purpose = b["type"], b["name"], b.get("purpose", "")
        if kind not in TYPES or type(name) is not str or not name.strip() or len(name) > NAME_MAX:
            raise _bad("type or name")
        if type(purpose) is not str or len(purpose) > PURPOSE_MAX or not purpose.isprintable():
            raise _bad("purpose")
        members = self._ids(b["members"], 64)
        reg = self.d.registry
        if any(reg.mode(w) is None for w in members):
            raise SpaceError("unknown_workspace")
        # a selective workspace joins only with its own owner's accept at its host: it waits, it is not added here
        now_in = [w for w in members if reg.mode(w) == "full"]
        waiting = [w for w in members if reg.mode(w) != "full"]
        if not now_in:
            raise SpaceError("wrong_mode", "no member can be added now")
        rec = self._spaces().create(name.strip(), now_in, c.user_id, kind, purpose.strip())
        self._record("space.created", actor_id=c.user_id, detail={"space": rec["space_id"]})
        return 200, {
            "space_id": rec["space_id"],
            "members": rec["members"],
            "waiting": [{"workspace_id": w, "name": self._ws_name(w)} for w in waiting],
        }

    def _h_a_space_about(self, c: Any, p: dict, q: dict, b: dict | None):
        return 200, {"about": self._spaces().store.metas()}

    def _h_a_space_member_add(self, c: Any, p: dict, q: dict, b: dict | None):
        if b is None or set(b) != {"workspace_id"} or type(b["workspace_id"]) is not str:
            raise _bad()
        ws = self._ids([b["workspace_id"]], 1)[0]
        try:
            rec = self._spaces().add(p["space_id"], ws, c.user_id)
        except SpaceError as e:
            if e.code == "already_member":
                raise SpaceError("exists") from None
            if e.code == "not_accepted":  # its owner has not accepted at the host yet: nothing changed
                return 200, {"added": False, "waiting": [{"workspace_id": ws, "name": self._ws_name(ws)}]}
            raise
        self._record("space.member_added", actor_id=c.user_id, workspace_id=ws, detail={"space": p["space_id"]})
        return 200, {"added": True, "members": rec["members"], "waiting": []}

    def _h_a_space_member_remove(self, c: Any, p: dict, q: dict, b: dict | None):
        if b is None or set(b) != {"workspace_id"} or type(b["workspace_id"]) is not str:
            raise _bad()
        ws = self._ids([b["workspace_id"]], 1)[0]
        rec = self._spaces().remove(p["space_id"], ws, c.user_id)
        self._record("space.member_removed", actor_id=c.user_id, workspace_id=ws, detail={"space": p["space_id"]})
        return 200, {"ended": rec is None, "members": [] if rec is None else rec["members"]}

    def _h_a_space_history(self, c: Any, p: dict, q: dict, b: dict | None):
        want = q.get("name")
        if want is not None and (not want or len(want) > 256):
            raise _bad("name")
        rows = self._spaces().read_items(p["space_id"])
        names: dict[str, list[dict[str, Any]]] = {}
        removed = []
        for r in rows:  # newest first
            who = self._ws_name(r["ws"])
            if r["revoked"]:
                removed.append({"item_id": r["item_id"], "author": who, "revoked": True})
                continue
            o = r["item"]
            if o is None:  # does not verify against the pins: not shown as content
                continue
            body = o["body"]
            name = body.get("name") if type(body.get("name")) is str and body.get("name") else "(unnamed)"
            if want is not None and name != want:
                continue
            if name not in names and len(names) >= NAMES_MAX:
                continue
            versions = names.setdefault(name, [])
            if len(versions) >= VERSIONS_MAX:
                continue
            versions.append(
                {
                    "item_id": r["item_id"],
                    "created_ms": o["created_ms"],  # the signed time, not the hub's order
                    "author": who,
                    "by": by_kind(body.get("by")),  # as the workspace says; the hub cannot check it
                    "restored": type(body.get("restored_from")) is str and bool(_ID16.fullmatch(body["restored_from"])),
                    "revoked": False,
                    "content": content_of(body),
                }
            )
        for versions in names.values():
            versions.sort(key=lambda v: -v["created_ms"])
        return 200, {
            "names": [{"name": P4.render_text(n), "versions": v} for n, v in names.items()],
            "removed": removed[:VERSIONS_MAX],
        }


def space_routes(route: Any) -> tuple[Any, ...]:
    """The routes, built with ``api._r`` (passed in, so this module does not import ``api`` and no cycle appears). Not one
    of them signs as a workspace, puts an item or restores one."""
    return (
        route(
            "POST", "/admin/spaces", "admin", "a_space_create", "Create a shared space (the admin has read the notice)"
        ),
        route("GET", "/admin/spaces/about", "admin", "a_space_about", "What each shared space is for (type, purpose)"),
        route("POST", "/admin/spaces/{space_id}/members", "admin", "a_space_member_add", "Add a workspace to a space"),
        route(
            "POST",
            "/admin/spaces/{space_id}/members/remove",
            "admin",
            "a_space_member_remove",
            "Remove a workspace from a space",
        ),
        route(
            "GET", "/admin/spaces/{space_id}/history", "admin", "a_space_history", "Versions of what is in one space"
        ),
    )

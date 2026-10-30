"""The shared spaces this workspace is in, read-only (run-15c P03).

| Method | Path                       | Host op                  | Shape                                                        |
|--------|----------------------------|--------------------------|--------------------------------------------------------------|
| GET    | /api/w/{ws}/spaces         | space.list               | `{"spaces": [{id, name, kind, members}], "note"}`            |
| GET    | /api/w/{ws}/spaces/{id}    | space.list {space_id}    | `{"id", "items": [{name, size}], "refused", "note"}`         |

Reads only: no signature, no write. Adding or editing is done in the hub. The space name and kind are the hub's own
display data (the signed `hub.space` record has none), so the answer says "from the hub, not signed". The items are
untrusted data written by other workspaces: names are closed to printable text without hidden characters, nothing
is rendered as markup or taken as an instruction, and the content of an item is never sent to the page. Without a
joined hub nothing is listed, and that is no error.
"""

from __future__ import annotations

import re

from fastapi import Depends, FastAPI

from . import reads
from .adapters.addons import display
from .errors import HostError, fail
from .gateway import Gateway

SPACE_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_SPACES = 100
MAX_ITEMS = 500
NOTE = "from the hub, not signed"
#: hub answers that mean "no hub to ask", not a failure (any other error, `hub.unreachable` included, still surfaces)
NO_HUB = ("hub.not_joined", "hub.state_mismatch")


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def spaces_view(raw) -> dict:
    """`space.list`: closed fields, cleaned text. A space without a usable id is dropped; a name that is not plain
    text is shown as an unnamed space (the page says "Shared space")."""
    raw = raw if isinstance(raw, dict) else {}
    out = []
    for r in (raw.get("spaces") if isinstance(raw.get("spaces"), list) else [])[:MAX_SPACES]:
        if not isinstance(r, dict) or not isinstance(r.get("space_id"), str) or not SPACE_ID.fullmatch(r["space_id"]):
            continue
        name, kind = display(r.get("name")), display(r.get("kind"))
        members = r.get("members")
        out.append({
            "id": r["space_id"], "name": (name or "")[:64], "kind": (kind or "")[:32],
            "members": len(members) if isinstance(members, list) else 0,
        })  # fmt: skip
    return {"spaces": out, "note": NOTE}


def items_view(sid: str, raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    items = []
    for r in (raw.get("items") if isinstance(raw.get("items"), list) else [])[:MAX_ITEMS]:
        if not isinstance(r, dict) or r.get("revoked") is True:
            continue
        name = display(r.get("name"))
        if name:
            items.append({"name": name[:200], "size": _int(r.get("size"))})
    return {"id": sid, "items": items, "refused": _int(raw.get("refused")), "note": NOTE}


def register(app: FastAPI, gateway: Gateway):
    @app.get("/api/w/{ws}/spaces")
    def spaces(ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("space.list", "hub/1"):
            return spaces_view({})
        try:
            return spaces_view(link.call("space.list"))
        except HostError as exc:
            if exc.error["code"] in NO_HUB:
                return spaces_view({})
            raise

    @app.get("/api/w/{ws}/spaces/{sid}")
    def space_items(sid: str, ws: str = Depends(reads.workspace)):
        if not SPACE_ID.fullmatch(sid):
            raise fail("invalid.input", "That is not a shared space.", exit=2)
        link = gateway.enabled(ws)
        try:
            return items_view(sid, link.call("space.list", {"space_id": sid}))
        except HostError as exc:
            if exc.error["code"] in NO_HUB:
                return items_view(sid, {})
            raise

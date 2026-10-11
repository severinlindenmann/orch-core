"""The read routes of #305 (M2): a closed allowlist, one typed route per host op, adapters between.

Nothing here forwards an operation name or free-form arguments from the browser: every route has a pydantic input
model that forbids extras, and the host op and its arguments are fixed by the route.
"""

import base64
import hashlib
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import adapters
from .errors import fail
from .gateway import WORKSPACE_ID, Gateway

Key = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}$", max_length=26)]
Person = Annotated[str, StringConstraints(pattern=r"^p_[A-Za-z0-9]{1,64}$")]
Word = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")]
ArtifactName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
UiStatus = Literal["backlog", "open", "in-progress", "testing", "done"]
MAX_ROWS = 1000  # board and Today read at most this many rows; a larger workspace shows the newest
CHUNK = 786432
MAX_ARTIFACT = 3 * CHUNK


class TicketsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: list[UiStatus] | None = Field(default=None, min_length=1, max_length=5)
    type: list[Word] | None = Field(default=None, min_length=1, max_length=16)
    label: list[Word] | None = Field(default=None, min_length=1, max_length=16)
    assignee: Person | None = None
    owner: Person | None = None
    parent: Key | None = None
    epic: Key | None = None
    q: str | None = Field(default=None, min_length=1, max_length=200)
    needs_me: bool | None = None
    limit: int = Field(default=50, ge=1, le=200)
    page: str | None = Field(default=None, max_length=512, pattern=r"^[A-Za-z0-9_.=-]+$")
    sort: Literal["updated", "priority", "key"] = "updated"
    order: Literal["asc", "desc"] = "desc"


class EventsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: int = Field(default=0, ge=0, le=2**31)
    limit: int = Field(default=100, ge=1, le=500)


class ChangesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: Word | None = None
    context: int = Field(default=3, ge=0, le=10)


class ContentQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


def workspace(ws: str):
    if not WORKSPACE_ID.fullmatch(ws):
        raise fail("invalid.input", exit=5)
    return ws


def host_args(query: TicketsQuery) -> dict:
    args = query.model_dump(exclude_none=True)
    args["status"] = [adapters.host_status(s) for s in args.get("status") or adapters.board_statuses()]
    return args


def all_rows(gateway: Gateway, ws: str, args: dict, cap: int = MAX_ROWS):
    """Every row of one snapshot (the first page fixes it), at most `cap`."""
    rows, page, last = [], None, {}
    while True:
        data = gateway.call(ws, "ticket.query", {**args, "limit": 200, **({"page": page} if page else {})})
        rows += data["rows"]
        last = data
        page = data["next_page"]
        if not page or len(rows) >= cap:
            return rows[:cap], last


def register(app: FastAPI, gateway: Gateway):
    def viewer(ws):
        return gateway.link(ws).welcome["peer"]["person"]

    def needs(ws):
        return gateway.call(ws, "needs")

    @app.get("/api/w/{ws}/today")
    def today(ws: str = Depends(workspace)):
        who = viewer(ws)
        data = needs(ws)
        rows, last = all_rows(gateway, ws, {"status": adapters.board_statuses(), "sort": "updated"})
        role = next((m.role for m in adapters.members(gateway.call(ws, "member.list")) if m.person == who), "viewer")
        return adapters.today(data, rows, ws, last.get("as_of") or "", who, role)

    def listing(ws: str, query: TicketsQuery):
        who = viewer(ws)
        data = gateway.call(ws, "ticket.query", host_args(query))
        return adapters.page(data, needs(ws), who)

    @app.get("/api/w/{ws}/tickets")
    def tickets(query: Annotated[TicketsQuery, Query()], ws: str = Depends(workspace)):
        return listing(ws, query)

    @app.get("/api/w/{ws}/board")
    def board(query: Annotated[TicketsQuery, Query()], ws: str = Depends(workspace)):
        return listing(ws, query)

    def mine(ws):
        out = {}
        for n in needs(ws)["items"]:
            if n.get("uid"):
                out.setdefault(n["uid"], []).append(n)
        return out

    @app.get("/api/w/{ws}/tickets/{key}")
    def ticket(key: Key, ws: str = Depends(workspace)):
        data = gateway.call(ws, "ticket.get", {"ref": key})
        if data["doc"]["status"] == "closed":
            # The UI has no closed state yet (HANDOVER, M2 concerns): refuse plainly, never draw it as another status.
            raise fail("ticket.closed", "Closed tickets are not shown in the dashboard yet.", exit=3)
        return adapters.document(data, viewer(ws), mine(ws))

    @app.get("/api/w/{ws}/tickets/{key}/events")
    def ticket_events(key: Key, query: Annotated[EventsQuery, Query()], ws: str = Depends(workspace)):
        data = gateway.call(ws, "ticket.get", {"ref": key, "log_since": query.since, "log_limit": query.limit})
        return adapters.events(data)

    @app.get("/api/w/{ws}/tickets/{key}/changes")
    def ticket_changes(key: Key, query: Annotated[ChangesQuery, Query()], ws: str = Depends(workspace)):
        args = {"ref": key, "context": query.context, **({"repo": query.repo} if query.repo else {})}
        return adapters.changes(gateway.call(ws, "diff", args), key)

    @app.get("/api/w/{ws}/tickets/{key}/artifacts/{name}")
    def artifact(key: Key, name: ArtifactName, query: Annotated[ContentQuery, Query()], ws: str = Depends(workspace)):
        """An evidence file, read in chunks up to a cap, its digest checked against the one the ticket names."""
        chunks, offset = [], 0
        while True:
            part = gateway.call(ws, "artifact.get", {"ref": key, "name": name, "offset": offset, "length": CHUNK})
            raw = base64.b64decode(part["b64"], validate=True)
            chunks.append(raw)
            offset += len(raw)
            if part["last"] or offset >= MAX_ARTIFACT:
                break
        data = b"".join(chunks)
        complete = bool(part["last"])
        digest = part["digest"]
        verified = complete and hashlib.sha256(data).hexdigest() == digest.removeprefix("sha256:")
        if complete and not verified:
            raise fail("artifact.mismatch", "The file does not match its recorded digest.", exit=3)
        try:
            # A cut-off file may end inside a character: drop that tail instead of calling the file binary.
            text, binary = data.decode("utf-8", errors="ignore" if not complete else "strict"), False
        except UnicodeDecodeError:
            text, binary = None, True
        return {
            "name": name,
            "size": part["size"],
            "digest": digest,
            "verified": verified,
            "truncated": not complete,
            "binary": binary,
            "text": text,
        }

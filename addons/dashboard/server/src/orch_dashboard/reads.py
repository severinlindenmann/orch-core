"""The read routes of #305 (M2): a closed allowlist, one typed route per host op, adapters between.

Nothing here forwards an operation name or free-form arguments from the browser: every route has a pydantic input
model that forbids extras, and the host op and its arguments are fixed by the route.
"""

import base64
import hashlib
import re
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
UiStatus = Literal["backlog", "open", "in-progress", "testing", "done", "closed"]
MAX_ROWS = 1000  # board and Today read at most this many rows; a larger workspace shows the newest
MAX_PAGES = 30  # a page may hold fewer rows than asked (rows drop out between pages): follow next_page, but never loop
RECENT = 6  # Today's "Recently" shows this many activity rows
RECENT_PAGES = 5
CHUNK = 786432
MAX_ARTIFACT = 7 * CHUNK  # 5.25 MiB: a pasted screenshot (at most 5 MiB) is read whole


class TicketsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: list[UiStatus] | None = Field(default=None, min_length=1, max_length=6)
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
    """Every row of one snapshot (the first page fixes it), at most `cap`. Pages may be short or empty: only
    `next_page` says whether more follow, never a count or a page size."""
    rows, page, last = [], None, {}
    for _ in range(MAX_PAGES):
        data = gateway.call(ws, "ticket.query", {**args, "limit": 200, **({"page": page} if page else {})})
        rows += data["rows"]
        last = data
        page = data["next_page"]
        if not page or len(rows) >= cap:
            break
    return rows[:cap], last


def recent_activity(gateway: Gateway, ws: str, want: int = RECENT):
    """Today's feed: ticket events only (`scope: tickets`; workspace-log events name members, devices and settings and
    belong on no ticket row). A page may be short under the host's scan budget, so follow `next_page`."""
    link = gateway.link(ws)
    if not link.supports("activity.list", "activity/1"):
        return []
    items, page, seen = [], None, set()
    for _ in range(RECENT_PAGES):
        data = gateway.call(ws, "activity.list", {"scope": "tickets", "limit": 50, **({"page": page} if page else {})})
        for a in data["items"]:
            if a["id"] not in seen:  # the live tail dedupes by id; so does this read
                seen.add(a["id"])
                items.append(a)
        page = data["next_page"]
        if not page or len(items) >= want:
            break
    return items[:want]


PASTE_NAME = re.compile(r"^paste-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.(png|webp)$")


def pasted_by_person(gateway: Gateway, ws: str, key: str, name: str, digest: str) -> bool:
    """R3 part B (lead decision): a picture is drawn inline only when a PERSON signed its `artifact.added` as a
    screenshot with a paste name. An agent's image stays a placeholder with a link to the artifact."""
    if not PASTE_NAME.fullmatch(name):
        return False
    data = gateway.call(ws, "ticket.get", {"ref": key})
    art = next((a for a in data.get("artifacts") or [] if a.get("name") == name), None)
    return bool(
        art
        and art.get("kind") == "screenshot"
        and (art.get("by") or {}).get("kind") == "person"
        and art.get("digest") == digest  # the bytes read are the ones the ticket names for THIS signed artifact
    )


def picture(data: bytes) -> dict | None:
    from orch.host import imagecheck

    try:
        kind = imagecheck.check(data)
    except imagecheck.ImageRefused:
        return None
    return {
        "type": "png" if kind == "image/png" else "webp",
        "b64": base64.b64encode(data).decode(),
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


def media(data: bytes) -> dict | None:
    """The bytes of an evidence picture (PNG, JPEG, GIF, WebP, SVG), typed by its first bytes, never by the name.

    Run only on a complete file whose sha256 matched the ticket's record. It is a read-only offer: no decoding here, and
    the page hashes the bytes again and shows them only as an <img> from a data: URL (an image context runs no script,
    loads nothing, and SVG is never drawn as markup).
    """
    head = data[:512]
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        kind = "jpeg"
    elif data[:4] == b"GIF8":
        kind = "gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = "webp"
    elif (lead := head.lstrip().lower()).startswith(b"<svg") or (
        lead.startswith((b"<?xml", b"<!doctype svg")) and b"<svg" in data[:4096].lower()
    ):
        kind = "svg"
    else:
        return None
    return {
        "type": kind,
        "b64": base64.b64encode(data).decode(),
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
    }


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
        # An open question on a closed ticket is still a need: read the closed rows only then, for their titles.
        closed = []
        if any(n.get("status") == "closed" for n in data["items"]):
            closed, _ = all_rows(gateway, ws, {"status": ["closed"], "sort": "updated"})
        from .hubapprove import HUB_ONLY, mode_of

        sign = "hub" if mode_of(gateway, ws) in HUB_ONLY else None  # V3 C11: full hub mode, approved elsewhere
        return adapters.today(
            data, rows, ws, last.get("as_of") or "", who, role, recent_activity(gateway, ws), closed, sign
        )

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
        # A plain read of a visible closed or done ticket never errors (v1.4 section 0.8.4): the page draws its state.
        data = gateway.call(ws, "ticket.get", {"ref": key})
        return adapters.document(data, viewer(ws), mine(ws), ext=gateway.link(ws).has("read_ext/1"))

    @app.get("/api/w/{ws}/tickets/{key}/events")
    def ticket_events(key: Key, query: Annotated[EventsQuery, Query()], ws: str = Depends(workspace)):
        data = gateway.call(ws, "ticket.get", {"ref": key, "log_since": query.since, "log_limit": query.limit})
        from .adapters.activity import acceptance_authority

        roster = gateway.call(ws, "member.list")
        return adapters.events(data, acceptance_authority(roster, data["doc"]))

    @app.get("/api/w/{ws}/tickets/{key}/changes")
    def ticket_changes(key: Key, query: Annotated[ChangesQuery, Query()], ws: str = Depends(workspace)):
        args = {"ref": key, "context": query.context, **({"repo": query.repo} if query.repo else {})}
        return adapters.changes(gateway.call(ws, "diff", args), key)

    @app.get("/api/w/{ws}/tickets/{key}/artifacts/{name}")
    def artifact(key: Key, name: ArtifactName, query: Annotated[ContentQuery, Query()], ws: str = Depends(workspace)):
        """An evidence file, read in chunks up to a cap, its digest checked against the one the ticket names."""
        chunks, offset = [], 0
        for _ in range(MAX_ARTIFACT // CHUNK + 1):
            part = gateway.call(ws, "artifact.get", {"ref": key, "name": name, "offset": offset, "length": CHUNK})
            raw = base64.b64decode(part["b64"], validate=True)
            if not raw and not part["last"]:
                raise fail("internal")  # a host that makes no progress must not pin a worker thread
            chunks.append(raw)
            offset += len(raw)
            if part["last"] or offset >= MAX_ARTIFACT:
                break
        else:
            raise fail("internal")
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
            # R3 part B: a screenshot pasted into the ticket. Only a complete, verified PNG/WebP that passes core's
            # header check is offered; the page hashes the bytes itself before showing them, and only ever as an <img>.
            "image": picture(data) if verified and pasted_by_person(gateway, ws, key, name, digest) else None,
            # Evidence preview (#1069): any complete, digest-verified picture, whoever added it. The page re-hashes it.
            "media": media(data) if verified else None,
        }

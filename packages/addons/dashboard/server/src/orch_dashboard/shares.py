"""Shares (the Publish add-on, design-publish-addon section 2.2): share files of a ticket with a link.

Typed routes with closed bodies, like the worktrees. The host op and its arguments are fixed here; the browser never
names an op. What each route does:

* `GET  /shares`          `publish.list {live}`: the settings, the shares and the staged copies. **Never a link**: the
                          host keeps no link, and this projection has no field that could carry one.
* `POST /shares/upload`   `publish.stage_upload`: a file the person picked on this computer (P05), kept aside by the
                          host; one file per call, at most 1 MiB. Nothing is shared; `prepare` may name it.
* `POST /shares/prepare`  `publish.prepare`: copy named artifacts (and picked files) aside. Nothing leaves the computer.
* `POST /shares/send`     `publish.send`: after a person's signature (the `publish_share` hand-off in handoff.py), the
                          host uploads and answers the link ONCE. This is the only route whose answer holds a link.
* `POST /shares/revoke`, `/shares/discard`, `/shares/register`.

**The link.** `send` is the one place this process sees a link. It is passed through to that one response and nowhere
else: it is not logged (the server runs with access logs off and no handler writes a body), not cached (`/api` answers
carry `Cache-Control: no-store`), not stored in any attribute, and not part of an error (a failed send has no link; the
host's error text is shown through the closed error filter, which never echoes a body). The test suite greps this
process's captured logs and heap-visible state for the token.

Host text is display-only: hidden characters are withheld, never repaired.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from . import reads
from .adapters.addons import display
from .cosmetic_routes import full_session, refused
from .errors import HostError, fail
from .gateway import Gateway
from .terminals import Idem

#: core `orch.publishclient.approval.NAME`: the alphabet the server takes for a file name
FileName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,99}$")]
UploadId = Annotated[str, StringConstraints(pattern=r"^pu_[0-9a-f]{32}$")]
#: core `publishing.UPLOAD_BYTES` (1 MiB) as base64; the host holds the exact limit
UPLOAD_B64_MAX = (1024 * 1024 + 2) // 3 * 4
StagedId = Annotated[str, StringConstraints(pattern=r"^ps_[0-9a-f]{32}$")]
ShareId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
KINDS = ("secret", "once")
DAYS = (1, 3, 7, 30)
VIEWS = (1, 3, 10)
STATES = ("live", "sending", "revoked", "expired")
#: core `publishOrigin` (schemas/common.json): https, or http on this computer only; no path
ORIGIN = re.compile(
    r"^(?:https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[1-9][0-9]{0,4})?"
    r"|http://(?:localhost|127\.0\.0\.1|10\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|172\.(?:1[6-9]|2[0-9]|3[01])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|192\.168\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]))(?::[1-9][0-9]{0,4})?)$"
)
HEX = re.compile(r"^[0-9a-f]{64}$")
WORKSPACE = re.compile(r"^[0-9a-f]{32}$")
PERSON = re.compile(r"^p_[A-Za-z0-9]{1,64}$")
TICKET = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}$")
TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
#: the ops this addon needs of the host, all under cap `publish/1`
OPS = ("publish.list", "publish.prepare", "publish.send", "publish.revoke", "publish.discard", "publish.register")


class ListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: reads.Key | None = None


class UploadBody(BaseModel):
    """One picked file: its plain name and its bytes as base64. One file per call; the host checks the size, the name
    and the bytes again."""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: FileName
    data: Annotated[str, StringConstraints(min_length=4, max_length=UPLOAD_B64_MAX, pattern=r"^[A-Za-z0-9+/]+={0,2}$")]
    idem: Idem


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket: reads.Key
    artifacts: Annotated[list[FileName], Field(max_length=20)] = []
    uploads: Annotated[list[UploadId], Field(max_length=20)] = []
    kind: Literal["secret", "once"] = "secret"
    days: Literal[1, 3, 7, 30] = 7
    views: Literal[1, 3, 10] | None = None
    idem: Idem

    @model_validator(mode="after")
    def shape(self):
        if not self.artifacts and not self.uploads:
            raise ValueError("no files")
        if len({a.lower() for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("different files")
        if len(set(self.uploads)) != len(self.uploads) or len(self.artifacts) + len(self.uploads) > 20:
            raise ValueError("different files")
        if (self.kind == "once") != (self.views is not None):
            raise ValueError("views belong to a show-once link only")
        return self

    def host_args(self) -> dict:
        args = {"ref": self.ticket, "kind": self.kind, "days": self.days}
        if self.artifacts:
            args["artifacts"] = self.artifacts
        if self.uploads:
            args["uploads"] = self.uploads
        if self.views is not None:
            args["views"] = self.views
        return args


class StagedBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    staged_id: StagedId
    idem: Idem


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    share_id: ShareId
    idem: Idem


class RegisterBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    idem: Idem


def _int(value, low=0, high=10**12):
    return value if type(value) is int and low <= value <= high else None


def _time(value):
    return value if isinstance(value, str) and TIME.fullmatch(value) else None


def file_row(data) -> dict | None:
    """One staged file: its plain name, its size and its hash (the review shows the same three)."""
    if not isinstance(data, dict):
        return None
    name, size, digest = display(data.get("name")), _int(data.get("size")), data.get("sha256")
    if name is None or size is None or not isinstance(digest, str) or not HEX.fullmatch(digest):
        return None
    return {"name": name, "size": size, "sha256": digest}


def share_row(data, titles: dict | None = None) -> dict | None:
    """One share of `publish.list`. Closed: a field the host adds later does not reach the page, and there is no
    link or token field in this shape."""
    if not isinstance(data, dict):
        return None
    ident = data.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
        return None
    kind = data.get("kind")
    state = data.get("state")
    key = data.get("ticket")
    files = data.get("files") if isinstance(data.get("files"), list) else []
    return {
        "id": ident,
        "ticket": display(key) if isinstance(key, str) and TICKET.fullmatch(key) else None,
        "title": display((titles or {}).get(key)),
        "kind": kind if kind in KINDS else "unknown",
        "state": state if state in STATES else "unknown",
        "expires_at": _time(data.get("expires_at")),
        "view_limit": _int(data.get("view_limit"), 0, 1000),
        "views": _int(data.get("views"), 0, 10**9),
        "files": [n for n in (display(f) for f in files[:50]) if n],
        "created_at": _time(data.get("created_at")),
        "server": display(data.get("server")),
    }


def upload_row(data) -> dict | None:
    """`publish.stage_upload`'s answer: the id to name in `prepare`, and the plain name, size and hash."""
    if not isinstance(data, dict):
        return None
    ident = data.get("upload_id")
    if not isinstance(ident, str) or not re.fullmatch(r"pu_[0-9a-f]{32}", ident):
        return None
    row = file_row(data)
    return {"upload_id": ident, **row} if row else None


def staged_row(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    ident = data.get("staged_id")
    if not isinstance(ident, str) or not re.fullmatch(r"ps_[0-9a-f]{32}", ident):
        return None
    files = [f for f in (file_row(f) for f in (data.get("files") or [])[:20]) if f]
    key = data.get("ticket")
    kind = data.get("kind")
    return {
        "staged_id": ident,
        "ticket": display(key) if isinstance(key, str) and TICKET.fullmatch(key) else None,
        "kind": kind if kind in KINDS else "unknown",
        "days": data.get("days") if data.get("days") in DAYS else None,
        "views": data.get("views") if data.get("views") in VIEWS else None,
        "origin": data.get("origin") if data.get("origin") in ("agent", "person") else None,
        "files": files,
        "created_at": _time(data.get("created_at")),
    }


def origin_of(value):
    """The server or share address as the host holds it: only a well-formed origin is shown."""
    return value if isinstance(value, str) and ORIGIN.fullmatch(value) else None


def listing(data, titles: dict | None = None) -> dict:
    """`publish.list` as the page's shape. `configured` false means no server is set up yet."""
    data = data if isinstance(data, dict) else {}
    owner = data.get("owner")
    workspace = data.get("workspace")
    return {
        "available": True,
        "configured": data.get("configured") is True,
        "server": origin_of(data.get("server")),
        "share": origin_of(data.get("share")),
        "workspace": workspace if isinstance(workspace, str) and WORKSPACE.fullmatch(workspace) else None,
        "owner": owner if isinstance(owner, str) and PERSON.fullmatch(owner) else None,
        "live": data.get("live") is True,
        "shares": [r for r in (share_row(s, titles) for s in (data.get("shares") or [])[:500]) if r],
        "staged": [r for r in (staged_row(s) for s in (data.get("staged") or [])[:50]) if r],
    }


def sent(data) -> dict:
    """`publish.send`'s answer for the one response that may hold a link. A host that sends no link is a failure the
    person is told about (the files may already be shared: they revoke), never an empty success."""
    if not isinstance(data, dict):
        raise fail("internal", exit=1)
    link = data.get("link")
    share_id = data.get("share_id")
    if not isinstance(link, str) or not 40 <= len(link) <= 1200 or not isinstance(share_id, str):
        raise fail(
            "publish.no_link", "The files were shared but no link came back. Revoke this share and share again.", exit=1
        )
    return {
        "share_id": share_id if re.fullmatch(r"[0-9a-f]{32}", share_id) else None,
        "ticket": display(data.get("ticket")),
        "kind": data.get("kind") if data.get("kind") in KINDS else "unknown",
        "link": link,
        "expires_at": _time(data.get("expires_at")),
        "view_limit": _int(data.get("view_limit"), 0, 1000),
    }


def register(app: FastAPI, gateway: Gateway) -> None:
    def supported(link) -> bool:
        return all(link.supports(op, "publish/1") for op in OPS)

    def titles(ws, wanted):
        from .adapters import board_statuses

        if not wanted:
            return {}
        try:
            rows, _ = reads.all_rows(gateway, ws, {"status": board_statuses(), "sort": "updated"}, cap=400)
        except HostError:
            return {}
        return {r["key"]: r["title"] for r in rows if r.get("key") in wanted and isinstance(r.get("title"), str)}

    @app.get("/api/w/{ws}/shares")
    def shares(query: Annotated[ListQuery, Query()], ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not supported(link):
            return {"available": False}
        args = {"live": True, **({"ref": query.ticket} if query.ticket else {})}
        data = link.call("publish.list", args)
        got = listing(data)
        names = titles(ws, {r["ticket"] for r in got["shares"] if r["ticket"]}) if not query.ticket else {}
        if names:
            for row in got["shares"]:
                row["title"] = display(names.get(row["ticket"]))
        return got

    @app.post("/api/w/{ws}/shares/upload")
    def upload(body: UploadBody, ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        data = link.call("publish.stage_upload", {"name": body.name, "b64": body.data}, write=True, idem=body.idem)
        row = upload_row(data)
        if row is None:
            raise fail("internal", exit=1)
        return row

    @app.post("/api/w/{ws}/shares/prepare")
    def prepare(body: PrepareBody, ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        data = link.call("publish.prepare", body.host_args(), write=True, idem=body.idem)
        row = staged_row({**data, "created_at": None}) if isinstance(data, dict) else None
        if row is None:
            raise fail("internal", exit=1)
        return {
            "staged": row,
            "server": origin_of(data.get("server")),
            "share": origin_of(data.get("share")),
        }

    @app.post("/api/w/{ws}/shares/send")
    def send(body: StagedBody, ws: str = Depends(reads.workspace)):
        # The host checks that a person's signature over exactly this staged copy is in the ticket's log. This
        # process adds nothing to that and keeps nothing of the answer.
        data = gateway.enabled(ws).call("publish.send", {"staged_id": body.staged_id}, write=True, idem=body.idem)
        return sent(data)

    @app.post("/api/w/{ws}/shares/revoke")
    def revoke(body: RevokeBody, ws: str = Depends(reads.workspace)):
        data = gateway.enabled(ws).call("publish.revoke", {"share_id": body.share_id}, write=True, idem=body.idem)
        ok = isinstance(data, dict) and data.get("state") == "revoked"
        return {"revoked": ok}

    @app.post("/api/w/{ws}/shares/discard")
    def discard(body: StagedBody, ws: str = Depends(reads.workspace)):
        data = gateway.enabled(ws).call("publish.discard", {"staged_id": body.staged_id}, write=True, idem=body.idem)
        return {"discarded": isinstance(data, dict) and data.get("discarded") is True}

    @app.post("/api/w/{ws}/shares/register")
    def register_server(body: RegisterBody, request: Request, ws: str = Depends(reads.workspace)):
        # Registering sends the workspace's delegation to the signed server: a full LOCAL session only (the host
        # checks the owner role, agents, the rate of three a minute and takes no address from this request).
        token = request.state.token
        if not (full_session(app.state.auth, token) and app.state.auth.login_kind(token) == "local"):
            raise refused("Sign in on this computer to connect this workspace to the publish server.")
        data = gateway.enabled(ws).call("publish.register", {}, write=True, idem=body.idem)
        result = data.get("result") if isinstance(data, dict) else None
        return {"registered": True, "result": result if result in ("registered", "unchanged") else "registered"}

"""The Add-ons page (V3 A10): thin, typed proxies to the marketplace host ops (design-g3 4.2).

The host is the authority for who may do what; this module only fixes the op, closes the arguments and adds nothing:

| route                                  | host op            | host column                                       |
|----------------------------------------|--------------------|---------------------------------------------------|
| GET    /market                         | market.list        | P and G, read                                     |
| GET    /market/installed               | market.installed   | P and G, read                                     |
| POST   /market/stage                   | market.stage       | P, owner, human only (agent: market.actor_refused) |
| DELETE /market/stage/{stage}           | market.unstage     | P, owner, human only                              |
| POST   /market/uninstall               | market.uninstall   | P, owner, human only                              |
| POST   /market/add                     | market.add         | P, owner, human only; the host decides the class |
| POST   /market/refresh                 | market.refresh     | P and G; no `commit` argument (human only) |

`market.add` is the picker's Add (a source the host offers; the policy gate refuses every other address).

Nothing here signs. Installing is the signature of `addon.grant {name, staged}` (`MarketGrant` in handoff.py): one
click where click-to-sign is on (V3-D38), the terminal hand-off where it is off. All text the host returns about a
marketplace or an add-on is untrusted data: this module passes it on unchanged and the page shows it inside
`AddonFrame` only.

Writes need an idem at the host link; the browser sends none, so each request draws a fresh one (a retry of a stage is a
new stage, an uninstall or a refresh is safe to repeat).
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import reads
from .errors import fail
from .gateway import Gateway
from .signing import MarketName, StageId

Url = Annotated[str, StringConstraints(pattern=r"^[\x21-\x7e]{1,200}$")]
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_idem() -> str:
    """`a_` + one char [0-7] + 25 Crockford chars (the shape every dashboard idem has)."""
    return "a_" + secrets.choice("01234567") + "".join(secrets.choice(_CROCKFORD) for _ in range(25))


class StageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: MarketName
    url: Url | None = None
    update: bool | None = None


class UninstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: MarketName


class AddBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: Url


class RefreshBody(BaseModel):
    """No `commit`: pinning a commit is human only at the host, and the page never needs it."""

    model_config = ConfigDict(extra="forbid", strict=True)
    url: Url | None = None


def register(app: FastAPI, gateway: Gateway) -> None:
    def read(ws: str, op: str, args: dict):
        return gateway.enabled(ws).call(op, args)

    def write(ws: str, op: str, args: dict):
        return gateway.enabled(ws).call(op, args, write=True, idem=new_idem())

    @app.get("/api/w/{ws}/market")
    def market(ws: str = Depends(reads.workspace)):
        return read(ws, "market.list", {"entries": True})

    @app.get("/api/w/{ws}/market/installed")
    def installed(ws: str = Depends(reads.workspace)):
        return read(ws, "market.installed", {})

    @app.post("/api/w/{ws}/market/stage")
    def stage(body: StageBody, ws: str = Depends(reads.workspace)):
        return write(ws, "market.stage", body.model_dump(exclude_none=True))

    @app.delete("/api/w/{ws}/market/stage/{stage_id}")
    def unstage(stage_id: StageId, ws: str = Depends(reads.workspace)):
        return write(ws, "market.unstage", {"stage": stage_id})

    @app.post("/api/w/{ws}/market/uninstall")
    def uninstall(body: UninstallBody, ws: str = Depends(reads.workspace)):
        return write(ws, "market.uninstall", {"name": body.name})

    @app.post("/api/w/{ws}/market/add")
    def add(body: AddBody, request: Request, ws: str = Depends(reads.workspace)):
        if app.state.auth.login_kind(request.state.token) == "switch":
            raise fail("session.readonly", "Open a full session to add a marketplace.", exit=3)
        return write(ws, "market.add", {"url": body.url})

    @app.post("/api/w/{ws}/market/refresh")
    def refresh(body: RefreshBody, ws: str = Depends(reads.workspace)):
        return write(ws, "market.refresh", body.model_dump(exclude_none=True))

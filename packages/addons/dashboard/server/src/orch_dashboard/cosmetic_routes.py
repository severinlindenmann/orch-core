"""The workspace name and icon (run 13, D6): unsigned, owner only, one click.

| Method | Path                           | Host op                | Notes                              |
|--------|--------------------------------|------------------------|------------------------------------|
| PUT    | /api/w/{ws}/workspace/cosmetic | workspace.cosmetic.set | `if_current` is the compare-and-set |

A switch session never reaches it (non-GET routes are default-refused for it, `security.SWITCH_POST`). The host
checks that the caller is a person and the workspace owner, the closed icon lists and the plain name.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import reads
from .gateway import Gateway
from .market_routes import new_idem

Name = Annotated[str, StringConstraints(min_length=1, max_length=80)]


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    icon: str
    colour: str


class Current(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    icon: Pair | None


class CosmeticBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Name | None = None
    icon: Pair | None = None
    if_current: Current


def register(app: FastAPI, gateway: Gateway) -> None:
    @app.put("/api/w/{ws}/workspace/cosmetic")
    def cosmetic(body: CosmeticBody, ws: str = Depends(reads.workspace)):
        args = {"if_current": body.if_current.model_dump()}
        sent = body.model_fields_set
        if "name" in sent and body.name is not None:
            args["name"] = body.name
        if "icon" in sent:
            args["icon"] = body.icon.model_dump() if body.icon else None
        return gateway.enabled(ws).call("workspace.cosmetic.set", args, write=True, idem=new_idem())

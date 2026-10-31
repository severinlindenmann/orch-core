"""Ticket work folders (design r3 part C, host ops ``workdir.get`` / ``workdir.delete``).

A ticket without a codebase keeps its working files in a folder next to the workspace store. The page shows how much is
there (never the full path: the folder is named relative to the project) and offers "Delete work folder", a person's
own action behind a confirmation. Typed routes with closed bodies, like the worktrees: the host decides who may and
what is deleted; the browser draws one idem per click and keeps it for every retry. Host text is display-only.
"""

from __future__ import annotations

import re

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict

from . import reads
from .adapters.addons import display
from .gateway import Gateway
from .terminals import Idem

#: the file-name alphabet of a share (as shares.FileName); a work-folder name outside it is not offered
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")


class DeleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket: reads.Key
    idem: Idem


def view(data: object) -> dict:
    """The page's work-folder card from the host's answer; anything unexpected reads as "no folder"."""
    d = data if isinstance(data, dict) else {}
    if d.get("codebase") is True or d.get("exists") is not True:
        return {"exists": False, "codebase": d.get("codebase") is True}
    files, size = d.get("files"), d.get("bytes")
    return {
        "exists": True,
        "codebase": False,
        "folder": display(d.get("folder")),
        "files": files if type(files) is int and files >= 0 else None,
        "bytes": size if type(size) is int and size >= 0 else None,
        "truncated": d.get("truncated") is True,
        "items": items(d.get("items")),
    }


def items(raw: object) -> list[dict]:
    """The files a share could take (P03): plain names and sizes, at most 20; anything else from the host is dropped."""
    out = []
    for e in raw if isinstance(raw, list) else ():
        name, size = (e.get("name"), e.get("bytes")) if isinstance(e, dict) else (None, None)
        if isinstance(name, str) and FILE_NAME.fullmatch(name) and type(size) is int and 0 <= size <= 10**12:
            out.append({"name": name, "bytes": size})
    return out[:20]


def register(app: FastAPI, gateway: Gateway) -> None:
    @app.get("/api/w/{ws}/tickets/{key}/workdir")
    def get(key: reads.Key, ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("workdir.get", "workdir/1"):
            return {"available": False, "exists": False, "codebase": False}
        return {"available": True, **view(link.call("workdir.get", {"ticket": key, "list": True}))}

    @app.post("/api/w/{ws}/workdir/delete")
    def delete(body: DeleteBody, ws: str = Depends(reads.workspace)):
        data = gateway.enabled(ws).call("workdir.delete", {"ticket": body.ticket}, write=True, idem=body.idem)
        return {"deleted": isinstance(data, dict) and data.get("deleted") is True}

"""Focus mode routes (focus/1, phase 2). All focus routes live here.

``GET /api/w/{ws}/runs/{run}/transcript`` -> the host op ``run.transcript``: a run's harness transcript as normalized
chat items, cursor based (``after`` is the ``next`` of the previous answer, so a poll returns only new items). The
host checks that the run is the caller's own. Items are projected to a closed set of fields; the browser draws all text
as plain text.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import reads
from .gateway import Gateway
from .terminals import RunId

Cursor = Annotated[str, StringConstraints(pattern=r"^o:[0-9]{1,12}$")]
Child = Annotated[str, StringConstraints(pattern=r"^agent-[A-Za-z0-9_-]{1,64}$")]
ITEM_STR = ("id", "role", "kind", "text", "ts")
ROLES = ("user", "assistant", "tool", "system")
KINDS = ("text", "tool", "result", "question", "status")


class TranscriptQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    after: Cursor | None = None
    limit: int | None = Field(default=None, ge=1, le=200)
    child: Child | None = None


def item(raw: object) -> dict | None:
    """One chat item with only the fields the page draws; anything of another shape is dropped."""
    if not isinstance(raw, dict) or raw.get("role") not in ROLES or raw.get("kind") not in KINDS:
        return None
    out = {k: raw.get(k) if isinstance(raw.get(k), str) else None for k in ITEM_STR}
    if raw.get("truncated") is True:
        out["truncated"] = True
    tool = raw.get("tool")
    if isinstance(tool, dict):
        out["tool"] = {k: tool.get(k) if isinstance(tool.get(k), str) else None for k in ("name", "call")}
    files = raw.get("files")
    if isinstance(files, list):
        out["files"] = [f for f in files if isinstance(f, str)][:50]
    options = raw.get("options")
    if isinstance(options, list):
        out["options"] = [o for o in options if isinstance(o, str)][:10]
    if raw.get("kind") == "result":
        out["error"] = raw.get("error") is True
    return out


def project(run: str, data: object) -> dict:
    data = data if isinstance(data, dict) else {}
    items = [i for i in map(item, data.get("items") if isinstance(data.get("items"), list) else []) if i]
    nxt = data.get("next")
    reason = data.get("reason")
    return {
        "run": run,
        "harness": data.get("harness") if data.get("harness") in ("claude-code", "codex") else None,
        "available": data.get("available") is True,
        "reason": reason[:200] if isinstance(reason, str) else None,
        "items": items,
        "next": nxt if isinstance(nxt, str) and nxt.startswith("o:") else None,
        "eof": data.get("eof") is not False,
    }


def register(app: FastAPI, gateway: Gateway) -> None:
    @app.get("/api/w/{ws}/runs/{run}/transcript")
    def transcript(run: RunId, query: Annotated[TranscriptQuery, Query()], ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("run.transcript", "focus/1"):
            return project(run, {"available": False, "reason": "this host has no transcript support"})
        args = {"run": run, **{k: v for k, v in query.model_dump().items() if v is not None}}
        return project(run, link.call("run.transcript", args))

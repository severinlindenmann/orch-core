"""Focus mode routes (focus/1, phase 2). All focus routes live here.

``GET /api/w/{ws}/runs/{run}/transcript`` -> the host op ``run.transcript``: a run's harness transcript as normalized
chat items, cursor based (``after`` is the ``next`` of the previous answer, so a poll returns only new items). The
host checks that the run is the caller's own. Items are projected to a closed set of fields; the browser draws all text
as plain text.

``GET /api/w/{ws}/tickets/{key}/worktree-diff?repo=`` -> the host op ``worktree.diff`` (P2-5): the uncommitted changes
of the ticket's worktree (files plus unified text), projected to closed fields; unavailability is data.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import reads
from .gateway import Gateway
from .terminals import RunId

RepoName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
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


def _s(v: object, n: int = 200) -> str | None:
    return v[:n] if isinstance(v, str) else None


def _dfiles(v: object) -> list[str]:
    return [f[:300] for f in v if isinstance(f, str)][:100] if isinstance(v, list) else []


def child(raw: object) -> dict | None:
    """One delegation row with only the fields the page draws."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        return None
    tr = raw.get("transcript") if isinstance(raw.get("transcript"), dict) else {}
    depth = raw.get("depth")
    return {
        "id": raw["id"][:100], "parent_run": _s(raw.get("parent_run")), "title": _s(raw.get("title")) or "Task",
        "spawned_by": raw.get("spawned_by") if raw.get("spawned_by") in ("agent", "human", "promoted") else "agent",
        "harness": raw.get("harness") if raw.get("harness") in ("claude-code", "codex", "shell") else None,
        "model": _s(raw.get("model"), 100), "depth": depth if isinstance(depth, int) and 0 < depth < 10 else 1,
        "state": raw.get("state") if raw.get("state") in ("running", "done", "failed") else "running",
        "started_at": _s(raw.get("started_at"), 40), "ended_at": _s(raw.get("ended_at"), 40),
        "run": _s(raw.get("run"), 40),
        "transcript": {"run": _s(tr.get("run"), 40), "child": _s(tr.get("child"), 100)},
        "files": _dfiles(raw.get("files")),
    }  # fmt: skip


def delegations(ticket: str, data: object) -> dict:
    data = data if isinstance(data, dict) else {}
    sessions = []
    for s in data.get("sessions") if isinstance(data.get("sessions"), list) else []:
        if not isinstance(s, dict) or not isinstance(s.get("run"), str):
            continue
        vis = s.get("visibility") if isinstance(s.get("visibility"), dict) else {}
        pol = s.get("policy") if isinstance(s.get("policy"), dict) else None
        sessions.append({
            "run": s["run"][:40], "session": _s(s.get("session"), 100), "model": _s(s.get("model"), 100),
            "harness": s.get("harness") if s.get("harness") in ("claude-code", "codex", "shell") else None,
            "state": _s(s.get("state"), 20), "policy": pol, "files": _dfiles(s.get("files")),
            "visibility": {"available": vis.get("available") is True, "reason": _s(vis.get("reason"))},
            "children": [c for c in map(child, s.get("children") if isinstance(s.get("children"), list) else []) if c],
        })  # fmt: skip
    return {"ticket": ticket, "sessions": sessions}


SourceId = Annotated[str, StringConstraints(pattern=r"^s_[0-9a-f]{10}$")]


class SourcesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    child: Child | None = None


def _result(v: object) -> dict | None:
    if not isinstance(v, dict) or not isinstance(v.get("url"), str):
        return None
    return {"title": _s(v.get("title")), "url": v["url"][:2000]}


def source(raw: object) -> dict | None:
    """One research source with only the fields the page draws (P2-4); text only, nothing is ever fetched."""
    kinds = ("web", "search", "file")
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or raw.get("kind") not in kinds:
        return None
    out: dict = {"id": raw["id"][:40], "kind": raw["kind"], "read_at": _s(raw.get("read_at"), 40)}
    if raw["kind"] == "web":
        out |= {"url": _s(raw.get("url"), 2000), "host": _s(raw.get("host"), 255), "title": _s(raw.get("title")),
                "has_extract": raw.get("has_extract") is True}  # fmt: skip
    elif raw["kind"] == "search":
        res = raw.get("results") if isinstance(raw.get("results"), list) else []
        out |= {"query": _s(raw.get("query"), 300), "results": [r for r in map(_result, res) if r][:20]}
    else:
        out["path"] = _s(raw.get("path"), 1000)
    return out


def sources(run: str, data: object) -> dict:
    data = data if isinstance(data, dict) else {}
    rows = data.get("sources") if isinstance(data.get("sources"), list) else []
    reason = data.get("reason")
    return {
        "run": run,
        "available": data.get("available") is True,
        "reason": reason[:200] if isinstance(reason, str) else None,
        "sources": [s for s in map(source, rows) if s][:200],
    }


def extract(source_id: str, data: object) -> dict:
    data = data if isinstance(data, dict) else {}
    text = data.get("extract")
    return {
        "id": source_id, "url": _s(data.get("url"), 2000),
        "extract": text[:65536] if isinstance(text, str) else "", "truncated": data.get("truncated") is True,
    }  # fmt: skip


class WorktreeDiffQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: RepoName | None = None


def _str(v: object, n: int) -> str | None:
    return v[:n] if isinstance(v, str) else None


def project_worktree_diff(key: str, data: object) -> dict:
    """``worktree.diff`` as the page draws it: closed fields, bounded strings; unavailability is data."""
    data = data if isinstance(data, dict) else {}
    files = []
    for f in data.get("files") if isinstance(data.get("files"), list) else []:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or f.get("status") not in ("M", "A", "D", "?"):
            continue
        ok = lambda k: f[k] if type(f.get(k)) is int and f[k] >= 0 else 0  # noqa: E731
        files.append({"path": f["path"][:4096], "status": f["status"], "added": ok("added"), "removed": ok("removed"),
                      "binary": f.get("binary") is True, "too_large": f.get("too_large") is True})  # fmt: skip
    return {
        "ticket": key,
        "repo": _str(data.get("repo"), 64),
        "available": data.get("available") is True,
        "reason": _str(data.get("reason"), 300),
        "path_label": _str(data.get("path_label"), 200),
        "branch": _str(data.get("branch"), 200),
        "head": _str(data.get("head"), 64),
        "checked_at": _str(data.get("checked_at"), 40),
        "truncated": data.get("truncated") is True,
        "files": files[:200],
        "unified": data["unified"][:1048576] if isinstance(data.get("unified"), str) else "",
    }


def register(app: FastAPI, gateway: Gateway) -> None:
    @app.get("/api/w/{ws}/tickets/{key}/delegations")
    def delegation_list(key: reads.Key, ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("delegation.list", "focus/1"):
            return {"ticket": key, "sessions": [], "supported": False}
        return delegations(key, link.call("delegation.list", {"ticket": key})) | {"supported": True}

    @app.get("/api/w/{ws}/runs/{run}/transcript")
    def transcript(run: RunId, query: Annotated[TranscriptQuery, Query()], ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("run.transcript", "focus/1"):
            return project(run, {"available": False, "reason": "this host has no transcript support"})
        args = {"run": run, **{k: v for k, v in query.model_dump().items() if v is not None}}
        return project(run, link.call("run.transcript", args))

    @app.get("/api/w/{ws}/runs/{run}/sources")
    def run_sources(run: RunId, query: Annotated[SourcesQuery, Query()], ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("run.sources", "focus/1"):
            return sources(run, {"available": False, "reason": "this host has no research support"})
        args = {"run": run, **({"child": query.child} if query.child else {})}
        return sources(run, link.call("run.sources", args))

    @app.get("/api/w/{ws}/runs/{run}/sources/{source_id}")
    def run_source(run: RunId, source_id: SourceId, query: Annotated[SourcesQuery, Query()],
                   ws: str = Depends(reads.workspace)):  # fmt: skip
        link = gateway.enabled(ws)
        if not link.supports("run.sources", "focus/1"):
            return extract(source_id, {})
        args = {"run": run, "source": source_id, **({"child": query.child} if query.child else {})}
        return extract(source_id, link.call("run.sources", args))

    @app.get("/api/w/{ws}/tickets/{key}/worktree-diff")
    def worktree_diff(key: reads.Key, query: Annotated[WorktreeDiffQuery, Query()], ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("worktree.diff", "worktree/1"):
            return project_worktree_diff(key, {"available": False, "reason": "this host cannot show the live diff"})
        args = {"ticket": key, **({"repo": query.repo} if query.repo else {})}
        return project_worktree_diff(key, link.call("worktree.diff", args))

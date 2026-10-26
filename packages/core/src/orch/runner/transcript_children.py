"""The runner's delegation reader (focus mode phase 2, P2-2): the children a run's harness started, from its files.

* **Claude Code** writes ``<uuid>/subagents/agent-<id>.jsonl`` + ``.meta.json`` (``agentType, description, toolUseId,
  spawnDepth, model, finished``) next to the parent transcript ``<uuid>.jsonl``; the parent's ``Agent`` tool result
  (``toolUseResult.agentId`` + ``status``) tells how a child ended. A child's id is its file stem ``agent-<id>``.
* **Codex** (spike 26 Oct 2026, codex-cli 0.163): the child rollout's ``session_meta.source`` is
  ``{"subagent": {"thread_spawn": {"parent_thread_id", "depth", "agent_path", "agent_nickname"}}}``, so a child is
  every rollout whose ``parent_thread_id`` is the parent rollout's ``session_meta.id``. Its id is ``agent-<thread id>``.

Both formats are undocumented harness internals: anything unexpected is skipped, never an error. The answer is
``{run, harness, available, reason?, files, children}``; a child is the row the dashboard draws
(the host adds ``parent_run``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import transcript as tr
from .supervise import SpawnError

__all__ = ["children", "codex_child_path", "serve"]

MAX_CHILDREN = 50
MAX_SCAN = 4 << 20  # bytes of one transcript read for its files and times
_FAILED = ("failed", "error", "errored", "cancelled", "canceled", "killed")


def _lines(path: Path, cap: int = MAX_SCAN) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as f:
            raw = f.read(cap)
    except OSError:
        return []
    out = []
    for line in raw.split(b"\n")[:-1] if len(raw) >= cap else raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _scan(recs: list[dict[str, Any]], harness: str, cwd: str | None, *, sidechain: bool) -> dict[str, Any]:
    """Files touched (union of the tool items' ``files``), first and last timestamp, and a Codex completion flag."""
    files: list[str] = []
    first = last = None
    done = False
    for n, rec in enumerate(recs):
        ts = rec.get("timestamp")
        if isinstance(ts, str):
            first = first or ts
            last = ts
        if harness == "codex":
            payload = rec.get("payload")
            done = done or (rec.get("type") == "event_msg" and isinstance(payload, dict)
                            and payload.get("type") == "task_complete")  # fmt: skip
            items = tr.normalize_codex(rec, cwd, key=str(n))
        else:
            items = tr.normalize_claude(rec, cwd, sidechain=sidechain)
        for item in items:
            files.extend(f for f in item.get("files") or [] if isinstance(f, str))
    return {"files": list(dict.fromkeys(files))[:100], "first": first, "last": last, "done": done}


def _claude_children(path: Path, cwd: str | None) -> list[dict[str, Any]]:
    sub = path.with_suffix("") / "subagents"
    metas = sorted(sub.glob("agent-*.meta.json"))[:MAX_CHILDREN] if sub.is_dir() else []
    if not metas:
        return []
    status: dict[str, str] = {}  # agentId -> the parent's toolUseResult status
    for rec in _lines(path):
        res = rec.get("toolUseResult")
        if isinstance(res, dict) and isinstance(res.get("agentId"), str) and isinstance(res.get("status"), str):
            status[res["agentId"]] = res["status"]
    out = []
    for meta_path in metas:
        stem = meta_path.name.removesuffix(".meta.json")
        if not tr.CHILD.fullmatch(stem):
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        agent_id = stem.removeprefix("agent-")
        got = _scan(_lines(sub / f"{stem}.jsonl"), "claude-code", cwd, sidechain=True)
        result = status.get(agent_id, "")
        if result.lower() in _FAILED:
            state = "failed"
        elif meta.get("finished") is True or result.lower() == "completed":
            state = "done"
        else:
            state = "running"
        title = meta.get("description") or meta.get("agentType") or "Task"
        depth = meta.get("spawnDepth")
        out.append({
            "id": f"cc:{stem}", "title": str(title)[:200], "spawned_by": "agent", "harness": "claude-code",
            "model": meta.get("model") if isinstance(meta.get("model"), str) else None,
            "depth": depth if isinstance(depth, int) and depth > 0 else 1, "state": state,
            "started_at": got["first"], "ended_at": got["last"] if state != "running" else None,
            "run": None, "transcript": {"child": stem}, "files": got["files"],
        })  # fmt: skip
    return out


def _meta(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            first = json.loads(f.readline(65536) or b"{}")
    except (OSError, ValueError):
        return {}
    payload = first.get("payload") if isinstance(first, dict) else None
    return payload if isinstance(payload, dict) and first.get("type") == "session_meta" else {}


def _codex_rollouts(env: Mapping[str, str], since: float) -> list[Path]:
    root = tr._home(env, "CODEX_HOME", ".codex") / "sessions"
    out = []
    for path in sorted(root.glob("*/*/*/rollout-*.jsonl"))[-2000:]:
        try:
            if path.stat().st_mtime >= since:
                out.append(path)
        except OSError:
            continue
    return out


def _spawn_of(meta: Mapping[str, Any]) -> Mapping[str, Any]:
    source = meta.get("source")
    sub = source.get("subagent") if isinstance(source, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    return spawn if isinstance(spawn, dict) else {}


def _codex_children(path: Path, cwd: str | None, env: Mapping[str, str]) -> list[dict[str, Any]]:
    parent = _meta(path).get("id")
    if not isinstance(parent, str):
        return []
    try:
        since = path.stat().st_mtime - 86400
    except OSError:
        return []
    out = []
    for cand in _codex_rollouts(env, since):
        meta = _meta(cand)
        spawn = _spawn_of(meta)
        if spawn.get("parent_thread_id") != parent or not isinstance(meta.get("id"), str):
            continue
        got = _scan(_lines(cand), "codex", cwd, sidechain=True)
        path_name = str(spawn.get("agent_path") or "").rsplit("/", 1)[-1]
        title = path_name or spawn.get("agent_nickname") or "Task"
        depth = spawn.get("depth")
        out.append({
            "id": f"cx:agent-{meta['id']}", "title": str(title)[:200], "spawned_by": "agent", "harness": "codex",
            "model": meta.get("model") if isinstance(meta.get("model"), str) else None,
            "depth": depth if isinstance(depth, int) and depth > 0 else 1,
            "state": "done" if got["done"] else "running", "started_at": got["first"],
            "ended_at": got["last"] if got["done"] else None, "run": None,
            "transcript": {"child": f"agent-{meta['id']}"}, "files": got["files"],
        })  # fmt: skip
        if len(out) >= MAX_CHILDREN:
            break
    return out


def codex_child_path(parent: Path, child: str, env: Mapping[str, str]) -> Path | None:
    """The rollout of Codex child ``agent-<thread id>`` of the parent rollout ``parent``, or ``None``."""
    thread = child.removeprefix("agent-")
    pid = _meta(parent).get("id")
    if not (isinstance(pid, str) and pid):
        return None
    try:
        since = parent.stat().st_mtime - 86400
    except OSError:
        return None
    for cand in _codex_rollouts(env, since):
        meta = _meta(cand)
        spawn = _spawn_of(meta)
        if meta.get("id") == thread and spawn and spawn.get("parent_thread_id") == pid:
            return cand
    return None


def children(
    path: Path, harness: str, cwd: str | None = None, env: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """The delegated children of the transcript ``path`` (rows without ``parent_run``; the host adds it)."""
    if harness == "claude-code":
        return _claude_children(path, cwd)
    if harness == "codex":
        return _codex_children(path, cwd, env or {})
    return []


def serve(args: Mapping[str, Any], sup: Any, env: Mapping[str, str]) -> dict[str, Any]:
    """The runner command ``transcript {run, children: true}``."""
    run = str(args.get("run"))
    rec = sup.table.get(run)
    if rec is None:
        raise SpawnError("not_found", "no such run")
    harness = str(rec.get("harness") or "")
    out: dict[str, Any] = {"run": run, "harness": harness}
    path = tr.locate(run, harness, rec, env)
    if path is None or not path.is_file():
        return out | {"available": True, "reason": "transcript not found yet", "files": [], "children": []}
    cwd = tr._cwd_of(rec)
    own = _scan(_lines(path), harness, cwd, sidechain=False)
    return out | {"available": True, "files": own["files"], "children": children(path, harness, cwd, env)}

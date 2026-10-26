"""The runner's transcript reader (focus mode phase 2, P2-1): a run's harness transcript as normalized chat items.

The transcripts live in the HOME of the account the runner runs agents as, so the runner reads them (the host may be
another UID). Binding run -> file:

* **Claude Code** is launched with ``--session-id <uuid>``, ``uuid = uuid5(ORCH_NS, run)`` (the host argv carries the
  placeholder :data:`PLACEHOLDER`, :func:`prepare` swaps it in): the file is
  ``${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/*/<uuid>.jsonl``. A child (sub-agent) is
  ``<uuid>/subagents/agent-<id>.jsonl`` next to it.
* **Codex** has no such flag: the first rollout under ``${CODEX_HOME:-$HOME/.codex}/sessions`` whose
  ``session_meta.cwd`` equals the run's cwd and whose time is not before the run's start (minus 5 s).
  The pick is cached per run.

The reader is cursor based: ``after`` is ``o:<byte offset>``; a read returns at most 200 items from at most 512 KiB, and
stops before a partial last line. Unknown record types are skipped (the formats are not ours and change).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .supervise import SpawnError

__all__ = [
    "FLAG", "ORCH_NS", "PLACEHOLDER", "locate", "normalize_claude", "normalize_codex", "prepare", "read", "serve",
    "session_id",
]  # fmt: skip

ORCH_NS = uuid.UUID("6f0c1c1e-5b0a-4c53-9a43-0f0c0a7e0f21")
FLAG = "--session-id"
PLACEHOLDER = "@orch-session-id"
MAX_ITEMS = 200
MAX_READ = 512 << 10
MAX_TEXT = 4096
CHILD = re.compile(r"agent-[A-Za-z0-9_-]{1,64}")
CURSOR = re.compile(r"o:([0-9]{1,12})")
_OPENERS = ("<command-", "<local-command", "<system-reminder>")
_CODEX_NOISE = ("<environment_context>", "<user_instructions>", "# AGENTS.md")
_FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)
_SUMMARY_KEYS = ("file_path", "command", "pattern", "url", "query", "description", "path", "notebook_path")
#: run -> rollout path (Codex), so the scan runs once per run
_CODEX_CACHE: dict[str, Path] = {}


def session_id(run: str) -> str:
    return str(uuid.uuid5(ORCH_NS, run))


def prepare(run: str, argv: list[str]) -> list[str]:
    """``argv`` with the placeholder replaced by the run's session uuid; it must follow :data:`FLAG` and occur once."""
    if PLACEHOLDER not in argv:
        return argv
    at = argv.index(PLACEHOLDER)
    if argv.count(PLACEHOLDER) != 1 or at == 0 or argv[at - 1] != FLAG:
        raise SpawnError("invalid.input", "spawn: the session placeholder is misplaced")
    return [*argv[:at], session_id(run), *argv[at + 1 :]]


# -- locating ----------------------------------------------------------------------------------------------------------


def _home(env: Mapping[str, str], var: str, default: str) -> Path:
    return Path(env.get(var) or os.path.join(env.get("HOME") or os.path.expanduser("~"), default))


def _ts(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def locate(run: str, harness: str, rec: Mapping[str, Any], env: Mapping[str, str]) -> Path | None:
    """The transcript file of ``run`` (``rec`` is the runner table row), or ``None`` while there is none."""
    if harness == "claude-code":
        base = _home(env, "CLAUDE_CONFIG_DIR", ".claude") / "projects"
        for hit in sorted(base.glob(f"*/{session_id(run)}.jsonl")):
            return hit
        return None
    if harness != "codex":
        return None
    have = _CODEX_CACHE.get(run)
    if have is not None and have.is_file():
        return have
    cwd = rec.get("cwd_path")
    started = _ts(rec.get("created_at"))
    if not cwd or started is None:
        return None
    root = _home(env, "CODEX_HOME", ".codex") / "sessions"
    for path in sorted(root.glob("*/*/*/rollout-*.jsonl")):
        try:
            if path.stat().st_mtime < started - 5:
                continue
            with path.open("rb") as f:
                first = json.loads(f.readline(65536) or b"{}")
        except (OSError, ValueError):
            continue
        meta = first.get("payload") if isinstance(first, dict) else None
        if not isinstance(meta, dict) or first.get("type") != "session_meta" or meta.get("cwd") != cwd:
            continue
        at = _ts(meta.get("timestamp")) or _ts(first.get("timestamp"))
        if at is not None and at >= started - 5:
            _CODEX_CACHE[run] = path
            return path
    return None


# -- normalizing -------------------------------------------------------------------------------------------------------


def _clip(text: str) -> tuple[str, bool]:
    raw = text.encode()
    if len(raw) <= MAX_TEXT:
        return text, False
    return raw[:MAX_TEXT].decode(errors="ignore"), True


def _item(id_: str, role: str, kind: str, text: str, ts: Any, **extra: Any) -> dict[str, Any]:
    clipped, cut = _clip(text)
    out: dict[str, Any] = {"id": id_, "role": role, "kind": kind, "text": clipped, "ts": ts}
    if cut:
        out["truncated"] = True
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _rel(path: Any, cwd: str | None) -> str | None:
    if not isinstance(path, str) or not path or not cwd:
        return None
    rel = os.path.relpath(path, cwd) if os.path.isabs(path) else os.path.normpath(path)
    return None if rel == ".." or rel.startswith("../") or os.path.isabs(rel) else rel


def _files(paths: list[Any], cwd: str | None) -> list[str]:
    out = [r for r in (_rel(p, cwd) for p in paths) if r]
    return list(dict.fromkeys(out))


def _summary(name: str, inp: Any) -> str:
    if isinstance(inp, dict):
        for key in _SUMMARY_KEYS:
            if isinstance(inp.get(key), str) and inp[key]:
                return f"{name} {inp[key]}".strip()
    return name


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _human(text: str, skip: tuple[str, ...]) -> bool:
    return bool(text.strip()) and not text.lstrip().startswith(skip)


def normalize_claude(rec: Any, cwd: str | None = None, *, sidechain: bool = False) -> list[dict[str, Any]]:
    """The chat items of one Claude Code transcript record (none for a type that is skipped)."""
    if not isinstance(rec, dict) or (rec.get("isSidechain") and not sidechain):
        return []
    kind, ts, base = rec.get("type"), rec.get("timestamp"), str(rec.get("uuid") or "")
    cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else cwd
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    if kind == "system":
        text = "turn complete" if rec.get("subtype") == "turn_duration" else rec.get("content") or rec.get("subtype")
        return [_item(base, "system", "status", text, ts)] if isinstance(text, str) and text else []
    if kind not in ("user", "assistant") or rec.get("isMeta"):
        return []
    content = msg.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        return []
    role = "user" if kind == "user" else "assistant"
    out: list[dict[str, Any]] = []
    for n, b in enumerate(blocks):
        if not isinstance(b, dict):
            continue
        bid = base if len(blocks) == 1 else f"{base}:{n}"
        bt = b.get("type")
        if bt == "text" and isinstance(b.get("text"), str) and _human(b["text"], _OPENERS):
            out.append(_item(bid, role, "text", b["text"], ts))
        elif bt == "tool_use" and kind == "assistant":
            name, inp = str(b.get("name") or "tool"), b.get("input")
            call = str(b.get("id") or "")
            if name == "AskUserQuestion" and isinstance(inp, dict) and isinstance(inp.get("questions"), list):
                q = next((x for x in inp["questions"] if isinstance(x, dict)), {})
                opts = [o.get("label") if isinstance(o, dict) else o for o in q.get("options") or []]
                out.append(_item(bid, role, "question", str(q.get("question") or ""), ts,
                                 options=[o for o in opts if isinstance(o, str)]))  # fmt: skip
                continue
            paths = (
                [inp.get("file_path") or inp.get("notebook_path")]
                if name in _FILE_TOOLS and isinstance(inp, dict)
                else []
            )
            files = _files(paths, cwd)
            out.append(_item(bid, role, "tool", _summary(name, inp), ts, tool={"name": name, "call": call},
                             files=files or None))  # fmt: skip
        elif bt == "tool_result" and kind == "user":
            call = str(b.get("tool_use_id") or bid)
            out.append(_item(f"{call}:r", "tool", "result", _result_text(b.get("content")), ts,
                             error=bool(b.get("is_error"))))  # fmt: skip
    return out


def normalize_codex(rec: Any, cwd: str | None = None, *, key: str = "") -> list[dict[str, Any]]:
    """The chat items of one Codex rollout line; ``key`` makes the ids of items without an id of their own."""
    if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict):
        return []
    ts, p = rec.get("timestamp"), rec["payload"]
    pt = p.get("type")
    if rec.get("type") == "event_msg":
        return [_item(f"c:{key}", "system", "status", "turn complete", ts)] if pt == "task_complete" else []
    if rec.get("type") != "response_item":
        return []
    if pt == "message":
        role = p.get("role")
        if role not in ("user", "assistant"):
            return []
        parts = [b.get("text", "") for b in p.get("content") or [] if isinstance(b, dict)]
        text = "\n".join(t for t in parts if isinstance(t, str))
        return [_item(f"c:{key}", role, "text", text, ts)] if _human(text, _CODEX_NOISE) else []
    if pt in ("function_call", "custom_tool_call", "web_search_call"):
        name = "web_search" if pt == "web_search_call" else str(p.get("name") or "tool")
        raw = p.get("arguments") if pt == "function_call" else p.get("input")
        if pt == "web_search_call":
            raw = p.get("action")
        inp: Any = raw
        if isinstance(raw, str):
            with contextlib.suppress(ValueError):
                inp = json.loads(raw)
        text = raw if isinstance(raw, str) else json.dumps(raw or {})
        files = _files(_PATCH_FILE.findall(text if isinstance(text, str) else ""), cwd)
        if isinstance(inp, dict) and isinstance(inp.get("command"), list):
            line = " ".join(str(x) for x in inp["command"] if not str(x).startswith("*** "))
            summary = f"{name} {line}"
        else:
            summary = _summary(name, inp) if isinstance(inp, dict) else name
        return [_item(str(p.get("call_id") or f"c:{key}"), "assistant", "tool", summary, ts,
                      tool={"name": name, "call": str(p.get("call_id") or "")}, files=files or None)]  # fmt: skip
    if pt in ("function_call_output", "custom_tool_call_output"):
        out = p.get("output")
        if isinstance(out, str):
            with contextlib.suppress(ValueError):
                loaded = json.loads(out)
                if isinstance(loaded, dict) and isinstance(loaded.get("output"), str):
                    out = loaded["output"]
        return [_item(f"{p.get('call_id') or key}:r", "tool", "result", out if isinstance(out, str) else "", ts,
                      error=False)]  # fmt: skip
    return []


# -- reading -----------------------------------------------------------------------------------------------------------


def read(
    path: Path, harness: str, after: int, limit: int = MAX_ITEMS, *, cwd: str | None = None, sidechain: bool = False
) -> dict[str, Any]:
    """``{items, next, eof, skipped}`` from byte offset ``after``; ``next`` is the cursor to ask for new items with."""
    limit = max(1, min(int(limit), MAX_ITEMS))
    with path.open("rb") as f:
        size = os.fstat(f.fileno()).st_size
        if after > size:
            after = 0
        f.seek(after)
        chunk = f.read(MAX_READ)
    if after + len(chunk) < size and b"\n" not in chunk:
        chunk += b"\n"  # one line longer than a read: skip it whole instead of stalling
    items: list[dict[str, Any]] = []
    skipped = 0
    pos = 0
    while pos < len(chunk) and len(items) < limit:
        end = chunk.find(b"\n", pos)
        if end < 0:
            break  # a partial last line: the next read starts here
        line = chunk[pos:end]
        at = after + pos
        pos = end + 1
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        got = (
            normalize_codex(rec, cwd, key=str(at))
            if harness == "codex"
            else normalize_claude(rec, cwd, sidechain=sidechain)
        )
        if not got:
            skipped += 1
        items.extend(got)
    nxt = after + pos
    return {"items": items, "next": f"o:{nxt}", "eof": nxt >= size, "skipped": skipped}


def _cwd_of(rec: Mapping[str, Any]) -> str | None:
    return rec.get("cwd_path") if isinstance(rec.get("cwd_path"), str) else None


def serve(args: Mapping[str, Any], sup: Any, env: Mapping[str, str]) -> dict[str, Any]:
    """The runner command ``transcript {run, after?, limit?, child?}``."""
    run = str(args.get("run"))
    rec = sup.table.get(run)
    if rec is None:
        raise SpawnError("not_found", "no such run")
    harness = str(rec.get("harness") or "")
    out: dict[str, Any] = {"run": run, "harness": harness}
    path = locate(run, harness, rec, env)
    child = args.get("child")
    if child is not None:
        if not (isinstance(child, str) and CHILD.fullmatch(child)) or harness != "claude-code":
            raise SpawnError("invalid.input", "transcript: invalid child")
        path = None if path is None else path.with_suffix("") / "subagents" / f"{child}.jsonl"
    if path is None or not path.is_file():
        return out | {"available": False, "reason": "transcript not found yet", "items": [], "next": None, "eof": True}
    cursor = CURSOR.fullmatch(str(args.get("after") or "o:0"))
    if cursor is None:
        raise SpawnError("invalid.input", "transcript: invalid cursor")
    got = read(path, harness, int(cursor.group(1)), int(args.get("limit") or MAX_ITEMS), cwd=_cwd_of(rec),
               sidechain=child is not None)  # fmt: skip
    return out | {"available": True, **got}

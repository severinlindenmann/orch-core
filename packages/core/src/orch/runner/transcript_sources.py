"""The runner's research reader (focus mode phase 2, P2-4): what a run read, from its harness transcript.

Sources (text only: nothing here, nor in the host or the browser, ever fetches a URL):

* **Claude Code**: ``WebFetch`` (input ``url``; the tool result is what the agent got, a model summary of the page),
  ``WebSearch`` (input ``query``; result titles + urls) and ``Read`` of a file inside the run's cwd (kind ``file``).
* **Codex**: ``web_search_call`` (``action.query``, and ``action.url`` when the call opened a page); no extract.

Dedupe: by url (``web``), query (``search``) or path (``file``); the newest ``read_at`` wins. The id of a source is a
short hash of its key, so it is stable across reads. The formats are the harness's, not ours: anything unexpected is
skipped. ``serve`` answers ``transcript {run, sources: true, child?, source?}``: the list, or one source's extract
(64 KiB cap).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import transcript as tr
from .supervise import SpawnError

__all__ = ["extract", "serve", "sources"]

MAX_SCAN = 8 << 20
MAX_EXTRACT = 64 << 10
MAX_SOURCES = 200
MAX_RESULTS = 20
SOURCE_ID = re.compile(r"s_[0-9a-f]{10}")
_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def _id(kind: str, key: str) -> str:
    return "s_" + hashlib.sha1(f"{kind}\0{key}".encode()).hexdigest()[:10]  # noqa: S324 (an id, not a secret)


def _host(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host[4:] if host and host.startswith("www.") else host


def _text(content: Any) -> str:
    return tr._result_text(content)


def _results(rec: dict[str, Any], text: str) -> list[dict[str, str | None]]:
    """The hits of a ``WebSearch`` result: the structured ``toolUseResult`` if it has them, else urls in the text."""
    out: list[dict[str, str | None]] = []
    tur = rec.get("toolUseResult")
    hits = tur.get("results") if isinstance(tur, dict) and isinstance(tur.get("results"), list) else []
    for h in hits:
        for e in h.get("content") if isinstance(h, dict) and isinstance(h.get("content"), list) else [h]:
            if isinstance(e, dict) and isinstance(e.get("url"), str):
                title = e.get("title")
                out.append({"title": title[:200] if isinstance(title, str) else None, "url": e["url"][:2000]})
    if not out:
        out = [{"title": None, "url": u[:2000]} for u in dict.fromkeys(_URL.findall(text))]
    return out[:MAX_RESULTS]


def _lines(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as f:
            raw = f.read(MAX_SCAN)
    except OSError:
        return []
    lines = raw.split(b"\n")
    if len(raw) >= MAX_SCAN:
        lines = lines[:-1]  # the cut line
    out = []
    for line in lines:
        try:
            rec = json.loads(line) if line.strip() else None
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _claude(recs: list[dict[str, Any]], cwd: str | None, sidechain: bool) -> list[dict[str, Any]]:
    pending: dict[str, tuple[str, dict[str, Any], str | None]] = {}
    found: list[dict[str, Any]] = []
    for rec in recs:
        if rec.get("isSidechain") and not sidechain:
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            continue
        ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and isinstance(b.get("input"), dict) and isinstance(b.get("id"), str):
                pending[b["id"]] = (str(b.get("name")), b["input"], ts)
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                name, inp, at = pending.pop(b["tool_use_id"])
                text = _text(b.get("content"))
                failed = bool(b.get("is_error"))
                if name == "WebFetch" and isinstance(inp.get("url"), str) and not failed:
                    url = inp["url"][:2000]
                    found.append({"kind": "web", "key": url, "url": url, "host": _host(url), "title": None,
                                  "read_at": at or ts, "extract": text})  # fmt: skip
                elif name == "WebSearch" and isinstance(inp.get("query"), str) and not failed:
                    found.append({"kind": "search", "key": inp["query"], "query": inp["query"][:300],
                                  "read_at": at or ts, "results": _results(rec, text)})  # fmt: skip
                elif name == "Read" and not failed:
                    rel = tr._rel(inp.get("file_path"), cwd)
                    if rel:
                        found.append({"kind": "file", "key": rel, "path": rel[:1000], "read_at": at or ts})
    return found


def _codex(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for rec in recs:
        p = rec.get("payload")
        if rec.get("type") != "response_item" or not isinstance(p, dict) or p.get("type") != "web_search_call":
            continue
        act = p.get("action") if isinstance(p.get("action"), dict) else {}
        ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
        if isinstance(act.get("url"), str) and act["url"]:
            url = act["url"][:2000]
            found.append({"kind": "web", "key": url, "url": url, "host": _host(url), "title": None, "read_at": ts,
                          "extract": ""})  # fmt: skip
        elif isinstance(act.get("query"), str) and act["query"]:
            found.append({"kind": "search", "key": act["query"], "query": act["query"][:300], "read_at": ts,
                          "results": []})  # fmt: skip
    return found


def _all(path: Path, harness: str, cwd: str | None, sidechain: bool) -> list[dict[str, Any]]:
    recs = _lines(path)
    raw = _codex(recs) if harness == "codex" else _claude(recs, cwd, sidechain)
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for s in raw:  # file order is time order: a later read of the same source wins
        best[(s["kind"], s["key"])] = s
    out = []
    for s in best.values():
        s["id"] = _id(s["kind"], s["key"])
        out.append(s)
    out.sort(key=lambda s: s.get("read_at") or "", reverse=True)
    return out[:MAX_SOURCES]


def sources(path: Path, harness: str, cwd: str | None = None, *, sidechain: bool = False) -> list[dict[str, Any]]:
    """The deduped sources of one transcript, newest first, in the shape the dashboard lists (no extract text)."""
    out = []
    for s in _all(path, harness, cwd, sidechain):
        row = {k: s.get(k) for k in ("id", "kind", "url", "host", "title", "query", "path", "read_at", "results")
               if k in s}  # fmt: skip
        if s["kind"] == "web":
            row["has_extract"] = bool(s.get("extract"))
        out.append(row)
    return out


def extract(
    path: Path, harness: str, source: str, cwd: str | None = None, *, sidechain: bool = False
) -> dict[str, Any]:
    """The text the agent got for one web source (``{id, url, extract, truncated}``)."""
    for s in _all(path, harness, cwd, sidechain):
        if s["id"] == source and s["kind"] == "web":
            raw = str(s.get("extract") or "").encode()
            return {"id": source, "url": s["url"], "extract": raw[:MAX_EXTRACT].decode(errors="ignore"),
                    "truncated": len(raw) > MAX_EXTRACT}  # fmt: skip
    raise SpawnError("not_found", "no such source")


def serve(args: Mapping[str, Any], sup: Any, env: Mapping[str, str]) -> dict[str, Any]:
    """The runner command ``transcript {run, sources: true, child?, source?}``."""
    run = str(args.get("run"))
    rec = sup.table.get(run)
    if rec is None:
        raise SpawnError("not_found", "no such run")
    harness = str(rec.get("harness") or "")
    out: dict[str, Any] = {"run": run, "harness": harness}
    path = tr.locate(run, harness, rec, env)
    child = args.get("child")
    if child is not None:
        if not (isinstance(child, str) and tr.CHILD.fullmatch(child)) or harness not in ("claude-code", "codex"):
            raise SpawnError("invalid.input", "transcript: invalid child")
        if path is not None and harness == "codex":
            from .transcript_children import codex_child_path

            path = codex_child_path(path, child, env)
        elif path is not None:
            path = path.with_suffix("") / "subagents" / f"{child}.jsonl"
    source = args.get("source")
    if source is not None and not (isinstance(source, str) and SOURCE_ID.fullmatch(source)):
        raise SpawnError("invalid.input", "transcript: invalid source")
    if path is None or not path.is_file():
        if source is not None:
            raise SpawnError("not_found", "no such source")
        return out | {"available": False, "reason": "transcript not found yet", "sources": []}
    cwd = rec.get("cwd_path") if isinstance(rec.get("cwd_path"), str) else None
    if source is not None:
        return extract(path, harness, source, cwd, sidechain=child is not None)
    return out | {"available": True, "sources": sources(path, harness, cwd, sidechain=child is not None)}

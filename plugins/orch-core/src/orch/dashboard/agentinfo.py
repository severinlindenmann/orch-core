"""What the agent in a Terminals session is doing (issue #40), from Claude Code's own files: read-only, local,
never sent anywhere.

The chain: the tmux pane's process is the agent (terminals.Session.pid) → `<claude dir>/sessions/<pid>.json`
(session id, busy | idle | waiting) → the transcript `<claude dir>/projects/*/<id>.jsonl` (title, tool steps, model
and tokens per message, PR links, an occasional cost record) → `<id>/subagents/*.meta.json` → orch's ticket claims
by that session id. A transcript is read once and then only its new bytes. Anything missing leaves its field None;
nothing here raises into a page.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ACTIVE_SECONDS = 120  # a subagent whose transcript changed this recently counts as running
TICKETS_SECONDS = 5.0  # how long the claims-by-session index is reused
CACHE_TTL = 300  # the prompt cache's default lifetime; a write marked ephemeral_1h lasts 3600


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _short(text, n: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _describe(name: str, data: dict) -> str:
    """A tool call as the person reads it: the description the agent gave, else what it touches."""
    for key in ("description", "file_path", "command", "pattern", "url", "query", "prompt"):
        if isinstance(data.get(key), str) and data[key].strip():
            return _short(data[key])
    return ""


class Transcript:
    """Running totals over one transcript file, fed incrementally."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.rest = b""
        self.title = None
        self.step = None  # (tool, what) of the latest tool call, or ("Reply", first line) of a text-only answer
        self.question = None  # the open AskUserQuestion, if that was the latest call
        self.last_prompt = None
        self.model = None
        self.models: Counter = Counter()
        self.tokens: Counter = Counter()
        self.context = None
        self.tools: Counter = Counter()
        self.prs: dict = {}
        self.cost = None
        self.cache_at = None  # epoch of the last reply that read or wrote the prompt cache
        self.cache_ttl = CACHE_TTL

    def update(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.offset:  # rewritten: start over
            self.__init__(self.path)
        if size == self.offset:
            return
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        self.offset = size
        lines = (self.rest + data).split(b"\n")
        self.rest = lines.pop()  # a line still being written
        for raw in lines:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                self._record(d)

    def _record(self, d: dict) -> None:
        kind = d.get("type")
        if kind == "ai-title" and d.get("aiTitle"):
            self.title = _short(d["aiTitle"], 90)
        elif kind == "pr-link" and isinstance(d.get("prNumber"), int) and not isinstance(d["prNumber"], bool):
            # the transcript is agent-written: a link is live only as an https URL that R24's canonical_link calls live
            from orch.dashboard.markdown import canonical_link
            url = str(d.get("prUrl") or "")
            live = url.startswith("https://") and canonical_link(url, None)[0] == "live"
            self.prs[d["prNumber"]] = {"number": d["prNumber"], "url": url if live else "",
                                       "repo": str(d.get("prRepository") or "")}
        elif kind == "cost-state":
            self.cost = {"usd": d.get("totalCostUSD"), "added": d.get("totalLinesAdded"),
                         "removed": d.get("totalLinesRemoved")}
        elif kind == "user":
            content = (d.get("message") or {}).get("content")
            if isinstance(content, list):
                if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                    return
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
                self.last_prompt = _short(content, 160)
        elif kind == "assistant":
            msg = d.get("message") or {}
            if msg.get("model") and not str(msg["model"]).startswith("<"):
                self.model = msg["model"]
                self.models[msg["model"]] += 1
            usage = msg.get("usage") or {}
            for key, name in (("output_tokens", "output"), ("input_tokens", "input"),
                              ("cache_read_input_tokens", "cache_read"), ("cache_creation_input_tokens", "cache_write")):
                if isinstance(usage.get(key), int):
                    self.tokens[name] += usage[key]
            if usage:
                written = usage.get("cache_creation") or {}
                if written.get("ephemeral_1h_input_tokens"):
                    self.cache_ttl = 3600
                elif written.get("ephemeral_5m_input_tokens"):
                    self.cache_ttl = CACHE_TTL
                try:  # a read or a write both restart the cache's clock
                    self.cache_at = datetime.fromisoformat(d["timestamp"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    pass
                self.context = sum(usage.get(k) or 0 for k in
                                   ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            texts = []
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    name, data = str(c.get("name") or "?"), c.get("input") or {}
                    self.tools[name] += 1
                    self.step = (name, _describe(name, data))
                    self.question = None
                    if name == "AskUserQuestion":
                        qs = data.get("questions") or []
                        if qs and isinstance(qs[0], dict):
                            self.question = _short(qs[0].get("question"), 160)
                elif c.get("type") == "text" and str(c.get("text") or "").strip():
                    texts.append(c["text"])
            if texts and not any(isinstance(c, dict) and c.get("type") == "tool_use" for c in msg.get("content") or []):
                self.step = ("Reply", _short(texts[-1].strip().splitlines()[0]))
                self.question = None


_TRANSCRIPTS: dict[str, Transcript] = {}
_LOCK = threading.Lock()
_TICKETS: dict[str, tuple[float, dict]] = {}


def _session_file(pid: int) -> dict | None:
    if not pid:
        return None
    try:
        data = json.loads((claude_dir() / "sessions" / f"{int(pid)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("sessionId"), str) else None


def _transcript(session_id: str) -> Transcript | None:
    if not session_id or "/" in session_id or ".." in session_id:
        return None
    with _LOCK:
        t = _TRANSCRIPTS.get(session_id)
        if t is None:
            found = next(iter(sorted((claude_dir() / "projects").glob(f"*/{session_id}.jsonl"))), None)
            if found is None:
                return None
            t = _TRANSCRIPTS[session_id] = Transcript(found)
        t.update()
        return t


def _subagents(t: Transcript, session_id: str) -> list[dict]:
    folder = t.path.parent / session_id / "subagents"
    out, now = [], time.time()
    for meta in sorted(folder.glob("*.meta.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        log = meta.with_name(meta.name.removesuffix(".meta.json") + ".jsonl")
        try:
            active = now - log.stat().st_mtime < ACTIVE_SECONDS
        except OSError:
            active = False
        out.append({"description": _short(data.get("description"), 80), "type": data.get("agentType") or "",
                    "model": data.get("model") or "", "active": active})
    out.sort(key=lambda s: not s["active"])
    return out


def _claims(ws) -> dict:
    """session id → [ticket dicts], rebuilt at most every TICKETS_SECONDS."""
    from orch.core import store
    from orch.dashboard.data.tasks import board_progress

    key = str(ws.root)
    hit = _TICKETS.get(key)
    if hit and time.monotonic() - hit[0] < TICKETS_SECONDS:
        return hit[1]
    index: dict = {}
    try:
        for e in store.scan(ws):
            claim = (e.meta or {}).get("claim") or {}
            sid = claim.get("session") if isinstance(claim, dict) else None
            if not sid or e.status == "done":
                continue
            prog = board_progress(e)
            index.setdefault(sid, []).append({"id": e.id, "title": (e.meta or {}).get("title") or "", "status": e.status,
                                              "tasks": prog["progress"]["short"] if prog else None})
    except Exception:  # a broken ticket never hides a terminal
        index = {}
    _TICKETS[key] = (time.monotonic(), index)
    return index


def cache(t: Transcript | None, now: float | None = None) -> dict | None:
    """Whether the next prompt finds the context in the prompt cache: warm with seconds left, or cold (it is
    written again in full). None before the first reply."""
    if not t or t.cache_at is None:
        return None
    left = int(t.cache_at + t.cache_ttl - (time.time() if now is None else now))
    return {"warm": left > 0, "left": max(left, 0), "ttl": t.cache_ttl}


def cache_label(c: dict | None) -> str:
    """cache warm · 42m left · cache cold."""
    if not c:
        return ""
    if not c["warm"]:
        return "cache cold"
    m = c["left"] // 60
    return f"cache warm · {m}m left" if m else f"cache warm · {c['left']}s left"


def model_label(model) -> str:
    """claude-opus-5-5 → Opus 5.5; anything unexpected stays as it is."""
    if not model:
        return ""
    parts = str(model).removeprefix("claude-").split("-")
    if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        return f"{parts[0].capitalize()} {parts[1]}.{parts[2]}"
    return str(model)


def info(ws, session) -> dict:
    """Everything the Terminals pages show about the agent in `session` (a terminals.Session). Never raises."""
    try:
        return _info(ws, session)
    except Exception:
        return dict(EMPTY)


EMPTY = {"known": False, "session_id": None, "status": None, "title": None, "now": "", "now_kind": "", "model": "",
         "models": [], "context": None, "tokens": {}, "tools": [], "prs": [], "cost": None, "last_prompt": None,
         "tickets": [], "subagents": [], "cache": None, "subagents_active": 0, "version": None, "sig": ""}


def _info(ws, session) -> dict:
    sf = _session_file(session.pid)
    status = sf.get("status") if sf else None
    status = status if status in ("busy", "idle", "waiting") else None  # the file is the agent's: only known states
    sid = sf.get("sessionId") if sf else None
    t = _transcript(sid) if sid else None
    tickets = _claims(ws).get(sid, []) if sid else []
    subs = _subagents(t, sid) if t else []
    if status == "waiting":
        now, kind = (f"Asks: {t.question}" if t and t.question else "Waiting for you"), "question"
    elif t and t.step:
        tool, what = t.step
        now, kind = (f"{tool} · {what}" if what else tool), "step"
    else:
        now, kind = "", ""
    progress = " ".join(f"{x['id']}:{x['tasks']}" for x in tickets)
    return {
        "known": sf is not None,
        "session_id": sid,
        "status": status,
        "title": t.title if t else None,
        "now": now,
        "now_kind": kind,
        "model": model_label(t.model) if t else "",
        "models": [(model_label(m), n) for m, n in t.models.most_common()] if t else [],
        "context": t.context if t else None,
        "tokens": dict(t.tokens) if t else {},
        "tools": t.tools.most_common(4) if t else [],
        "prs": sorted(t.prs.values(), key=lambda p: p["number"]) if t else [],
        "cost": t.cost if t else None,
        "last_prompt": t.last_prompt if t else None,
        "tickets": tickets,
        "cache": cache(t),
        "subagents": subs,
        "subagents_active": sum(1 for s in subs if s["active"]),
        "version": sf.get("version") if sf else None,
        # what "changed since you looked" compares: the state, the step and the ticket progress
        "sig": f"{status}|{now}|{progress}",
    }


def compact(n) -> str:
    """519680 → 520k, 188266056 → 188.3M."""
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}k"
    return str(n)

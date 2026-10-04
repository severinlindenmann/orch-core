"""Reading Claude Code's own files: transcripts, cost records and the limits log.

The transcript format is internal to Claude Code and changes between versions, so every reader here skips what it
does not understand and never raises. Nothing in this module runs while a page renders: the provider calls it.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

_PARSED: dict = {}  # path -> (size, mtime_ns, parsed); ponytail: in memory only, re-parsed after a restart


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def to_epoch(text) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_file(path: Path) -> dict:
    """{"msgs": [(epoch, model, output_tokens)], "runs": {startTime: cost-state}} for one transcript, cached by
    size and mtime. A reply is logged several times while it streams, under one message id: the LAST line counts."""
    try:
        st = path.stat()
    except OSError:
        return {"msgs": [], "runs": {}}
    hit = _PARSED.get(str(path))
    if hit and hit[:2] == (st.st_size, st.st_mtime_ns):
        return hit[2]
    last: dict = {}
    runs: dict = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                if '"assistant"' in line:
                    try:
                        row = json.loads(line)
                        msg = row["message"]
                        model, out = msg["model"], int(msg["usage"]["output_tokens"])
                        ts = to_epoch(row.get("timestamp"))
                    except (ValueError, KeyError, TypeError):
                        continue
                    if row.get("type") == "assistant" and isinstance(model, str) and not model.startswith("<") and ts is not None:
                        last[msg.get("id") or f"line{n}"] = (ts, model, out)
                elif '"cost-state"' in line:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("type") == "cost-state":
                        runs[str(row.get("startTime"))] = row  # cumulative per run: the last one wins
    except OSError:
        pass
    parsed = {"msgs": sorted(last.values()), "runs": runs}
    _PARSED[str(path)] = (st.st_size, st.st_mtime_ns, parsed)
    return parsed


def cost_of(parsed: dict) -> dict | None:
    """Sum of a session's process runs: {"models": {model: usd}, "total", "ms", "added", "removed"}, or None while no
    run has written its cost record (Claude Code writes it when a session ends)."""
    if not parsed["runs"]:
        return None
    out = {"models": {}, "total": 0.0, "ms": 0, "added": 0, "removed": 0}
    for run in parsed["runs"].values():
        try:
            out["total"] += float(run.get("totalCostUSD") or 0)
            out["ms"] += int(run.get("totalDuration") or 0)
            out["added"] += int(run.get("totalLinesAdded") or 0)
            out["removed"] += int(run.get("totalLinesRemoved") or 0)
            for model, u in (run.get("modelUsage") or {}).items():
                out["models"][model] = out["models"].get(model, 0.0) + float(u.get("costUSD") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def transcripts(root: Path) -> dict[str, Path]:
    """session id -> transcript, over every project folder of the Claude dir."""
    return {p.stem: p for p in sorted((root / "projects").glob("*/*.jsonl"))}


def subagents(transcript: Path) -> list[tuple[Path, str]]:
    """(transcript, description) of each subagent of a session."""
    out = []
    for f in sorted((transcript.parent / transcript.stem / "subagents").glob("agent-*.jsonl")):
        try:
            desc = json.loads(f.with_suffix(".meta.json").read_text(encoding="utf-8")).get("description")
        except (OSError, ValueError, AttributeError):
            desc = None
        out.append((f, desc if isinstance(desc, str) else ""))
    return out


def names(description: str, ticket_ids) -> str | None:
    """The first ticket id that appears as that exact string in `description` (B-29 is not in B-2914)."""
    best = None
    for tid in ticket_ids:
        m = re.search(rf"(?<![A-Za-z0-9-]){re.escape(tid)}(?![A-Za-z0-9])", description)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), tid)
    return best[1] if best else None


def read_limits(path: str) -> list[dict]:
    """The recorder's log, oldest first, lines that do not parse skipped."""
    rows = []
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
            ts = to_epoch(row["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if ts is not None and isinstance(row, dict):
            row["ts"] = ts
            rows.append(row)
    return sorted(rows, key=lambda r: r["ts"])


def week_rises(log: list[dict]) -> list[tuple[float, float, float, object]]:
    """(from_ts, to_ts, points, week_reset) for each new high of the weekly percentage within one weekly window.
    Readings come from several sessions and can be stale, so only a new high counts; a new reset time starts over."""
    out, reset, top, at = [], None, None, 0.0
    for r in log:
        w = r.get("week")
        if not isinstance(w, (int, float)) or isinstance(w, bool):
            continue
        if r.get("week_reset") != reset or top is None:
            reset, top, at = r.get("week_reset"), w, r["ts"]
        elif w > top:
            out.append((at, r["ts"], float(w - top), reset))
            top, at = w, r["ts"]
    return out


def distribute(rises, messages) -> dict[str, dict]:
    """Spread each rise over the owners of the output tokens produced in its interval. `messages` are
    (epoch, owner, output_tokens). Returns {owner: {"all": points, "week": points in the latest window}}."""
    latest = rises[-1][3] if rises else None
    out: dict = {}
    for lo, hi, points, reset in rises:
        by_owner: dict = {}
        for ts, owner, tokens in messages:
            if lo < ts <= hi:
                by_owner[owner] = by_owner.get(owner, 0) + tokens
        total = sum(by_owner.values())
        for owner, tokens in by_owner.items():
            if total:
                share = points * tokens / total
                e = out.setdefault(owner, {"all": 0.0, "week": 0.0})
                e["all"] += share
                if reset == latest:
                    e["week"] += share
    return out

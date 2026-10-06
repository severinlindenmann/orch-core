"""Reading Claude Code's own files: transcripts, cost records and the limits log.

The transcript format is internal to Claude Code and changes between versions, so every reader here skips what it
does not understand and never raises. Nothing in this module runs while a page renders: the provider calls it.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_PARSED: dict = {}  # path -> (size, mtime_ns, parsed); also kept in the addon's state folder (open_cache / save_cache)
_CACHE: dict = {"file": None, "dirty": False, "touched": set()}
CACHE_VERSION = 1
_RUN_KEYS = ("totalCostUSD", "totalDuration", "totalLinesAdded", "totalLinesRemoved")


def open_cache(path: Path) -> None:
    """Use `path` (a JSON file in the addon's state folder) as the parse cache: transcripts whose size and mtime did not
    change since the last run are not parsed again, so the first fetch after `orch serve` restarts stays cheap. A missing
    or unreadable file is an empty cache."""
    if _CACHE["file"] == path:
        return
    _CACHE.update(file=path, dirty=False, touched=set())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("v") != CACHE_VERSION:
            return
        for p, e in raw["files"].items():
            msgs = [(float(t), str(m), int(o)) for t, m, o in e["msgs"]]
            _PARSED.setdefault(p, (int(e["size"]), int(e["mtime_ns"]), {"msgs": msgs, "runs": dict(e["runs"])}))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return


def begin_fetch() -> None:
    _CACHE["touched"] = set()


def save_cache() -> None:
    """Write the entries this fetch used (so deleted transcripts drop out), only when something was parsed or dropped."""
    path = _CACHE["file"]
    if path is None:
        return
    keep = {p: v for p, v in _PARSED.items() if p in _CACHE["touched"]}
    if not _CACHE["dirty"] and len(keep) == len(_PARSED):
        return
    for p in set(_PARSED) - set(keep):
        del _PARSED[p]
    doc = {"v": CACHE_VERSION, "files": {p: {"size": s, "mtime_ns": m, "msgs": [list(x) for x in parsed["msgs"]],
                                              "runs": parsed["runs"]} for p, (s, m, parsed) in keep.items()}}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
        _CACHE["dirty"] = False
    except OSError:
        pass  # a cache that cannot be written only costs a re-parse next time


def _slim(run: dict) -> dict:
    """The fields cost_of reads from a cost-state row: the cache keeps nothing else."""
    out = {k: run.get(k) for k in _RUN_KEYS}
    usage = run.get("modelUsage")
    out["modelUsage"] = {m: {"costUSD": u.get("costUSD")} for m, u in usage.items() if isinstance(u, dict)} if isinstance(usage, dict) else {}
    return out


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


DEFAULT_LOG = "~/.claude/orch-usage/limits.jsonl"
RECORDER_MARK = "orch-usage/statusline.sh"


def recorder_wired(claude: Path) -> bool:
    """Claude's user settings have a status line that runs the recorder (by name, or a script file that names it)."""
    try:
        line = json.loads((claude / "settings.json").read_text(encoding="utf-8")).get("statusLine")
        command = line.get("command") if isinstance(line, dict) else None
        if not isinstance(command, str):
            return False
        if RECORDER_MARK in command:
            return True
        first = Path(command.split()[0]).expanduser() if command.split() else None
        return bool(first and first.is_file() and first.stat().st_size <= 256 * 1024
                    and RECORDER_MARK in first.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, AttributeError):
        return False


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
    _CACHE["touched"].add(str(path))
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
                        runs[str(row.get("startTime"))] = _slim(row)  # cumulative per run: the last one wins
    except OSError:
        pass
    parsed = {"msgs": sorted(last.values()), "runs": runs}
    _PARSED[str(path)] = (st.st_size, st.st_mtime_ns, parsed)
    _CACHE["dirty"] = True
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


def limits_log_state(path: str) -> dict:
    """{"path": what to show, "state": "ok" | "missing" | "relative"} for the configured Limits log: a relative path is
    not usable (it would depend on where Mission Control was started), a path that is not a file is missing."""
    raw = str(path or "")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        return {"path": raw, "state": "relative"}
    return {"path": str(p), "state": "ok" if p.is_file() else "missing"}


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
            for k in ("five", "week"):  # a percentage is 0..100; anything else (Infinity, NaN, a string) is unknown
                v = row.get(k)
                if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 100):
                    row[k] = None
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


FAMILIES = ("opus", "sonnet", "haiku", "fable", "other")


def family(model: str) -> int:
    """Index into FAMILIES: claude-opus-5-5 -> 0; anything unknown -> other."""
    m = model.lower()
    return next((i for i, f in enumerate(FAMILIES[:-1]) if f in m), len(FAMILIES) - 1)


def day_of(epoch: float) -> str:
    """The calendar day of an epoch in this machine's local zone, ISO (the dashboard runs on the user's machine)."""
    return datetime.fromtimestamp(epoch).date().isoformat()


def monday_of(day: str) -> str:
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def limit_history(log: list[dict], since: float, cap: int = 400) -> list[list[float]]:
    """[[epoch, five, week]] for each reading that is a new high of the 5-hour or the weekly percentage within its
    reset window (stale readings from other sessions are dropped, a new reset starts over). Thinned to `cap`."""
    state = {"five": [None, None], "week": [None, None]}  # key -> [reset, top]
    out = []
    for r in log:
        moved = False
        for key in state:
            v = r.get(key)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            reset, top = state[key]
            if top is None or r.get(key + "_reset") != reset:
                state[key] = [r.get(key + "_reset"), v]
                moved = True
            elif v > top:
                state[key][1] = v
                moved = True
        if moved and r["ts"] >= since and state["five"][1] is not None and state["week"][1] is not None:
            out.append([r["ts"], float(state["five"][1]), float(state["week"][1])])
    if len(out) > cap:
        step = -(-len(out) // cap)
        out = out[::step][:cap - 1] + [out[-1]]
    return out


def pace(log: list[dict], key: str) -> dict | None:
    """The newest `key` window of the log: {"first_ts", "last_ts", "first", "last", "n", "reset"} over its readings
    (stale readings below the running high are skipped), or None when there is no reading."""
    rows = [r for r in log if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]
    if not rows:
        return None
    reset = rows[-1].get(key + "_reset")
    first = last = None
    n = 0
    for r in (r for r in rows if r.get(key + "_reset") == reset):
        if first is None:
            first = last = r
            n = 1
        elif r[key] > last[key]:
            last = r
            n += 1
    return {"first_ts": first["ts"], "last_ts": last["ts"], "first": float(first[key]), "last": float(last[key]),
            "n": n, "reset": reset}

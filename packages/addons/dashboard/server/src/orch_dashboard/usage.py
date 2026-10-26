"""Usage (add-on, read only): how much agent time went into which ticket.

What is measured, honestly: the host records when each run of an agent started and ended (`run.list`, state `all`).
Those two times are the host's own, not the agent's. From them this module adds up, per ticket and per workspace,
the number of runs and the session time. Tokens and money are NOT measured: no harness reports them in a form this
dashboard can trust, so the answer says `not_reported` and the page shows "Not reported". Nothing is invented and
nothing an agent writes is shown as a number.

The host keeps ended runs for 7 days (at most 1 000), so a month cannot be read from it alone. This server therefore
keeps a small ledger of the ended runs it has seen (a copy of host values: run id, ticket, start, end, harness) in the
dashboard state directory, mode 0600, per person and workspace. The page says from when the ledger is complete.
The host lists a person's own runs only (column P), so the totals are "runs you started". The route answers only
while the Usage add-on is on, and writes nothing to the host. Money, when a trusted source exists, is whole cents
plus the currency (`money`), never a float.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict

from . import reads
from .adapters import board_statuses
from .adapters.addons import display
from .errors import HostError, fail
from .gateway import Gateway, state_dir

FILE = "usage_runs.json"
_LOCK = threading.Lock()
RUN = re.compile(r"^r_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
TIME = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d{1,6})?Z$")
TICKET = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}$")
HARNESS = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
LIVE = ("starting", "running")
DAY = 86400
WINDOWS = {"week": 7 * DAY, "month": 30 * DAY}
#: the host keeps ended runs at least this long, so the ledger is complete this far before its first look
HOST_KEEPS = 7 * DAY
MAX_LEDGER = 5000  # runs kept per person and workspace; the oldest go first
MAX_AGE = 400 * DAY
MAX_PAGES = 6
MAX_FILE = 8 * 1024 * 1024
RECENT = 8
#: one run longer than this is not believed as a session time (a clock jump): its time counts as unknown
MAX_RUN = 14 * DAY
CURRENCY = re.compile(r"^[A-Z]{3}$")


def now() -> float:
    return time.time()


def parse(value) -> float | None:
    """Seconds since 1970 of a host time `YYYY-MM-DDTHH:MM:SS[.f]Z`, or None for anything else."""
    m = TIME.fullmatch(value) if isinstance(value, str) else None
    if not m:
        return None
    try:
        y, mo, d, h, mi, s = (int(g) for g in m.groups())
        base = datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp()
    except ValueError:
        return None
    frac = re.search(r"\.(\d{1,6})Z$", value)
    return base + (int(frac.group(1).ljust(6, "0")) / 1e6 if frac else 0.0)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def money(cents, currency) -> dict | None:
    """`{cents, currency}` for a whole number of cents and a three-letter currency; anything else is None (a float,
    a bool, a negative number or a made-up code is never shown as money)."""
    if type(cents) is not int or cents < 0 or not isinstance(currency, str) or not CURRENCY.fullmatch(currency):
        return None
    return {"cents": cents, "currency": currency}


def fact(row) -> dict | None:
    """One run of the host as a ledger fact `{run, ticket, harness, start, end, live}`, or None when it is not an
    agent run with a start time the host gave."""
    if not isinstance(row, dict) or row.get("kind") != "agent" or not isinstance(row.get("run"), str):
        return None
    if not RUN.fullmatch(row["run"]):
        return None
    start = parse(row.get("started_at"))
    if start is None:
        return None
    ticket = row.get("ticket")
    ticket = ticket if isinstance(ticket, str) and TICKET.fullmatch(ticket) else None
    harness = row.get("harness")
    harness = harness if isinstance(harness, str) and HARNESS.fullmatch(harness) else None
    live = row.get("state") in LIVE
    exit_ = row.get("exit") if isinstance(row.get("exit"), dict) else {}
    end = None if live else parse(exit_.get("at"))
    if end is not None and (end < start or end - start > MAX_RUN):
        end = None  # a time that cannot be right is unknown, not guessed
    return {"run": row["run"], "ticket": ticket, "harness": harness, "start": start, "end": end, "live": live}


# -- the ledger --


def _path(env=None) -> Path:
    return state_dir(env) / "dashboard" / FILE


def _read(path: Path) -> dict:
    from orch.host.custody import read_json

    try:  # O_NOFOLLOW, regular file, ours, 0600, size cap: the same rules as the write side
        doc = read_json(path, os.getuid(), limit=MAX_FILE)
        if isinstance(doc, dict) and doc.get("v") == 1 and isinstance(doc.get("people"), dict):
            return doc
    except Exception:  # noqa: BLE001 - any unsafe or unreadable file: start fresh
        pass
    return {"v": 1, "people": {}}


def remember(person: str, ws: str, facts: list[dict], at: float, env=None) -> dict:
    """Add the ended runs in `facts` to this person's ledger for `ws` (idempotent by run id) and return the entry
    `{first_seen, runs: {run: [ticket, start, end|None, harness]}}`. An unreadable file starts a fresh ledger."""
    from orch.host.custody import write_private

    path = _path(env)
    with _LOCK:
        doc = _read(path)
        mine = doc["people"].get(person)
        if not isinstance(mine, dict):
            mine = doc["people"][person] = {}
        entry = mine.get(ws)
        if not isinstance(entry, dict):
            entry = mine[ws] = {}
        if not isinstance(entry.get("runs"), dict) or type(entry.get("first_seen")) not in (int, float):
            entry.clear()
            entry.update(first_seen=at, runs={})
        runs = entry["runs"]
        for f in facts:
            if not f["live"]:
                runs[f["run"]] = [f["ticket"], f["start"], f["end"], f["harness"]]
        for run in [r for r, v in runs.items() if not _row_ok(v) or v[1] < at - MAX_AGE]:
            del runs[run]
        for run in sorted(runs, key=lambda r: runs[r][1])[: max(0, len(runs) - MAX_LEDGER)]:
            del runs[run]
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        write_private(path, doc)
        return {"first_seen": entry["first_seen"], "runs": dict(runs)}


def _row_ok(v) -> bool:
    return (
        isinstance(v, list)
        and len(v) == 4
        and type(v[1]) in (int, float)
        and (v[2] is None or type(v[2]) in (int, float))
    )


# -- the answer --


def _add(bucket: dict, f: dict) -> None:
    bucket["runs"] += 1
    if f["live"]:
        bucket["running"] += 1
    if f["end"] is not None:
        bucket["ms"] += int((f["end"] - f["start"]) * 1000)
    elif not f["live"]:
        bucket["unknown"] += 1


def _empty() -> dict:
    return {"runs": 0, "ms": 0, "running": 0, "unknown": 0}


def summarize(facts: list[dict], at: float, covered_from: float, shown: set[str], titles: dict) -> dict:
    """The windows `week` and `month`: runs and session time in total and per ticket. A run counts in a window when
    it ended inside it (or is still running); its whole time counts. A live run's time so far is added."""
    out = {}
    for name, span in WINDOWS.items():
        cutoff = at - span
        total, by = _empty(), {}
        for f in facts:
            if not f["live"] and (f["end"] if f["end"] is not None else f["start"]) < cutoff:
                continue
            g = dict(f)
            if g["live"]:
                g["end"] = min(max(at, g["start"]), g["start"] + MAX_RUN)
            key = g["ticket"] if g["ticket"] in shown else ("" if g["ticket"] is None else None)
            _add(total, g)
            _add(by.setdefault(key, {"ticket": key, **_empty()}), g)
        rows = []
        for key, b in by.items():
            row = {k: b[k] for k in ("runs", "ms", "running", "unknown")}
            row["ticket"] = key or None
            row["title"] = titles.get(key) if key else None
            row["other"] = key is None
            row["none"] = key == ""
            rows.append(row)
        rows.sort(key=lambda r: (-r["ms"], -r["runs"], r["ticket"] or "~"))
        out[name] = {
            **total,
            "tickets": rows,
            "counted_since": iso(covered_from) if cutoff < covered_from else None,
        }
    return out


def recent(facts: list[dict], at: float) -> list[dict]:
    rows = sorted(facts, key=lambda f: -f["start"])[:RECENT]
    return [
        {
            "started_at": iso(f["start"]),
            "ended_at": iso(f["end"]) if f["end"] is not None else None,
            "ms": int(((min(max(at, f["start"]), f["start"] + MAX_RUN) if f["live"] else f["end"]) - f["start"]) * 1000)
            if (f["live"] or f["end"] is not None)
            else None,
            "running": f["live"],
            "harness": f["harness"],
        }
        for f in rows
    ]


class Q(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: reads.Key | None = None


def register(app: FastAPI, gateway: Gateway) -> None:
    def fetch(link) -> list[dict]:
        out, page = [], None
        for _ in range(MAX_PAGES):
            data = link.call("run.list", {"state": "all", "kind": "agent", **({"page": page} if page else {})})
            out += [f for f in (fact(r) for r in data.get("runs") or []) if f]
            page = data.get("next_page")
            if not page:
                return out
        return out

    def titles(ws) -> dict:
        """Ticket titles the person can see (best effort: none when the board cannot be read)."""
        try:
            rows, _ = reads.all_rows(gateway, ws, {"status": board_statuses(), "sort": "updated"}, cap=400)
        except HostError:
            return {}
        return {r["key"]: display(r.get("title")) for r in rows if isinstance(r.get("key"), str)}

    @app.get("/api/w/{ws}/usage")
    def usage(query: Annotated[Q, Query()], ws: str = Depends(reads.workspace)):
        link = gateway.enabled(ws)
        if not link.supports("run.list", "run/1"):
            return {"available": False}
        if not gateway.addon_on(link, "usage"):
            raise fail("addon.disabled", exit=3)  # the add-on is off: no numbers, whatever the page asks
        at = now()
        person = link.welcome["peer"]["person"]
        fresh = fetch(link)  # always all of the person's runs: the ledger must never claim to be complete from a part
        entry = remember(person, ws, fresh, at)
        kept = {
            r: {"run": r, "ticket": v[0], "harness": v[3], "start": v[1], "end": v[2], "live": False}
            for r, v in entry["runs"].items()
        }
        for f in fresh:
            kept[f["run"]] = f
        facts = list(kept.values())
        names = titles(ws)
        # only tickets on the person's current board are named; every other row (a ticket no longer visible, or a board
        # that cannot be read) is folded into "other tickets"
        shown = set(names)
        if query.ticket:
            ok = query.ticket in shown  # a ticket this person cannot see has no numbers here
            facts = [f for f in facts if f["ticket"] == query.ticket and ok]
            shown = {query.ticket}
        covered = entry["first_seen"] - HOST_KEEPS
        return {
            "available": True,
            "scope": "your",  # the host lists a person's own runs only
            "windows": summarize(facts, at, covered, shown, names),
            "recent": recent(facts, at) if query.ticket else [],
            "tokens": None,
            "cost": None,
            "not_reported": "Tokens and cost are not reported in a form this dashboard can trust, so none are shown.",
        }

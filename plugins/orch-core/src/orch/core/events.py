from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field

from orch.clock import stamp, stamp_s
from orch.core.locks import lock

EVENT_KINDS = frozenset({
    "ticket.created", "ticket.moved", "ticket.edited", "log.added", "state.updated",
    "question.asked", "question.answered", "gate.approved", "gate.invalidated",
    "gate.changes_requested",
    "verdict.given", "artifact.added", "claim.taken", "claim.released", "addon.action",
    "addon.decision",
    "task.added", "task.edited", "task.moved", "ledger.adopted",
    "gate.delegated", "delegation.paused",  # epics: an agent's approval under delegation; the human's pause
    "ticket.option",  # a human (or the addon relaying the human's phone) set an addon's yes/no option on a ticket
    "setting.changed",  # a workspace setting changed through orch (`orch widget html`)
    # AI Factory (orch.core.permits): an agent's request and a grant's use; the human's answers (signed in the ledger)
    "permit.requested", "permit.used", "permit.granted", "permit.denied", "permit.revoked",
    # Quick tasks (orch.core.quick): one-line jobs outside the ticket flow; `data["quick"]` names the task
    "quick.added", "quick.claimed", "quick.released", "quick.done", "quick.outgrew", "quick.reopened",
    "quick.dropped", "quick.promoted", "quick.artifact",
})


@dataclass(frozen=True)
class Actor:
    kind: str  # "human" | "agent"
    name: str
    via: str  # "tty" | "dashboard" | "cli" | "addon:<name>" | "phone:<label>" | "check"
    session: str | None = None
    device: str | None = None  # a paired phone's id (orch.remote): the signed ledger entry names it

    def __post_init__(self):
        if self.kind not in ("human", "agent"):
            raise ValueError(f"actor kind must be human or agent, got {self.kind!r}")

    @property
    def is_human(self) -> bool:
        return self.kind == "human"

    @property
    def label(self) -> str:
        if self.is_human:
            return "you"
        return f"{self.name} {self.session[:4]}" if self.session else self.name

    def to_str(self) -> str:
        if self.is_human:
            return "human:you"
        return f"agent:{self.name}" + (f":{self.session[:8]}" if self.session else "")


@dataclass(frozen=True)
class Event:
    seq: int
    at: str
    ticket: str | None
    kind: str
    actor: str
    via: str
    data: dict = field(default_factory=dict)
    # Human events only: the process evidence of the writer (orch.actor.process_evidence), for `orch check`.
    evidence: dict | None = None

    def to_json(self) -> str:
        d = asdict(self)
        if d["evidence"] is None:
            del d["evidence"]
        return json.dumps(d, ensure_ascii=False)


def _path(ws):
    return ws.state_dir / "events.jsonl"


def events_path(ws):
    """The event log file (read-only callers such as `orch wait` watch its size)."""
    return _path(ws)


# How far a seq may jump ahead of the last valid one and still count. events.jsonl is tracked in git, so a merge or
# a hand trim leaves real gaps; a jump beyond this is not a gap but a line meant to push cursors past real events.
MAX_SEQ_GAP = 1000


@dataclass(frozen=True)
class Tampered:
    """A line of the event log that orch did not write as it stands. In `EventScan.tampered` (skipped by every
    reader): no readable event, or a seq that jumps far ahead, repeats, goes back or is not an integer. In
    `EventScan.gaps` (accepted): a seq that skips at most MAX_SEQ_GAP. `orch check` reports both."""
    line: int  # 1-based line number in events.jsonl
    seq: object  # the seq the line claims (None when the line is no readable event)
    reason: str
    level: str = "warning"  # "error" only for a far-ahead jump
    hint: str = ""


@dataclass(frozen=True)
class EventScan:
    """One pass over the event log: the events orch numbered (seq 1, 2, 3, ... in file order) and every line that
    breaks that order. `offset` is the byte length of the complete lines read (a last line without its newline is
    still being written and is left for the next scan)."""
    events: tuple = ()
    tampered: tuple = ()
    gaps: tuple = ()
    offset: int = 0
    lines: int = 0
    digest: bytes = b""

    @property
    def last_seq(self) -> int:
        return self.events[-1].seq if self.events else 0


# path -> (inode, size, mtime_ns, ctime_ns, scan): the long-running dashboard parses only what was appended
_CACHE: dict[str, tuple[int, int, int, int, EventScan]] = {}
_CACHE_LOCK = threading.Lock()


_MERGE_HINT = "likely a git merge or a hand edit; the copy is ignored, review the raw log line"


def _parse(raw: bytes, base: EventScan) -> EventScan:
    """Continue `base` over the complete lines in `raw` (the bytes after base.offset)."""
    events, tampered, gaps = list(base.events), list(base.tampered), list(base.gaps)
    last, n, used = base.last_seq, base.lines, 0
    while True:
        end = raw.find(b"\n", used)
        if end < 0:
            break
        line, used, n = raw[used:end], end + 1, n + 1
        if not line.strip():
            continue
        try:
            d = json.loads(line.decode("utf-8"))
            seq = d["seq"]
            evidence = d.get("evidence") if isinstance(d.get("evidence"), dict) else None
            event = Event(seq, d["at"], d.get("ticket"), d["kind"], d["actor"], d.get("via", ""),
                          d.get("data") or {}, evidence)
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
            tampered.append(Tampered(n, None, "not a readable event", hint="a torn write or a hand edit; ignored"))
            continue
        if type(seq) is not int:  # bool, float and str seqs are never written by orch
            tampered.append(Tampered(n, seq, f"seq {seq!r} is not an integer", hint="ignored"))
        elif not events:  # the first event may start anywhere: a log whose head was trimmed stays readable
            if seq >= 1:
                events.append(event)
                last = seq
            else:
                tampered.append(Tampered(n, seq, f"seq {seq} is not positive", hint="ignored"))
        elif seq == last + 1:
            events.append(event)
            last = seq
        elif seq > last + MAX_SEQ_GAP:
            tampered.append(Tampered(n, seq, f"seq {seq} jumps far ahead (expected {last + 1})", "error",
                                     "orch never writes this; ignored, no cursor moves past it"))
        elif seq > last:
            gaps.append(Tampered(n, seq, f"seq {seq} skips {seq - last - 1} (expected {last + 1}); accepted",
                                 hint="likely a git merge or a hand trim"))
            events.append(event)
            last = seq
        elif seq == last:
            tampered.append(Tampered(n, seq, f"seq {seq} is a duplicate", hint=_MERGE_HINT))
        else:
            tampered.append(Tampered(n, seq, f"seq {seq} is out of order (expected {last + 1})", hint=_MERGE_HINT))
    return EventScan(tuple(events), tuple(tampered), tuple(gaps), base.offset + used, n)


def _scan(path) -> EventScan:
    """The event log, validated in a single pass. Appends since the last scan of this file are parsed on their own;
    a log that was replaced or rewritten (its earlier bytes changed) is parsed again from the start."""
    key = os.fspath(path)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            if cached and cached[:4] == (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns):
                return cached[4]
            data = f.read()
    except FileNotFoundError:
        return EventScan()
    base = EventScan()
    if cached and cached[0] == st.st_ino and cached[4].offset <= len(data):
        prev = cached[4]
        if hashlib.blake2b(data[:prev.offset], digest_size=16).digest() == prev.digest:
            base = prev
    scan = _parse(data[base.offset:], base) if base.offset < len(data) else base
    if scan is not base:
        scan = EventScan(scan.events, scan.tampered, scan.gaps, scan.offset, scan.lines,
                         hashlib.blake2b(data[:scan.offset], digest_size=16).digest())
    with _CACHE_LOCK:
        _CACHE[key] = (st.st_ino, len(data), st.st_mtime_ns, st.st_ctime_ns, scan)
    return scan


def scan_events(ws) -> EventScan:
    """The valid events and the tampered lines of the workspace's event log (`orch check` reports the latter)."""
    return _scan(_path(ws))


def last_seq(ws) -> int:
    """The seq of the last event orch numbered (0 when there is none). A line with a forged higher seq never
    counts, so cursors that start "at now" never jump past real events still to come."""
    return _scan(_path(ws)).last_seq


def append_event(ws, ticket_id: str | None, kind: str, actor: Actor, data: dict | None = None) -> Event:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")
    path = _path(ws)
    evidence = None
    if actor.is_human:
        from orch.actor import process_evidence
        evidence = process_evidence()
    with lock(ws, "events"):
        scan = _scan(path)
        if path.exists() and path.stat().st_size > scan.offset:
            # a last line cut short (no newline) becomes a line of its own instead of swallowing this event
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write("\n")
            scan = _scan(path)
        data = dict(data or {})
        if actor.device and actor.via.startswith("device:"):  # a bridged post: name the device; not a signed phone decision
            data["device"] = actor.device
        event = Event(scan.last_seq + 1, stamp_s(), ticket_id, kind, actor.to_str(), actor.via, data, evidence)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(event.to_json() + "\n")
    return event


def read_events(ws, ticket_id: str | None = None, after: int = 0) -> list[Event]:
    """The events orch numbered, oldest first; tampered lines (see `Tampered`) are left out. Seqs only ever rise,
    by at most MAX_SEQ_GAP, so `after` works as a cursor: a forged far-ahead seq cannot move it."""
    out = []
    for event in _scan(_path(ws)).events:
        if event.seq <= after or (ticket_id and event.ticket != ticket_id):
            continue
        out.append(event)
    return out


def log_line(actor: Actor, text: str) -> str:
    return f"- {stamp()} [{actor.label}] {text}"


def latest_testing_round(events) -> int:
    """The current testing round of a ticket: the event seq of the latest move into testing (`ticket.moved`
    with `data["to"] == "testing"`), 0 if it was never there. A verdict decision is bound to this number so a
    phone's signed verdict from an earlier round (before a follow-up moved the ticket back into testing) can
    never be applied once the ticket has gone through another round."""
    return max((e.seq for e in events if e.kind == "ticket.moved" and e.data.get("to") == "testing"), default=0)

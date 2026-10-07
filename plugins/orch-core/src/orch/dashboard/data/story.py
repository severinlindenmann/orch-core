"""The ticket page as a story (#16, ticket design review §3.4): Asked → Agreed → Doing → Proven, then the
agent's notes and the activity. Pure helpers over a ticket, its events and its ticket_card(); every heading and
sentence here is rule text. Agent prose only appears in the Agent notes (and, in testing, the handoff under the status
card), attributed to whoever wrote it."""
from __future__ import annotations

import difflib
import re

from orch.core import evidence
from orch.core.constants import AGENT_NOTES, SECTIONS
from orch.core.gates import HASH_VERSION, gate_hash, gate_meta, gate_parts, normalized_text, snapshot_matches
from orch.dashboard.data.steps import CHANGES_REQUESTED, YOUR_TURN, day, when
from orch.dashboard.data.timeline import describe, who

# What is left is one line in the status card (cards._left), not a chapter of its own.
CHAPTERS = ("asked", "agreed", "doing", "proven")
CHAPTER_TITLES = {"asked": "Asked", "agreed": "What we agreed", "doing": "Doing", "proven": "Proof so far"}
NOTE_LABELS = {"Current state": "Handoff", "Context": "Context", "Findings": "Findings"}
# Events that only say "a section changed": folded on the timeline (the text itself is on the page).
_NOISE = ("ticket.edited", "state.updated", "task.edited")
_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+")


def items(text: str) -> int:
    """Top-level list items in a section (or 1 for prose), for "Requirements (3)" summaries."""
    n = sum(1 for line in (text or "").split("\n") if _ITEM.match(line))
    return n or (1 if (text or "").strip() else 0)


def short_hash(h) -> str:
    """"sha256 ab1e…7f" for a gate hash."""
    h = str(h or "")
    body = h.removeprefix("sha256:")
    return f"sha256 {body[:4]}…{body[-2:]}" if len(body) > 8 else h


def current_chapter(t, card: dict) -> str | None:
    status = t.status
    if status == "done":
        return None
    if status == "backlog":
        return "agreed" if t.section("Requirements").strip() else "asked"
    if status == "testing":
        return "proven"
    plan = card["gates"]["plan"]["state"]
    if status != "open" and (plan in ("you", "invalidated", "changes") or (plan == "pending" and not card["tasks"])):
        return "agreed"
    return "doing"


def shown_version(t, gate: str, *, approving: bool) -> int:
    """The hash version whose coverage the page shows for a gate: the current version when the human approves it
    now (a new approval binds that), else the version the approval was recorded with (`hash_v`; an approval without
    it is v1 when its hash is the v1 hash, else the current version)."""
    if approving:
        return HASH_VERSION
    g = (t.meta.get("gates") or {}).get(gate) or {}
    v = g.get("hash_v")
    if v not in (None, ""):
        try:
            return int(v)
        except (TypeError, ValueError):
            return HASH_VERSION
    return 1 if g.get("hash") and g.get("hash") == gate_hash(t, gate, 1) else HASH_VERSION


def gated_sections(t, gate: str, version: int = HASH_VERSION) -> tuple[list[str], list[tuple[str, str]]]:
    """(section names, frontmatter (key, value) pairs) that the gate's hash of `version` binds, in hash order:
    main's gate_parts/gate_meta, so the page shows exactly what an approval covers (v2 requirements: a non-empty
    Summary first, then the three sections, plus size and type)."""
    return [name for name, _ in gate_parts(t, gate, version)], gate_meta(t, gate, version)


def diff(approved: str | None, current: str) -> list[dict] | None:
    """A unified diff of the approved snapshot against the current gate text, as [{kind: add|del|ctx|gap, text}];
    None when no snapshot was kept."""
    if approved is None:
        return None
    out = []
    for line in difflib.unified_diff(approved.rstrip("\n").split("\n"), current.rstrip("\n").split("\n"),
                                     lineterm="", n=2):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            out.append({"kind": "gap", "text": "…"})
        elif line.startswith("+"):
            out.append({"kind": "add", "text": line[1:]})
        elif line.startswith("-"):
            out.append({"kind": "del", "text": line[1:]})
        elif line[1:].strip():  # blank context lines only stretch the block
            out.append({"kind": "ctx", "text": line[1:]})
    return out


def gate_view(ws, t, gate: str, card: dict, *, can_approve: bool, seen: str, snapshot=None, question=None,
              unsigned: bool = False) -> dict:
    """One gate in the Agreed chapter: its sections, state, approval record, and (when the human approves it now)
    the full text open above the Approve button, with a diff against the approved snapshot for a re-approve. The
    sections and frontmatter shown are exactly what the hash covers (`gated_sections`); a section holding Markdown
    link definitions (hashed but invisible when rendered) is shown as raw text."""
    from orch.dashboard.data.decisions import _REF_DEF

    g = (t.meta.get("gates") or {}).get(gate) or {}
    shown = card["gates"][gate]["state"]
    approving = can_approve and shown == "you"
    reapprove = bool(g.get("approved")) and shown in ("you", "invalidated")
    version = shown_version(t, gate, approving=approving or reapprove)
    names, meta = gated_sections(t, gate, version)
    sections = []
    for name in names:
        text = t.section(name)
        entry = {"name": name, "text": text, "count": items(text), "raw": bool(_REF_DEF.search(text))}
        if name == "Acceptance criteria":
            entry["criteria"] = evidence.criteria(t)
        sections.append(entry)
    cr = g.get("changes_requested") if isinstance(g.get("changes_requested"), dict) else None
    return {
        "gate": gate, "state": shown, "word": card["gates"][gate]["word"], "glyph": card["gates"][gate]["glyph"],
        "role": card["gates"][gate]["role"], "sections": sections, "filled": any(s["text"].strip() for s in sections),
        "meta": [{"name": k, "value": v} for k, v in meta], "version": version,
        "approved_day": day(g.get("approved")), "approved_at": when(g.get("approved")), "via": g.get("via"),
        "hash": short_hash(g.get("hash")) if g.get("hash") else "", "seen": seen, "seen_short": short_hash(seen),
        "open": approving, "can_approve": approving, "question": question if approving else None,
        "unsigned": unsigned and shown == "approved",
        "reapprove": reapprove, "diff": (diff(snapshot, normalized_text(t, gate)) if reapprove and snapshot_matches(t, gate, snapshot)
                 else None),
        "changes": cr if cr and shown == "changes" else None,
    }


def _author(events, kinds: tuple, section: str | None) -> dict | None:
    for e in reversed(events):
        if e.kind in kinds and (section is None or (e.data or {}).get("section") == section):
            return {"who": who(e), "at": when(e.at)}
    return None


def evidence_author(events) -> dict | None:
    """Who last wrote the Verification section (its evidence lines), from the events; None when edited by hand."""
    return _author(events, ("ticket.edited",), "Verification")


def agent_notes(t, events) -> list[dict]:
    """Handoff (Current state), Context, Findings: each with who
    last wrote it and when (from the events), or no author when the file was edited by hand."""
    out = []
    extra = [s for s in t.sections if s not in SECTIONS]  # a `## Heading` orch does not know
    for name in (*AGENT_NOTES, *extra):
        text = t.section(name)
        if not text.strip():
            continue
        if name == "Current state":
            author = _author(events, ("state.updated",), None) or _author(events, ("ticket.edited",), name)
        else:
            author = _author(events, ("ticket.edited",), name)
        out.append({"name": name, "label": NOTE_LABELS.get(name, name), "text": text, "author": author})
    return out


def timeline(events, limit: int = 50) -> list[dict]:
    """Newest first. A run of section edits by the same actor folds into one row ("edited Requirements, Plan ·
    4 edits"); every other event is its own row."""
    rows: list[dict] = []
    for e in reversed(events):
        noise = e.kind in _NOISE and not any((e.data or {}).get(k) for k in ("branch", "pr", "worktree", "external", "raw"))
        actor = who(e)
        if e.kind == "ledger.adopted" and rows and rows[-1]["kind"] == "ledger.adopted":
            rows[-1]["adopted"] += 1  # `orch ledger adopt` writes one event per decision: one line says it
            rows[-1]["first"] = when(e.at)
            continue
        if noise and rows and rows[-1]["folded"] and rows[-1]["who"] == actor:
            row = rows[-1]
            row["count"] += 1
            section = (e.data or {}).get("section") or ("Handoff" if e.kind == "state.updated" else (e.data or {}).get("task"))
            if section and section not in row["sections"]:
                row["sections"].append(section)
            row["first"] = when(e.at)
            continue
        if len(rows) >= limit:
            break
        section = (e.data or {}).get("section") or ("Handoff" if e.kind == "state.updated" else (e.data or {}).get("task"))
        rows.append({"at": when(e.at), "raw_at": e.at, "who": actor, "what": describe(e), "folded": noise, "count": 1,
                     "sections": [section] if noise and section else [], "first": when(e.at), "kind": e.kind, "adopted": 1})
    for row in rows:
        if row["adopted"] > 1:
            row["what"] = f"adopted {row['adopted']} earlier decisions into the ledger"
        if row["folded"] and row["count"] > 1:
            row["what"] = "edited " + (", ".join(row["sections"]) if row["sections"] else "the ticket") + f" · {row['count']} edits"
    return rows


ACTIVITY_MAX = 400  # events the ticket page's Activity reads (the newest); the raw Log has the rest
_STATUS_WORDS = {"backlog": "Backlog", "open": "Open", "in-progress": "In progress", "waiting": "Waiting",
                 "testing": "Testing", "done": "Done"}
_FOLD_PHRASE = {"artifact.added": "added {n} files", "ticket.edited": "edited the ticket {n} times",
                "state.updated": "updated the handoff {n} times", "task.edited": "edited tasks {n} times",
                "log.added": "logged {n} lines", "task.added": "added tasks {n} times",
                "claim.taken": "claimed it {n} times", "question.asked": "asked {n} times"}
_TASK_ID = re.compile(r"^[A-Za-z]{1,3}\d{1,4}$")


def elapsed(start, end) -> str:
    """"7 min", "4 h 32", "2 d 3 h" between two datetimes; "" when either is unknown."""
    if not start or not end:
        return ""
    s = max(0, int((end - start).total_seconds()))
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60:02d}"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def _fold_key(e) -> tuple | None:
    """Events that fold with their neighbours of the same actor: files, section edits, task moves of one kind."""
    if e.kind == "task.moved":
        return ("task.moved", str((e.data or {}).get("now")))
    if e.kind == "ticket.edited" and any((e.data or {}).get(k) for k in ("branch", "pr", "worktree", "external", "raw")):
        return None  # a link is worth its own line
    return (e.kind,) if e.kind in _FOLD_PHRASE else None


def _fold_text(key: tuple, events: list) -> str:
    if key[0] == "task.moved":
        from orch.dashboard.data.timeline import _TASK_VERB
        ids = [str((e.data or {}).get("task")) for e in events if _TASK_ID.match(str((e.data or {}).get("task") or ""))]
        verb = _TASK_VERB.get(key[1], "moved")
        return f"{verb} {len(events)} tasks" + (f" ({', '.join(ids)})" if ids and len(ids) <= 6 else "")
    return _FOLD_PHRASE[key[0]].format(n=len(events))


def activity(events, now=None) -> list[dict]:
    """The ticket page's Activity: oldest first, split at every status change into rounds (a status entered twice,
    say testing after a send-back, is "round 2"), each {label, round, start, end, elapsed, you, rows}. Within a round
    a run of 2+ events of one kind by the same actor folds into one row that keeps its events for a disclosure; a
    row shows its time only when the minute changes. Durations are wall-clock time between status changes, never
    split into agent time and the human's (nothing measures that)."""
    from orch.clock import now as clock_now
    events = list(events)[-ACTIVITY_MAX:]
    rounds: list[dict] = []
    seen: dict[str, int] = {}

    def start_round(status: str, at) -> dict:
        seen[status] = seen.get(status, 0) + 1
        r = {"status": status, "label": _STATUS_WORDS.get(status, status.capitalize() if status else "Created"),
             "round": seen[status], "start": at, "end": None, "elapsed": "", "rows": [], "you": status == "testing"}
        rounds.append(r)
        return r

    current = None
    for e in events:
        data = e.data or {}
        at = when(e.at)
        if e.kind == "ticket.moved" and data.get("to") and data.get("to") != data.get("from"):
            if current is not None:
                current["end"] = at
            current = start_round(str(data["to"]), at)
        elif current is None:
            current = start_round(str(data.get("status") or "backlog") if e.kind == "ticket.created" else "", at)
        actor = who(e)
        key = _fold_key(e)
        rows = current["rows"]
        last = rows[-1] if rows else None
        if key and last and last["key"] == key and last["who"] == actor:
            last["events"].append({"at": at, "what": describe(e)})
            last["what"] = _fold_text(key, last["raw"] + [e])
            last["raw"].append(e)
            continue
        rows.append({"at": at, "who": actor, "what": describe(e), "key": key, "raw": [e], "events": [],
                     "human": actor == "you", "kind": e.kind})
    for r in rounds:
        prev_minute = None
        for row in r["rows"]:
            if row["events"]:  # a folded run: its first event's line leads the disclosure
                row["events"].insert(0, {"at": row["at"], "what": describe(row["raw"][0])})
            minute = row["at"].astimezone().strftime("%d.%m %H:%M") if row["at"] else None
            row["time"] = row["at"].astimezone().strftime("%H:%M") if row["at"] and minute != prev_minute else ""
            prev_minute = minute
            del row["raw"]
    if rounds:
        last = rounds[-1]
        if last["status"] != "done":
            last["end"] = None
            last["open"] = True
        end_now = now or clock_now()
        for r in rounds:
            r["elapsed"] = elapsed(r["start"], r["end"] or (end_now if r.get("open") else None))
            r["day"] = r["start"].astimezone().strftime("%d.%m") if r["start"] else ""
    return rounds


JOURNEY = ("Asked", "Agreed", "Doing", "Proven", "Done")


def _moved_to(events, status: str):
    """When the ticket last entered `status` (its newest status-change event), or None."""
    for e in reversed(events):
        if (e.data or {}).get("to") == status and (e.data or {}).get("from") != status:
            return when(e.at)
    return None


def gate_signers(ws, t, signed=None) -> dict:
    """R26: who agreed to each approved gate, as the signed ledger on this machine says it: "you", "from your phone"
    (the signed entry's own `via`), "by delegation" (an agent under a signed epic delegation), or None when the
    ledger does not back the approval (ledger.gate_verification "unverified", the same check as the unsigned chip).
    The ticket's frontmatter alone never names a human."""
    from orch.core import ledger
    signed = ledger.entries(ws) if signed is None else signed
    gates = t.meta.get("gates") if isinstance(t.meta.get("gates"), dict) else {}
    out = {}
    for g in ("requirements", "plan"):
        state = ledger.gate_verification(ws, t, g, signed)
        if state == "none":
            continue
        if state == "verified":
            entry = next((e for e in signed if e.get("kind") == "gate" and e.get("ticket") == t.id
                          and e.get("gate") == g and e.get("hash") == (gates.get(g) or {}).get("hash")), None)
            out[g] = ("by your epic charter" if entry is None else
                      "from your phone" if str(entry.get("via") or "").startswith("phone:") else "you")
        else:
            out[g] = "by delegation" if state == "delegated" else None
    return out


def done_signer(ws, t, events, signed=None) -> str | None:
    """R26 for Done: "accepted" (a done verdict the signed ledger holds) or "closed" (a signed human close), as
    ledger.done_verification and `orch check` decide it; None when the ledger does not back the ticket's done."""
    from orch.core import ledger
    from orch.core.check import _closed_by_human
    if t.status != "done":
        return None
    closed = _closed_by_human(events, t.id)
    if ledger.done_verification(ws, t, closed=closed, signed=signed, events=events) != "verified":
        return None
    return "closed" if closed else "accepted"


def journey(t, card: dict, steps: list[dict], events, ask_by: dict, signers: dict | None = None,
            done_by: str | None = None) -> list[dict]:
    """M: the ticket's journey as five stages, Asked → Agreed → Doing → Proven → Done, each {name, state: done | now |
    todo, day, note}: a date ("DD.MM") and a short who/what summary, rule text only. Built from the five-step tracker
    (steps.steps: Requirements and Plan make Agreed), the gates, the status-change events and the card."""
    by = {s["name"]: s for s in steps}
    gates = t.meta.get("gates") if isinstance(t.meta.get("gates"), dict) else {}

    def state(*names) -> str:
        found = [by[n]["state"] for n in names if n in by]
        if any(s == "current" for s in found):
            return "now"
        return "done" if found and all(s in ("done", "skipped") for s in found) else "todo"

    asked_note = (f"from {card['external']['key']}" if card.get("external") else
                  "by you" if ask_by.get("by") == "you" else
                  f"by {ask_by['agent']}" if ask_by.get("by") == "agent" else "")
    approved = [g for g in ("requirements", "plan") if isinstance(gates.get(g), dict) and gates[g].get("approved")
                and card["gates"][g]["state"] == "approved"]
    agreed_day = _day(max((at for g in approved if (at := when(gates[g]["approved"])) is not None), default=None))
    signers = signers or {}
    unsigned = [g for g in approved if signers.get(g) is None]
    whos = sorted({signers[g] for g in approved if signers.get(g)})
    agreed_who = "approval not signed here" if unsigned else ", ".join(whos)
    agreed_note = " + ".join({"requirements": "req", "plan": "plan"}[g] for g in approved)
    skipped_all = all(by[n]["state"] == "skipped" for n in ("Requirements", "Plan") if n in by)  # #172
    agreed_note = (", ".join(x for x in (agreed_note, agreed_who) if x)
                   or ("skipped for this size" if skipped_all else "waits for your approval"))
    tasks = card.get("tasks")
    doing_bits = [f"tasks {tasks['closed']}/{tasks['total']}"] if tasks else []
    if card.get("code"):
        doing_bits.append(card["code"]["label"])
    ac = card.get("ac") or {}
    done = t.status == "done"
    work = state("Work")
    stages = [
        ("Asked", "done", day(t.meta.get("created")), asked_note),
        ("Agreed", state("Requirements", "Plan"), agreed_day, agreed_note),
        ("Doing", work, _day(_moved_to(events, "in-progress")),
         " · ".join(doing_bits) or ("under way" if work == "now" else "not started")),
        ("Proven", state("Testing"), _day(_moved_to(events, "testing")),
         f"AC {ac.get('proven', 0)}/{ac['total']}" if ac.get("total") else "no criteria"),
        ("Done", "done" if done else "todo", _day(_moved_to(events, "done")) if done else "",
         (done_by or "closed, not signed here") if done else "your verdict"),
    ]
    dropped = done and card.get("resolution")  # won't do, superseded, duplicate (cards._resolution)
    if dropped:  # only what really happened is ticked (an approval, a start, a testing round); Done says how it ended
        how = card["resolution_label"].lower() + (f" {card['superseded_by']}" if card.get("superseded_by") else "")
        reached = {"Asked": True, "Agreed": bool(approved), "Doing": _moved_to(events, "in-progress") is not None,
                   "Proven": _moved_to(events, "testing") is not None, "Done": True}
        stages = [(n, "done" if reached[n] else "todo", d,
                   ", ".join(x for x in (how, done_by) if x) if n == "Done" else note) for n, _, d, note in stages]
    elif done:  # every stage before Done is behind it
        stages = [(n, "done", d, note) for n, _, d, note in stages]
    turn = {"Agreed": ("Requirements", "Plan"), "Doing": ("Work",), "Proven": ("Testing",)}
    out = []
    for n, s, d, note in stages:
        flags = [by[x]["note"] for x in turn.get(n, ()) if x in by and by[x]["state"] == "current"
                 and by[x]["note"] in (YOUR_TURN, CHANGES_REQUESTED)]
        out.append({"name": n, "state": s, "day": d, "note": " · ".join([note] + flags[:1]),
                    "warn": (n == "Agreed" and bool(unsigned)) or (n == "Done" and done and not done_by),
                    # exactly one stage is current: the one in progress, or Done once the ticket is done
                    "current": s == "now" or (done and n == "Done")})
    return out


def _day(at) -> str:
    return at.astimezone().strftime("%d.%m") if at else ""

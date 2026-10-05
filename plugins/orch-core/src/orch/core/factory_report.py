"""AI Factory phase 3 (#2): the Ready report and the Stopped message of a factory epic.

Both are derived, read-only views. Nothing here writes, signs, approves, grants or starts anything, and nothing an
agent can write turns one on or hides it: they come from the epic's signed charter, the signed ledger entries (the
human's send-backs and denials), the budget markers beside the ledger and the tickets' own state.

- Ready: every child is in testing or done, at least one is in testing, and every criterion of every child in testing
  cites evidence. "In testing" and "done" are claims in files an agent can edit, so each must be backed by a record
  the agent cannot write (a done child: a signed verdict or close) or by what orch itself observed (a testing child:
  the move into testing by the session that claimed it, every task closed); a child without that backing is
  reported as not verifiable and Ready does not fire. The evidence itself is what the agents wrote: the report never
  calls it verified, the human reads it before signing. The human's one action on it is the epic verdict that already exists (`orch verdict <epic> done`, the
  epic page): it signs the epic's verdict hash, so the hash the report carries is the one `epics.verdict_hash` gives.
  The report itself accepts nothing and never closes a child.
- Stopped: a dead end the agents cannot get out of by themselves, with the reasons: the budget is used up, a child
  was sent back by the human FAILED_TRIES times, a request the human denied still holds a child back, or the ledger on
  this machine was cut, or (phase 6, a charter that signs a release) a sensitive path is touched, a release stage
  failed or its outcome is unknown. It is computed on its own, never suppressed by Ready (a forged Ready must not hide a real
  stop), and it keeps its other reasons under a cut ledger (entries that still verify can only add a reason). It
  says what the human can do; it offers no action of its own.

Only while `factory.enabled` is on (as the permission cards), judged by the signed charter alone.
"""
from __future__ import annotations

import re

from orch.core import epics, evidence, permits, store

FAILED_TRIES = 3  # signed send-backs of one child after which the factory counts as stuck on it
_EXCERPT = 400


def _text(s, limit: int = _EXCERPT) -> str:
    """Agent text as one plain line: characters outside printable ASCII escaped, never rendered as Markdown (the
    template's own escaping covers HTML)."""
    line = " ".join(str(s or "").split())[:limit]
    return "".join(c if 32 <= ord(c) < 127 else c.encode("unicode_escape").decode("ascii") for c in line)


def _full(s) -> str:
    """Agent text in full, line breaks kept, characters outside printable ASCII escaped: what the verdict binds, so
    never cut."""
    return "".join(c if c == "\n" or 32 <= ord(c) < 127 else c.encode("unicode_escape").decode("ascii")
                   for c in str(s or "").replace("\r\n", "\n").replace("\r", "\n"))


def _kids(ws, epic, entries):
    """[(entry, Ticket)] of the epic's children; None when one cannot be read (a report never guesses)."""
    out = []
    for e in epics.children(ws, epic.id, entries):
        try:
            out.append((e, store.read_ticket(e.path)))
        except Exception:
            return None
    return out


def finished(ws, t, signed, events) -> str | None:
    """"testing" or "done" when the record backs the status the ticket file claims, else None. Done: a signed done
    verdict or close that chains (ledger.done_verification). Testing: the newest move event of the ticket is the move
    into testing, by the session that holds the claim, and every task is closed."""
    from orch.core import ledger, tasks
    if t.status == "done":
        return "done" if any(ledger.done_verification(ws, t, closed=c, signed=signed, events=events) == "verified"
                             for c in (False, True)) else None
    if t.status != "testing":
        return None
    moves = [e for e in events if e.ticket == t.id and e.kind == "ticket.moved"]
    claim = t.meta.get("claim") if isinstance(t.meta.get("claim"), dict) else {}
    session = str(claim.get("session") or "")[:8]
    if not moves or moves[-1].data.get("to") != "testing" or not session:
        return None
    if not str(moves[-1].actor).startswith("agent:") or not str(moves[-1].actor).endswith(":" + session):
        return None
    try:
        s = tasks.summary(tasks.ticket_tasks(t))
    except Exception:
        return None
    return "testing" if s["total"] and s["closed"] == s["total"] else None


def _ready_state(ws, epic, entries, signed, events):
    """(report | None, [ids that claim testing or done without a record behind it])."""
    if not permits.enabled(ws) or epic.status != "open":
        return None, []
    from orch.core.gates import invalidated_gates
    d = permits.factory_delegation(ws, epic, signed)
    if d is None or d["epic_changed"]:
        return None, []
    kids = _kids(ws, epic, entries)
    if not kids or any(e.status not in ("testing", "done") for e, _ in kids):
        return None, []
    events = permits._events(ws, events)
    signed = permits._signed(ws, signed)
    forged = [t.id for _, t in kids if finished(ws, t, signed, events) is None]
    testing = [t for e, t in kids if e.status == "testing"]
    if forged or not testing or any(invalidated_gates(t) for t in testing):
        return None, forged
    from orch.core.artifacts import binding
    rows = []
    for e, t in kids:
        proven, total = evidence.progress(t)
        if e.status == "testing" and (total == 0 or proven != total):
            return None, []
        ac, ver = t.section("Acceptance criteria"), t.section("Verification")
        rows.append({"id": t.id, "title": _text(t.title), "status": e.status, "size": t.meta.get("size"),
                     "how": epics.STATE_LABELS[epics.child_state(ws, epic, t, signed, events, entries)],
                     "proven": proven, "total": total, "ac": _full(ac), "verification": _full(ver),
                     "bound": sorted(dict(binding(t, [ac, ver], ws, widgets=True)) if e.status == "testing" else []),
                     "findings": _text(t.section("Findings")),
                     "where": [_text(r) for r in (t.meta.get("repos") or []) if isinstance(r, str)][:5]})
    return {"epic": epic.id, "title": _text(epic.title), "children": rows, "open": len(testing),
            "seen": epics.verdict_hash(testing, ws), "proven": sum(r["proven"] for r in rows),
            "total": sum(r["total"] for r in rows), "coverage": _coverage(ws, epic, entries, signed)}, []


def ready(ws, epic, *, entries=None, signed=None, events=None) -> dict | None:
    """The Ready report of factory epic `epic`, or None while it is not ready (or not verifiably so). It carries the
    epic's `coverage` (what was asked against what the children's text mentions)."""
    return _ready_state(ws, epic, entries, signed, events)[0]


# -- what the epic asked for against what its children mention ----------------------------------------------------------
# Pure text, deterministic: the file names the epic's Requirements and Acceptance criteria name, and for each whether one
# child's Requirements or Acceptance criteria name it too. A mention is not a check that anything was built.
# Rules (docs/factory.md, "What was asked against what the children cover"):
# - a file name is a word ending in a known extension, or anything in backticks holding a `/` or such an extension;
#   version-like words (`v1.2.c`, `3.11.md`) and a bare `<library>.js` from a short list (node.js, vue.js, ...) are not;
# - a name on a line that negates before it ("do not build x.html", "without y.json", "no z.csv") is not a name there;
# - a child covers a name when it names the same path, or a longer path ending in it (`web/elephants.html` covers
#   `elephants.html`; a bare `elephants.json` does not cover `data/elephants.json`);
# - a child the human closed without it being built (a signed close) covers nothing.
_EXTS = ("html", "htm", "json", "csv", "tsv", "md", "txt", "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "css", "scss",
         "yaml", "yml", "toml", "xml", "svg", "png", "jpg", "jpeg", "gif", "webp", "pdf", "sh", "sql", "go", "rs", "rb",
         "java", "kt", "swift", "c", "h", "cpp", "hpp", "php", "ini", "cfg", "ipynb", "vue", "svelte", "lua", "r")
_BOUND = r"[\w./-]"
_END = r"(?![\w/-]|\.\w)"  # a sentence's full stop may follow a name
_FILE_RE = re.compile(rf"(?<!{_BOUND})(\w[\w./-]*\.(?:{'|'.join(_EXTS)})){_END}", re.I)
_TICK_RE = re.compile(r"`([^`\s]{1,200})`")
_VERSION = re.compile(r"v?\d+(?:\.\d+)+(?:\.[a-z]+)?", re.I)
_JS_LIBS = frozenset(("node", "vue", "next", "nuxt", "express", "chart", "three", "d3", "react", "angular", "ember",
                      "backbone", "socket.io", "moment", "lodash", "jquery", "alpine", "preact", "solid", "svelte",
                      "deno", "bun", "p5", "anime", "leaflet", "plotly", "highcharts"))
_NEGATION = re.compile(r"(?i)\b(?:not|never|no|without|except|excluding|instead of|don't|doesn't|won't|avoid)\b")
MAX_TOKENS = 50  # names shown on the card; every name counts


def _is_name(tok: str) -> bool:
    stem = tok.rsplit(".", 1)[0]
    if _VERSION.fullmatch(tok) or _VERSION.fullmatch(stem):
        return False
    return not (tok.endswith(".js") and "/" not in tok and stem in _JS_LIBS)


def named_files(text: str) -> list[str]:
    """The concrete file names in `text`, in order and each once (casefolded), leaving out a name on a line that
    negates before it (see the rules above). Never cut: every name counts."""
    out: list[str] = []
    for line in (text or "").splitlines():
        found = sorted([*_FILE_RE.finditer(line), *_TICK_RE.finditer(line)], key=lambda m: m.start())
        for m in found:
            tok = m.group(1).rstrip(".,;:").casefold()
            if (m.re is _TICK_RE and not ("/" in tok or _FILE_RE.fullmatch(tok))) or "://" in tok \
                    or tok.startswith("-") or not _is_name(tok) or _NEGATION.search(line[:m.start()]):
                continue
            if tok and tok not in out:
                out.append(tok)
    return out


def _covers(child_names: list[str], tok: str) -> bool:
    return any(c == tok or c.endswith("/" + tok) for c in child_names)


def _counts(ws, t, signed) -> bool:
    """Whether child `t` may cover anything: not closed by the human without being built (a signed close)."""
    return not (t.status == "done" and any(e.get("kind") == "close" and e.get("ticket") == t.id for e in signed))


def coverage(ws, epic, *, entries=None, signed=None) -> dict:
    """{asked: [the epic's acceptance criteria lines], files: [every named file name], shown: [the first MAX_TOKENS],
    more: how many more, covered: {name: [child ids]}, uncovered: [names no counting child names], children: [{id,
    title, files, counts}], readable: bool}. Text only, never a claim that anything was built; a child that cannot be
    read leaves `readable` False."""
    from orch.core import ledger
    signed = ledger.entries(ws) if signed is None else signed
    asked_text = f"{epic.section('Requirements')}\n{epic.section('Acceptance criteria')}"
    files = named_files(asked_text)
    asked = [_text(re.sub(r"^\s*[-*]\s*(\[[ xX]\]\s*)?", "", ln), 300)
             for ln in epic.section("Acceptance criteria").splitlines() if ln.strip()][:30]
    kids = _kids(ws, epic, entries)
    rows, covered = [], {f: [] for f in files}
    for _, t in kids or []:
        names = named_files(f"{t.section('Requirements')}\n{t.section('Acceptance criteria')}")
        counts = _counts(ws, t, signed)
        mine = [f for f in files if _covers(names, f)] if counts else []
        for f in mine:
            covered[f].append(t.id)
        rows.append({"id": t.id, "title": _text(t.title, 120), "files": mine[:MAX_TOKENS], "counts": counts})
    uncovered = [f for f in files if not covered[f]]
    return {"asked": asked, "files": files, "shown": files[:MAX_TOKENS], "more": max(0, len(files) - MAX_TOKENS),
            "covered": covered, "uncovered": uncovered, "uncovered_shown": uncovered[:MAX_TOKENS],
            "uncovered_more": max(0, len(uncovered) - MAX_TOKENS), "children": rows, "readable": kids is not None}


def coverage_ok(ws, epic, *, entries=None) -> bool | None:
    """True when the epic names at least one file and every one is named by a child that counts (and every child
    could be read); False when one is not, or anything cannot be read; None (unknown, never a pass for a gate) when
    the epic names no file. Named in text, not checked as built."""
    try:
        c = coverage(ws, epic, entries=entries)
    except Exception:
        return False
    if not c["readable"] or c["uncovered"]:
        return False
    return True if c["files"] else None


def _coverage(ws, epic, entries, signed) -> dict | None:
    """coverage() for the Ready report; an error is shown as unreadable, never as covered."""
    try:
        return coverage(ws, epic, entries=entries, signed=signed)
    except Exception:
        return {"asked": [], "files": [], "shown": [], "more": 0, "covered": {}, "uncovered": [],
                "uncovered_shown": [], "uncovered_more": 0, "children": [], "readable": False}


def _ledger_cut_epic(kids) -> bool:
    """A cut ledger loses the charter that says which epics are factories: an epic with a child whose gate names a
    delegation counts (only consulted while the ledger is cut)."""
    return any(isinstance(g, dict) and g.get("delegation")
               for _, t in kids for g in (t.meta.get("gates") or {}).values())


def _delegation(ws, epic, raw, cut):
    """The epic's factory delegation. Whole ledger: permits.factory_delegation. Cut ledger: built from the entries
    that still verify (they can only add a reason to stop, never lift one)."""
    if not cut:
        return permits.factory_delegation(ws, epic, raw)
    charter = next((e for e in reversed(raw) if e.get("kind") == "charter" and e.get("ticket") == epic.id), None)
    if not charter or not isinstance(charter.get("delegate"), dict) or not charter["delegate"].get("factory"):
        return None
    d = epics.normalize_delegate(charter["delegate"])
    return {"id": charter.get("delegation"), **d, "epic_changed": not epics._epic_current(epic, charter),
            "paused": any(e.get("kind") == "pause" and e.get("ticket") == epic.id
                          and e.get("delegation") == charter.get("delegation") for e in raw),
            "expired": epics.budget_used_up(charter, d), "at": charter.get("at")}


def stopped(ws, epic, *, entries=None, signed=None, events=None) -> list[dict]:
    """Why factory epic `epic` is at a dead end: [{code, label, text}], empty while it is not (and for a done epic).
    Independent of Ready."""
    if not permits.enabled(ws) or epic.status in ("backlog", "done"):
        return []
    from orch.core import ledger
    cut = not ledger.head_ok()
    raw = ledger.entries(ws) if signed is None or cut else signed
    kids = _kids(ws, epic, entries)
    d = _delegation(ws, epic, raw, cut)
    out = []
    if cut and d is None and kids and _ledger_cut_epic(kids):
        d = {}
    if d is None:
        return []
    if cut:
        out.append({"code": "ledger-cut", "label": "Ledger cut",
                    "text": "the approval ledger on this machine is shorter than its signed head, so no approval or "
                            "grant counts (`orch check` reports ledger-cut)"})
    events = permits._events(ws, events)
    reason = permits.budget_reason(ws, epic, d, events) if d else None
    if reason:
        out.append({"code": "budget", "label": "Budget used up", "text": reason})
    by_id = {e.id: (e, t) for e, t in kids or []}
    sent_back: dict[str, int] = {}
    for x in raw:
        if x.get("kind") == "verdict" and x.get("verdict") == "follow-up" and x.get("ticket") in by_id:
            sent_back[x["ticket"]] = sent_back.get(x["ticket"], 0) + 1
    for tid, n in sent_back.items():
        if n >= FAILED_TRIES and finished(ws, by_id[tid][1], raw, events) != "done":
            out.append({"code": "sent-back", "label": "Sent back repeatedly",
                        "text": f"{tid} was sent back {n} times and is not finished"})
    denied = {(e["request"], e.get("command_sha")): e for e in raw
              if e.get("kind") in ("grant", "permit_deny") and isinstance(e.get("request"), str)}
    for r in permits.requests(ws, events).values():
        if r["epic"] != epic.id or r["ticket"] not in by_id or finished(ws, by_id[r["ticket"]][1], raw, events):
            continue
        if denied.get((r["id"], r["sha"]), {}).get("kind") == "permit_deny" and not any(
                g.get("kind") == "grant" and g.get("epic") == epic.id and g.get("command_sha") == r["sha"]
                for g in raw):
            out.append({"code": "denied", "label": "Permission denied",
                        "text": f"you denied {r['id']} and {r['ticket']} has not finished since"})
    if d and d.get("release"):  # phase 6: a sensitive path, a failed stage, a stage whose outcome is unknown
        from orch.core import factory_release
        out += factory_release.reasons(ws, epic, d)
    return out


def _epics(ws, entries):
    for e in store.scan(ws) if entries is None else entries:
        if e.status != "done" and e.meta is not None and epics.is_epic(e.meta):
            try:
                yield store.read_ticket(e.path)
            except Exception:
                continue


def cards(ws, entries=None, events=None) -> dict:
    """{ready: [report], stopped: [{epic, title, reasons}], suspect: [{epic, title, children}]} over every factory
    epic (suspect: children that claim testing or done without a record behind it); all empty while the factory is
    off. Ready and Stopped are computed independently. Memoised per request scope."""
    if not permits.enabled(ws):
        return {"ready": [], "stopped": [], "suspect": []}

    def compute():
        from orch.core import ledger
        raw = ledger.entries(ws)
        out = {"ready": [], "stopped": [], "suspect": []}
        for epic in _epics(ws, entries):
            rep, forged = _ready_state(ws, epic, entries, raw, events)
            if rep:
                out["ready"].append(rep)
            if forged:
                out["suspect"].append({"epic": epic.id, "title": _text(epic.title), "children": forged})
            why = stopped(ws, epic, entries=entries, signed=raw, events=events)
            if why:
                out["stopped"].append({"epic": epic.id, "title": _text(epic.title), "reasons": why})
        return out
    return store.memo(ws, "factory-cards", compute)


def items(ws, entries=None, events=None) -> list[dict]:
    """The waiting-on-the-human items of query.waiting(): one per Ready report and one per Stopped epic."""
    c = cards(ws, entries, events)
    out = [{"ticket": r["epic"], "title": r["title"], "status": "open", "kind": "factory-ready",
            "detail": f"{r['open']} in testing", "scope": "blocking", "priority": None} for r in c["ready"]]
    out += [{"ticket": s["epic"], "title": s["title"], "status": "open", "kind": "factory-stopped",
             "detail": s["reasons"][0]["label"].lower(), "scope": "blocking", "priority": None} for s in c["stopped"]]
    return out


def signal(ws, epic) -> tuple[str, str] | None:
    """("stopped" if it has a reason to stop, else "ready", token) for factory epic `epic` itself, else None; the token
    names the state (the hash the verdict would bind and the reasons), so a waiter can tell a new state from one it already saw. Never raises."""
    import hashlib
    try:
        if not permits.enabled(ws):
            return None
        epic = store.read_ticket(store.resolve(ws, epic.id).path)
        from orch.core import ledger
        raw = ledger.entries(ws)
        rep, _ = _ready_state(ws, epic, None, raw, None)
        why = stopped(ws, epic, signed=raw)
        if not rep and not why:
            return None
        state = (rep["seen"] if rep else "") + "|".join(w["code"] + w["text"] for w in why)
        return ("stopped" if why else "ready"), hashlib.sha256(state.encode()).hexdigest()[:12]
    except Exception:
        return None
    return None

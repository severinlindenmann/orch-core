"""`orch migrate`: rewrite what older orch versions wrote into the current format, once.

Rules (each is independent, except that the tracker rule is all or nothing):

- proposal-decisions: a `## Proposal` or `## Decisions` section moves into `## Context` as a `###` subsection.
- plan-checklist: a ticket being worked (in progress, waiting, testing) whose Plan holds a `- [ ]` checklist and
  that has no tasks gets those items as `## Tasks`. The Plan stays as it is.
- artifact-paths: `../artifacts/<ticket>/<name>` link targets become `artifact:<name>` (this ticket) or
  `/a/<ticket>/<name>` (another ticket). The Log is history and is never rewritten.
- trackers: an `external_trackers` pattern that accepts a bare number (`\\d+`) becomes `<prefix>-(?P<id>\\d+)`, and
  the bare numbers in tickets' `external` keys get the prefix. A bare number that more than one tracker could own
  is refused, never guessed.

Human decisions stay valid. A rewrite never touches the ledger, the event log or the approved snapshots, and it
never changes text a decision is bound to: if a rule would change the text of an approved gate (Summary,
Requirements, Acceptance criteria, Out of scope, size, type, Plan) or the Verification of a ticket in testing or done,
that rule is refused for that ticket and reported. Only a human re-approving the new text can fix that; nothing is
signed here. Text nobody signed yet is rewritten freely.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from orch.core import fences, tasks as tk, trackers
from orch.core.constants import OLD_SECTIONS, STATUSES
from orch.core.fsutil import atomic_write_text
from orch.core.model import Ticket, parse_ticket, render_ticket, section_text_problem
from orch.errors import OrchError, TicketParseError

IMPORT_NOTE = "imported from the Plan checklist"
WORKED = ("in-progress", "waiting", "testing")  # a ticket in these has been claimed, so it has tasks now
_FILE = re.compile(r"^([A-Za-z][A-Za-z0-9]*-\d+)(?:-[^/\\]*)?\.md$")
_DEST = re.compile(r"((?:\]\(|^ {0,3}\[[^\]\n]+\]:)[ \t]*<?)(?:\.{1,2}/)*artifacts/([^/\s)>]+)/", re.M)
_PLAN_ITEM = re.compile(r"^[-*+]\s+\[([ xX])\]\s+(.*)$")
_CODE = re.compile(r"`([^`]+)`")
_BARE_SHAPES = ("\\d+", "[0-9]+", "(?P<id>\\d+)", "(?P<id>[0-9]+)")
_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


@dataclass
class Item:
    """One file the migration would rewrite."""
    path: Path
    rel: str
    rules: list[str]
    old: str
    new: str
    ticket: str | None = None  # the ticket id, whose lock a write takes
    notes: list[str] = field(default_factory=list)

    def diff(self) -> str:
        return "".join(difflib.unified_diff(self.old.splitlines(True), self.new.splitlines(True),
                                            f"a/{self.rel}", f"b/{self.rel}"))


@dataclass
class Plan:
    home: Path
    items: list[Item] = field(default_factory=list)
    refused: list[tuple[str, str, str]] = field(default_factory=list)  # (where, rule, why)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file, why) set by apply
    ws: object = None
    notes: list[tuple[str, str]] = field(default_factory=list)  # (file, what a human has to do)

    def empty(self) -> bool:
        return not (self.items or self.refused or self.unreadable)


# -- the text rules ---------------------------------------------------------------------------------------

def move_old_sections(t: Ticket) -> str | None:
    """Fold Proposal and Decisions into Context. A reason to refuse, or None (done)."""
    from orch.widgets.blocks import has_blocks
    found = [(n, t.section(n)) for n in OLD_SECTIONS if n in t.sections]
    parts = []
    for name, text in found:
        if text.strip():
            if has_blocks(text):
                return f"{name} holds an orch widget block; move it by hand (a block id must stay unique per ticket)"
            parts.append(f"### {name} (migrated)\n\n{text.strip()}")
    context = "\n\n".join(([t.section("Context")] if t.section("Context").strip() else []) + parts)
    if parts and (problem := section_text_problem(context)):
        return f"the merged Context would break the file ({problem})"
    for name, _ in found:
        del t.sections[name]
    if parts:
        t.set_section("Context", context)
    return None


def plan_items(text: str, repo_names) -> list[tk.Task]:
    """The top-level `- [ ]` / `- [x]` items of a Plan as tasks (T1…); indented lines join the text, a `repo/path`
    code span of a configured repo becomes a file ref."""
    items: list[list] = []
    for line in (text or "").split("\n"):
        if not line.strip():
            continue
        m = None if line[:1].isspace() else _PLAN_ITEM.match(line.strip())
        if m:
            items.append([m.group(1) != " ", m.group(2).strip()])
        elif items and line[:1].isspace():
            items[-1][1] += " " + line.strip().lstrip("-*+ ").strip()
    out = []
    for n, (checked, body) in enumerate(items, 1):
        refs = []
        for code in _CODE.findall(body):
            if "/" in code and re.split(r"[/\\]", code, maxsplit=1)[0] in set(repo_names):
                try:
                    refs.append(tk.parse_ref(f"file:{code}"))
                except ValueError:
                    pass
        out.append(tk.Task(id=f"T{n}", state="done" if checked else "todo", text=tk.one_line(body), refs=refs))
    return out


def import_plan(t: Ticket, repo_names, used) -> str | None:
    """Write the Plan's checklist as the ticket's tasks. A reason to refuse or skip, or None (done or nothing to do)."""
    if t.status not in WORKED or t.section("Tasks").strip():
        return None
    new = plan_items(t.section("Plan"), repo_names)
    if not new:
        return None
    if t.status == "testing" and any(x.state != "done" for x in new):
        return "the ticket is in testing and the Plan checklist has open items; tick or drop them, then move it back"
    start = tk.next_number([], used())
    for i, task in enumerate(new):
        task.id = f"T{start + i}"
        task.note = IMPORT_NOTE
    t.set_section("Tasks", tk.render(new))
    return None


def artifact_paths(text: str, ticket_id: str) -> str:
    """`text` with old relative artifact link targets in the current form. Fenced code stays as it is."""
    def swap(m):
        owner = m.group(2)
        return m.group(1) + ("artifact:" if owner.lower() == ticket_id.lower() else f"/a/{owner}/")

    out, fence = [], None
    for line in text.split("\n"):
        was = fence
        fence, is_fence = fences.step(fence, line)
        out.append(line if was or is_fence else _DEST.sub(swap, line))
    return "\n".join(out)


# -- what a human decision is bound to --------------------------------------------------------------------

def bound_text(t: Ticket, ws) -> dict[str, object]:
    """What an approval or verdict of this ticket covers, per decision: gate_parts and gate_meta of each gate (what
    the gate hash is made of), and the verdict hash itself (Verification, criteria, inline artifacts, widget pins)."""
    from orch.core import epics
    from orch.core.gates import GATE_SECTIONS, gate_meta, gate_parts
    out = {g: (tuple(gate_parts(t, g)), tuple(gate_meta(t, g))) for g in GATE_SECTIONS}
    try:
        out["verdict"] = epics.verdict_hash([t], ws)
    except Exception:  # a widget that cannot be pinned: compare the text it is made of
        out["verdict"] = (t.section("Verification"), t.section("Acceptance criteria"))
    return out


def signed(t: Ticket, decision: str, epics_with_verdict=frozenset()) -> bool:
    """True when a decision that still holds may cover this ticket's current text. An approval whose hash no longer
    matches (gate state invalidated) is void already, and rewriting cannot void it further."""
    from orch.core.gates import gate_state
    gates = t.meta.get("gates") or {}
    if decision == "verdict":
        return (t.status in ("testing", "done") or bool((gates.get("verify") or {}).get("verdict"))
                or str(t.meta.get("parent") or "") in epics_with_verdict)
    g = gates.get(decision) or {}
    return gate_state(t, decision) == "approved" or bool(g.get("changes_requested"))


def changed_decisions(before: Ticket, after: Ticket, ws, epics_with_verdict=frozenset()) -> list[str]:
    old, new = bound_text(before, ws), bound_text(after, ws)
    return [d for d in old if old[d] != new[d] and signed(before, d, epics_with_verdict)]


def voided(t: Ticket) -> list[str]:
    """Gates whose approval no longer matches the text while the gated text has old artifact links: what the upgrade
    voided (old links stopped binding their images)."""
    from orch.core.gates import GATE_SECTIONS, gate_parts, gate_state
    return [g for g in GATE_SECTIONS if gate_state(t, g) == "invalidated"
            and any(artifact_paths(body, t.id) != body for _, body in gate_parts(t, g))]


# -- tickets ------------------------------------------------------------------------------------------------

def _ticket_files(home: Path):
    for status in STATUSES:
        d = home / "tickets" / status
        if d.is_dir():
            yield from sorted(p for p in d.iterdir() if _FILE.match(p.name) and p.is_file())


def _copy(t: Ticket) -> Ticket:
    from orch.core.store import _copy_ticket
    return _copy_ticket(t)


def migrate_ticket(t: Ticket, repo_names, used, ws, epics_with_verdict=frozenset()) -> tuple[Ticket, list[str], list[tuple[str, str]]]:
    """(the migrated ticket, the rules that changed it, [(rule, why refused)])."""
    cur, rules, refused = _copy(t), [], []

    def attempt(rule, edit):
        nonlocal cur
        trial = _copy(cur)
        why = edit(trial)
        if why is None and (trial.sections != cur.sections or trial.meta != cur.meta):
            if hit := changed_decisions(cur, trial, ws, epics_with_verdict):
                why = (f"it would change text a human decision is bound to ({', '.join(hit)}); "
                       "that needs a human re-approval, so it is left as it is")
            else:
                cur = trial
                rules.append(rule)
        if why:
            refused.append((rule, why))

    def paths(x):
        for name in list(x.sections):
            if name != "Log":
                x.sections[name] = artifact_paths(x.sections[name], x.id)

    attempt("proposal-decisions", move_old_sections)
    attempt("plan-checklist", lambda x: import_plan(x, repo_names, used))
    attempt("artifact-paths", paths)
    return cur, rules, refused


# -- trackers -----------------------------------------------------------------------------------------------

def _migrated_shape(tracker: dict) -> bool:
    """A tracker in the form this migration writes (`<prefix>-(?P<id>\\d+)`): the config of an earlier run."""
    pre = str(tracker.get("prefix") or "")
    return bool(_PREFIX.match(pre)) and tracker.get("pattern") == f"{pre}-(?P<id>\\d+)"


def migrate_trackers(cfg: dict, tickets: list[Ticket]) -> tuple[list | None, dict[str, list[dict]], str | None]:
    """(the new config's external_trackers or None, {ticket id: new external list}, a reason to refuse). Bare numbers
    in tickets go to the trackers just migrated; with none in this config, to the one tracker already in the migrated
    shape (an earlier run, or the same edit by hand). Anything else is left alone, and an ambiguous owner is refused."""
    old = [dict(x) if isinstance(x, dict) else x for x in cfg.get("external_trackers") or []]
    bare = [i for i, x in enumerate(old) if isinstance(x, dict) and isinstance(x.get("pattern"), str)
            and trackers.accepts_bare_number(x["pattern"])]
    new = [dict(x) if isinstance(x, dict) else x for x in old]
    for i in bare:
        x, pre = new[i], str(new[i].get("prefix") or "")
        if x["pattern"] not in _BARE_SHAPES:
            return None, {}, f"tracker {pre or i} has the pattern {x['pattern']!r}, which accepts a bare number; rewrite it by hand"
        if not _PREFIX.match(pre):
            return None, {}, f"tracker {i} has a bare-number pattern and no usable prefix to put in front of it; set \"prefix\" first"
        x["pattern"] = f"{pre}-(?P<id>\\d+)"
        if isinstance(x.get("url"), str):
            x["url"] = x["url"].replace("{key}", "{id}")
    live = [x for x in new if isinstance(x, dict) and isinstance(x.get("pattern"), str)]
    owners = [new[i] for i in bare] or [x for x in live if _migrated_shape(x)]
    keys: dict[str, list[dict]] = {}
    for t in tickets:
        have = {str(x.get("key", "")).upper() for x in t.meta.get("external") or [] if isinstance(x, dict)}
        entries, changed = [], False
        for x in t.meta.get("external") or []:
            key = str(x.get("key", "")).strip() if isinstance(x, dict) else ""
            if not key.isdigit() or trackers.find(live, key):
                entries.append(x)
                continue
            fit = [o for o in owners if trackers.matches(o["pattern"], f"{o['prefix']}-{key}")]
            if not fit:
                entries.append(x)
                continue
            if len(fit) > 1:
                return None, {}, (f"{t.id}: the external key {key} could belong to "
                                  f"{' or '.join(str(o['prefix']) for o in fit)}; pick one by hand")
            ref = trackers.external_ref(live, f"{fit[0]['prefix']}-{key}")
            changed = True
            if ref["key"] in have or any(e.get("key") == ref["key"] for e in entries):
                continue  # the same link is already there: the bare number is a duplicate of it
            entries.append({**x, **ref})
        if changed:
            keys[t.id] = entries
    if new == old and not keys:
        return None, {}, None
    return new, keys, None


# -- the whole workspace ------------------------------------------------------------------------------------

def _workspace(home: Path, cfg: dict):
    from orch.config.load import DEFAULTS, deep_merge
    from orch.core.workspace import Workspace
    return Workspace(home=home, config=deep_merge(DEFAULTS, cfg))  # no layout changes, no validation: reads only


def plan(home: Path) -> Plan:
    """What migrating the workspace at `home` (the orchestrator folder) would change. Reads only."""
    from orch.core.ops_tasks import used_numbers
    home = Path(home)
    cfg_path = home / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    ws = _workspace(home, cfg)
    result = Plan(home, ws=ws)
    repo_names = list((cfg.get("git") or {}).get("repos") or {})
    loaded: list[tuple[Path, Ticket]] = []
    for path in _ticket_files(home):
        rel = path.relative_to(home).as_posix()
        try:
            loaded.append((path, parse_ticket(path.read_text(encoding="utf-8"), rel, allow_old=True)))
        except (TicketParseError, UnicodeDecodeError, OSError) as e:
            result.unreadable.append((rel, e.message if isinstance(e, OrchError) else str(e)))
    new_trackers, new_external, why = migrate_trackers(cfg, [t for _, t in loaded])
    if why:
        result.refused.append(("config.json", "trackers", why))
    verdicts = frozenset(t.id for _, t in loaded if ((t.meta.get("gates") or {}).get("verify") or {}).get("verdict"))
    for path, t in loaded:
        rel = path.relative_to(home).as_posix()
        cur, rules, refused = migrate_ticket(t, repo_names, lambda: used_numbers(ws, t.id), ws, verdicts)
        result.refused += [(rel, rule, why) for rule, why in refused]
        if t.id in new_external:
            cur.meta["external"] = new_external[t.id]
            rules.append("trackers")
        if not rules:
            continue
        old = path.read_text(encoding="utf-8")
        if render_ticket(cur) != render_ticket(t):  # dropping empty old headings alone is not an edit
            cur.meta["updated"] = _stamp()
        notes = [f"the {g} approval was voided by the upgrade (old artifact links no longer bind their images); "
                 "migrate, then re-approve" for g in voided(t)] if "artifact-paths" in rules else []
        result.items.append(Item(path, rel, rules, old, render_ticket(cur), t.id, notes))
    if new_trackers is not None:
        old_cfg = cfg_path.read_text(encoding="utf-8-sig")
        new_cfg = json.dumps({**cfg, "external_trackers": new_trackers}, indent=2, ensure_ascii=False) + "\n"
        result.items.append(Item(cfg_path, "config.json", ["trackers"], old_cfg, new_cfg))
    for path, t in loaded:  # an approval already void whose gated text has old links, which stays as it is
        if not any(i.ticket == t.id for i in result.items) and (g := voided(t)):
            result.notes.append((path.relative_to(home).as_posix(),
                                 f"the {', '.join(g)} approval was voided by the upgrade; fix the links, then re-approve"))
    return result


def apply(result: Plan) -> int:
    """Write every item, the number written. Each file is re-read under its ticket's lock (the one orch's own writes
    take) and skipped, with a reason in `result.skipped`, if it is no longer what the plan was made from. Tickets
    first, the config last; the config is left out when a ticket that needs it was skipped."""
    from orch.core.locks import lock
    written, ticket_skipped = 0, False
    for item in sorted(result.items, key=lambda i: i.rel == "config.json"):
        if item.ticket is None and ticket_skipped:
            result.skipped.append((item.rel, "a ticket that needs the new tracker keys was skipped; run orch migrate again"))
            continue
        with lock(result.ws, item.ticket or "config"):
            enc = "utf-8-sig" if item.ticket is None else "utf-8"
            now = item.path.read_text(encoding=enc) if item.path.exists() else None
            if now != item.old:
                result.skipped.append((item.rel, "changed since the plan was made; run orch migrate again"))
                ticket_skipped = ticket_skipped or "trackers" in item.rules
                continue
            atomic_write_text(item.path, item.new)
            written += 1
    return written


def _stamp() -> str:
    from orch.clock import stamp
    return stamp()

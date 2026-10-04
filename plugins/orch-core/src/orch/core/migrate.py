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

    def diff(self) -> str:
        return "".join(difflib.unified_diff(self.old.splitlines(True), self.new.splitlines(True),
                                            f"a/{self.rel}", f"b/{self.rel}"))


@dataclass
class Plan:
    home: Path
    items: list[Item] = field(default_factory=list)
    refused: list[tuple[str, str, str]] = field(default_factory=list)  # (where, rule, why)
    unreadable: list[tuple[str, str]] = field(default_factory=list)

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


def import_plan(t: Ticket, repo_names, used: set[int]) -> str | None:
    """Write the Plan's checklist as the ticket's tasks. A reason to refuse or skip, or None (done or nothing to do)."""
    if t.status not in WORKED or t.section("Tasks").strip():
        return None
    new = plan_items(t.section("Plan"), repo_names)
    if not new:
        return None
    if t.status == "testing" and any(x.state != "done" for x in new):
        return "the ticket is in testing and the Plan checklist has open items; tick or drop them, then move it back"
    start = tk.next_number([], used)
    for i, task in enumerate(new):
        task.id = f"T{start + i}"
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

def bound_text(t: Ticket) -> dict[str, tuple]:
    """What an approval or verdict of this ticket covers, per decision: gate_parts and gate_meta of each gate (what
    the gate hash is made of), and the text a verdict reads."""
    from orch.core.gates import GATE_SECTIONS, gate_meta, gate_parts
    out = {g: (tuple(gate_parts(t, g)), tuple(gate_meta(t, g))) for g in GATE_SECTIONS}
    out["verdict"] = (t.section("Verification"), t.section("Acceptance criteria"))
    return out


def signed(t: Ticket, decision: str) -> bool:
    """True when somebody may have signed `decision` for this ticket's current text."""
    gates = t.meta.get("gates") or {}
    if decision == "verdict":
        return t.status in ("testing", "done") or bool((gates.get("verify") or {}).get("verdict"))
    g = gates.get(decision) or {}
    return bool(g.get("approved") or g.get("changes_requested"))


def changed_decisions(before: Ticket, after: Ticket) -> list[str]:
    old, new = bound_text(before), bound_text(after)
    return [d for d in old if old[d] != new[d] and signed(before, d)]


# -- tickets ------------------------------------------------------------------------------------------------

def _read(path: Path) -> Ticket:
    return parse_ticket(path.read_text(encoding="utf-8"), path.name, allow_old=True)


def _ticket_files(home: Path):
    for status in STATUSES:
        d = home / "tickets" / status
        if d.is_dir():
            yield from sorted(p for p in d.iterdir() if _FILE.match(p.name) and p.is_file())


def _copy(t: Ticket) -> Ticket:
    from orch.core.store import _copy_ticket
    return _copy_ticket(t)


def migrate_ticket(t: Ticket, repo_names, used: set[int]) -> tuple[Ticket, list[str], list[tuple[str, str]]]:
    """(the migrated ticket, the rules that changed it, [(rule, why refused)])."""
    cur, rules, refused = _copy(t), [], []

    def attempt(rule, edit):
        nonlocal cur
        trial = _copy(cur)
        why = edit(trial)
        if why is None and (trial.sections != cur.sections or trial.meta != cur.meta):
            if hit := changed_decisions(cur, trial):
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

def migrate_trackers(cfg: dict, tickets: list[Ticket]) -> tuple[dict | None, dict[str, list[dict]], str | None]:
    """(the new config's external_trackers or None, {ticket id: new external list}, a reason to refuse)."""
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
    owners = [new[i] for i in bare] or live
    keys: dict[str, list[dict]] = {}
    for t in tickets:
        entries, changed = [], False
        for x in t.meta.get("external") or []:
            key = str(x.get("key", "")).strip() if isinstance(x, dict) else ""
            if not key.isdigit() or trackers.find(live, key):
                entries.append(x)
                continue
            fit = [o for o in owners if _PREFIX.match(str(o.get("prefix") or ""))
                   and trackers.matches(o["pattern"], f"{o['prefix']}-{key}")]
            if not fit:
                entries.append(x)
                continue
            if len(fit) > 1:
                return None, {}, (f"{t.id}: the external key {key} could belong to "
                                  f"{' or '.join(str(o['prefix']) for o in fit)}; pick one by hand")
            ref = trackers.external_ref(live, f"{fit[0]['prefix']}-{key}")
            if any(isinstance(e, dict) and str(e.get("key", "")).upper() == ref["key"] for e in t.meta["external"]):
                return None, {}, f"{t.id} already links {ref['key']}; remove the bare {key} by hand"
            entries.append({**x, **ref})
            changed = True
        if changed:
            keys[t.id] = entries
    if new == old and not keys:
        return None, {}, None
    return new, keys, None


# -- the whole workspace ------------------------------------------------------------------------------------

def plan(home: Path) -> Plan:
    """What migrating the workspace at `home` (the orchestrator folder) would change. Reads only."""
    from orch.core.ops_tasks import used_numbers
    home = Path(home)
    result = Plan(home)
    cfg_path = home / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    ws = _Light(home, cfg)
    repo_names = list((cfg.get("git") or {}).get("repos") or {})
    loaded: list[tuple[Path, Ticket, str]] = []
    for path in _ticket_files(home):
        rel = path.relative_to(home).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
            loaded.append((path, parse_ticket(text, rel, allow_old=True), text))
        except (TicketParseError, UnicodeDecodeError, OSError) as e:
            result.unreadable.append((rel, e.message if isinstance(e, OrchError) else str(e)))
    new_trackers, new_external, why = migrate_trackers(cfg, [t for _, t, _ in loaded])
    if why:
        result.refused.append(("config.json", "trackers", why))
    pending: list[tuple[Path, Ticket, Ticket, list[str]]] = []
    for path, t, text in loaded:
        rel = path.relative_to(home).as_posix()
        cur, rules, refused = migrate_ticket(t, repo_names, used_numbers(ws, t.id))
        result.refused += [(rel, rule, why) for rule, why in refused]
        if t.id in new_external:
            cur.meta["external"] = new_external[t.id]
            rules.append("trackers")
        if rules:
            pending.append((path, t, cur, rules))
    for path, t, cur, rules in pending:
        old = path.read_text(encoding="utf-8")
        cur.meta["updated"] = _stamp()
        result.items.append(Item(path, path.relative_to(home).as_posix(), rules, old, render_ticket(cur)))
    if new_trackers is not None:
        old_cfg = cfg_path.read_text(encoding="utf-8-sig")
        new_cfg = json.dumps({**cfg, "external_trackers": new_trackers}, indent=2, ensure_ascii=False) + "\n"
        result.items.append(Item(cfg_path, "config.json", ["trackers"], old_cfg, new_cfg))
    return result


def apply(result: Plan) -> int:
    """Write every item; the number written. Tickets first, the config last, so a stop in between leaves tickets
    that a second run finishes (the second run sees the config still waiting)."""
    for item in sorted(result.items, key=lambda i: i.rel == "config.json"):
        atomic_write_text(item.path, item.new)
    return len(result.items)


def _stamp() -> str:
    from orch.clock import stamp
    return stamp()


@dataclass
class _Light:
    """Just enough of a Workspace to read the event log (used_numbers): no config validation, no layout changes."""
    home: Path
    config: dict

    @property
    def state_dir(self) -> Path:
        return self.home / ".state"


def pending_paths(home: Path) -> list[tuple[str, str]]:
    """(ticket file, rule) for what `orch check` reports as needing `orch migrate`: old artifact paths (the other
    old shapes fail to load, or are config problems, and say so themselves)."""
    out = []
    for path in _ticket_files(Path(home)):
        try:
            t = _read(path)
        except (TicketParseError, UnicodeDecodeError, OSError):
            continue
        if any(artifact_paths(text, t.id) != text for name, text in t.sections.items() if name != "Log"):
            out.append((path.relative_to(home).as_posix(), "artifact-paths"))
    return out

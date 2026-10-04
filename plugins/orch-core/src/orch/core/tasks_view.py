"""The task list as data (format orch.tasks.v1): what `orch task list --json`, `orch show --json`
(key "tasks") and the dashboard read. Agents never parse the Markdown; see docs/tasks-format.md."""
from __future__ import annotations

import re

from orch.core import store
from orch.core import tasks as tk
from orch.core.constants import FILE_ORDER
from orch.errors import OrchError, UsageError

_AC = re.compile(r"^[-*+]\s+\[[ xX]\]\s+(.*)$")


def acceptance_criteria(ticket) -> list[str]:
    out = []
    for line in ticket.section("Acceptance criteria").split("\n"):
        m = None if line[:1].isspace() else _AC.match(line.strip())
        if m:
            out.append(m.group(1).strip())
    return out


def _repo_of(ws, path: str):
    best = None
    for name, spec in (ws.config["git"].get("repos") or {}).items():
        prefix = str((spec or {}).get("path") or name).strip("/")
        if (path == prefix or path.startswith(prefix + "/")) and (best is None or len(prefix) > len(best[1])):
            best = (name, prefix)
    return (None, path) if best is None else (best[0], path[len(best[1]):].lstrip("/"))


def _exists(base, rel: str) -> bool:
    """A literal path only: glob refs are a parse error (tasks.parse_ref), and no pattern is expanded."""
    try:
        return (base / rel).exists()
    except (OSError, ValueError):
        return False


def _question(ticket, qid: str):
    return next((q for q in ticket.meta.get("questions") or []
                 if isinstance(q, dict) and str(q.get("id", "")).upper() == qid.upper()), None)


def _answered(q) -> bool:
    return bool(q) and q.get("answer") not in (None, "", [])


def _tracker_url(ws, key: str):
    for tr in ws.config.get("external_trackers") or []:
        if re.fullmatch(tr["pattern"], key, re.IGNORECASE):
            return tr["url"].replace("{key}", key.upper())
    return None


def resolve_ref(ws, ticket, ref, entries=None) -> dict:
    out = {"kind": ref.kind, "target": ref.target, "label": ref.label, "exists": True}
    kind, target = ref.kind, ref.target
    if kind in ("file", "static"):
        m = tk.LINES.match(target)
        path, lines = m.group(1), (f"{m.group(2)}-{m.group(3)}" if m.group(3) else m.group(2))
        if kind == "file":
            repo, rel = _repo_of(ws, path)
            out.update(repo=repo, path=rel, lines=lines, exists=_exists(ws.root, path))
        else:
            out.update(path=path, lines=lines, exists=_exists(ws.static_dir, path))
    elif kind == "artifact":
        out.update(url=f"/a/{ticket.id}/{target}", exists=(ws.artifacts_dir / ticket.id / target).is_file())
    elif kind == "ticket":
        try:
            e = store.resolve(ws, target, entries)
            out.update(resolved=e.id, status=e.status, title=(e.meta or {}).get("title"))
        except UsageError:
            out.update(resolved=None, status=None, title=None, exists=False)
    elif kind == "ext":
        out["url"] = _tracker_url(ws, target)
    elif kind == "url":
        out["url"] = target
    elif kind == "section":
        names = {s.lower(): s for s in list(FILE_ORDER) + list(ticket.sections)}
        out.update(target=names.get(target.lower(), target), exists=target.lower() in names)
    elif kind == "ac":
        acs, n = acceptance_criteria(ticket), int(target)
        out.update(text=acs[n - 1] if n <= len(acs) else None, exists=n <= len(acs))
    elif kind == "q":
        q = _question(ticket, target)
        out.update(text=q.get("text") if q else None, answered=_answered(q), exists=q is not None)
    return out


def _on(ws, ticket, items, on, entries) -> dict | None:
    if not on:
        return None
    kind = tk.on_kind(on)
    if kind == "question":
        q = _question(ticket, on)
        return {"kind": "question", "id": on, "exists": q is not None, "answered": _answered(q)}
    if kind == "task":
        other = next((x for x in items if x.id == on), None)
        return {"kind": "task", "id": on, "exists": other is not None,
                "state": other.state if other else None, "owner": other.owner if other else None}
    try:
        e = store.resolve(ws, on, entries)
        return {"kind": "ticket", "id": e.id, "exists": True, "status": e.status, "title": (e.meta or {}).get("title")}
    except UsageError:
        url = _tracker_url(ws, on)
        return {"kind": "ext", "id": on, "exists": url is not None, "url": url}


def _bare_ref(ref) -> dict:
    return {"kind": ref.kind, "target": ref.target, "label": ref.label, "exists": None}


def _task(ws, ticket, t, items, entries, resolve_refs=True) -> dict:
    on_ref = _on(ws, ticket, items, t.on, entries)
    waits_on_you = t.state == "blocked" and bool(on_ref) and (
        (on_ref["kind"] == "question" and on_ref["exists"] and not on_ref["answered"])
        or (on_ref["kind"] == "task" and on_ref["owner"] == "human" and on_ref["state"] in tk.OPEN_STATES))
    return {"id": t.id, "state": t.state, "text": t.text, "owner": t.owner,
            "needs": list(t.needs), "needs_open": tk.needs_open(t, items),
            "refs": [resolve_ref(ws, ticket, r, entries) if resolve_refs else _bare_ref(r) for r in t.refs],
            "verify": t.verify, "why": t.why, "on": t.on, "on_ref": on_ref, "note": t.note,
            "added": t.added.removesuffix(" " + tk.AFTER_APPROVAL) if t.added else None,
            "added_after_approval": t.added_after_approval, "waits_on_you": waits_on_you}


def _can_move(ws, ticket) -> bool:
    from orch.core.events import Actor
    from orch.core.lifecycle import check_move
    if ticket.status != "in-progress":
        return False
    try:
        check_move(ticket, "testing", Actor("agent", "orch", "cli"),
                   plan_skip_sizes=tuple(ws.config["gates"]["plan_skip_sizes"]))
    except OrchError:
        return False
    return True


def view(ws, ticket, entries=None, *, resolve_refs: bool = True) -> dict:
    """The task-list view. With resolve_refs=False (dashboard summaries such as Today's In flight)
    refs are {kind, target, label, exists: None}: no file probes, ticket lookups or tracker URLs."""
    plan = (ticket.meta.get("gates") or {}).get("plan") or {}
    out = {"format": tk.FORMAT, "ticket": ticket.id, "status": ticket.status, "error": None,
           "summary": tk.summary([]), "doing": None, "next": None, "open": [],
           "can_move_to_testing": False, "plan_approved": plan.get("approved"),
           "added_since_approval": 0, "tasks": []}
    try:
        items = tk.ticket_tasks(ticket)
    except tk.TaskParseError as e:
        return {**out, "error": e.message, "line": e.line}
    doing, nxt = tk.doing(items), tk.next_task(items)
    return {**out, "summary": tk.summary(items), "doing": doing.id if doing else None,
            "next": nxt.id if nxt else None, "open": tk.open_ids(items),
            "can_move_to_testing": _can_move(ws, ticket),
            "added_since_approval": sum(t.added_after_approval for t in items),
            "tasks": [_task(ws, ticket, t, items, entries, resolve_refs) for t in items]}

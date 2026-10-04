"""AI Factory phase 4 (#2, #31): the runner. One `tick` keeps agent sessions going for the children of the factory
epics the human started, and stops them when the factory must stop. It runs in the dashboard server the human started,
and refuses any process with an agent harness in its ancestry.

It never approves, grants, signs or starts anything by itself. It works only while the factory is switched on, the
epic's signed charter is a factory one and still active (not paused, the epic text unchanged, the time budget not
used up, the epic not done), the ledger is whole, and the human armed that delegation by starting it from the
dashboard (orch.core.factory_sessions). The limits are the signed charter's, plus a concurrency cap (3, which the
workspace config may only lower with `factory.max_concurrency`).

- Launch: one session per eligible child (auto-approved or covered by the human's charter, size within the limit,
  open, in progress or waiting), at most `max_children` children per delegation and a few launches per child, both
  counted in markers beside the ledger. The runner generates the session id, writes the binding the permission hook
  trusts, and only then starts the agent.
- Wake: a child whose session ended is started again when something it waits for changed (a grant, a denial, a
  revocation, its own text or status), never otherwise, within the launch cap.
- Stop: when the epic is paused, edited, done, approved again, out of time, the ledger is cut, the factory is off, or
  the child is done. A stopped session loses its binding at once, whether or not tmux obeys.

The launcher (list, start, stop) is a parameter, so tests never start a real agent.
"""
from __future__ import annotations

import hashlib
import json
from typing import Protocol

from orch.core import epics, factory_sessions as fs, ledger, permits, store
from orch.errors import OrchError

DEFAULT_CONCURRENCY = 3
RUNNABLE = ("open", "in-progress", "waiting")


class Launcher(Protocol):
    def alive(self) -> set[str]: ...
    def start(self, name: str, cwd: str, argv: list[str]) -> None: ...
    def stop(self, name: str) -> None: ...


def concurrency(ws) -> int:
    """The cap: 3, or lower when the workspace config says so (a config value can only restrict)."""
    v = (ws.config.get("factory") or {}).get("max_concurrency")
    ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
    return min(DEFAULT_CONCURRENCY, v) if ok else DEFAULT_CONCURRENCY


def wake_token(epic, child, signed) -> str:
    """What a parked child waits for: the human's answers in this epic (grants, denials, revocations) and what its
    approvals bind (its text and gates). Not its status: the agent moves that itself."""
    mine = [e for e in signed if e.get("ticket") == epic.id]
    gates = {k: bool((v or {}).get("approved")) for k, v in sorted((child.meta.get("gates") or {}).items())}
    body = [epics.child_hashes(child), gates, sorted(str(e.get("grant")) for e in mine if e.get("kind") == "grant"),
            sum(1 for e in mine if e.get("kind") == "permit_deny"),
            sum(1 for e in mine if e.get("kind") == "permit_revoke")]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _ticket(ws, ref):
    try:
        return store.read_ticket(store.resolve(ws, ref).path)
    except Exception:
        return None


def stop_reason(ws, b: dict, signed, cut: bool) -> str | None:
    """Why the session bound by `b` must stop now, or None."""
    if not permits.enabled(ws):
        return "the factory is switched off"
    if cut:
        return "the approval ledger on this machine was cut"
    epic = _ticket(ws, b["epic"])
    if epic is None:
        return "its epic is gone"
    if epic.status == "done":
        return "the epic is done"
    d = permits.factory_delegation(ws, epic, signed)
    if d is None or d["id"] != b["delegation"]:
        return "the epic was approved again or is no longer a factory epic"
    if not fs.armed(ws, d["id"]):
        return "the human has not started it from the dashboard"
    if d["paused"]:
        return "the delegation is paused"
    if d["epic_changed"]:
        return "the epic text changed"
    if d["expired"]:
        return "the time budget is used up"
    child = _ticket(ws, b["child"])
    if child is None:
        return "its child is gone"
    if child.status == "done":
        return "the child is done"
    return None


def _launchable(ws, epic, d, t, signed) -> bool:
    return (t.status in RUNNABLE and not epics.is_epic(t) and epics.within_limits(t, d) is None
            and not epics.hidden_in(t) and epics.child_state(ws, epic, t, signed) in ("delegated", "covered"))


def _launch(ws, actor, launcher, settings, epic, d, t, token, lines) -> dict | None:
    from orch.dashboard.data import agent_start
    sid = fs.new_session_id()
    name = f"fx-{t.id}-{sid[:8]}"
    prompt = agent_start.build(ws, t.id, "work", "claude", settings=settings)[0]
    argv = [a.replace("{session}", sid).replace("{prompt}", prompt) for a in settings["factory_command"]]
    b = fs.bind(ws, actor, session=sid, epic=epic.id, delegation=d["id"], child=t.id, name=name, wake=token)
    try:
        launcher.start(name, str(ws.root), argv)
    except (OrchError, OSError) as e:
        fs.end(ws, sid)
        lines.append(f"could not start {name}: {e}")
        return None
    lines.append(f"started {name}")
    return b


def tick(ws, actor, launcher: Launcher, *, settings: dict) -> list[str]:
    """One round; returns what it did, one line each. `settings`: orch.dashboard.launch.load_settings()."""
    fs.human_check(actor, "running the AI Factory")
    lines: list[str] = []
    on = permits.enabled(ws)
    live = fs.bindings(ws)
    if not on and not live:
        return lines
    cut = not ledger.head_ok()
    signed = [] if cut else ledger.entries(ws)
    names = launcher.alive()
    keep = []
    for b in live:
        why = None if b["name"] in names else "its session ended"
        if why is None:
            why = stop_reason(ws, b, signed, cut)
        if why is None:
            keep.append(b)
            continue
        if b["name"] in names:
            try:
                launcher.stop(b["name"])
            except (OrchError, OSError):
                pass  # the binding ends anyway: what keeps running is an ordinary session the harness asks about
        fs.end(ws, b["session"])
        lines.append(f"{b['name']}: {why}")
    if not on or cut:
        return lines
    cap = concurrency(ws)
    gone = fs.ended(ws)
    for entry in store.scan(ws):
        if entry.meta is None or not epics.is_epic(entry.meta) or entry.status == "done":
            continue
        epic = _ticket(ws, entry.id)
        d = permits.factory_delegation(ws, epic, signed) if epic is not None else None
        if d is None or not d["active"] or not fs.armed(ws, d["id"]):
            continue
        for ce in epics.children(ws, epic.id):
            if len(keep) >= cap:
                return lines
            t = _ticket(ws, ce.id)
            if t is None or not _launchable(ws, epic, d, t, signed):
                continue
            if any(b["child"] == t.id for b in keep):
                continue
            token = wake_token(epic, t, signed)
            if any(g["child"] == t.id and g["delegation"] == d["id"] and g["wake"] == token for g in gone):
                continue  # parked: nothing it waits for changed since it last started
            with epics.delegation_lock(d["id"]):
                if fs.runs(ws, d["id"], t.id) == 0 and fs.runs(ws, d["id"]) >= d["max_children"]:
                    continue
                if not fs.mark_run(ws, d["id"], t.id):
                    continue
            b = _launch(ws, actor, launcher, settings, epic, d, t, token, lines)
            if b is not None:
                keep.append(b)
    return lines

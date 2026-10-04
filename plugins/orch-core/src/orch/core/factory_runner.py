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
import os
import secrets
from pathlib import Path
from typing import Protocol

from orch.core import epics, factory_sessions as fs, ledger, permits, store
from orch.errors import OrchError

DEFAULT_CONCURRENCY = 3
RUNNABLE = ("open", "in-progress", "waiting")
# The only variables an agent session starts with (no dashboard token, no key, nothing else the server holds). The two
# config-dir variables are not secret: without them the agent's own orch would read another records folder.
ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "SHELL", "TMPDIR", "ORCH_STATE_DIR", "XDG_CONFIG_HOME")
_HARNESS_FILES = (".claude/settings.json", ".claude/settings.local.json", ".mcp.json")


class Launcher(Protocol):
    def alive(self) -> set[str]: ...
    def start(self, name: str, cwd: str, argv: list[str]) -> int:
        """Start the session; returns the pid of its first process (an ancestor of everything the agent runs)."""
    def stop(self, name: str) -> None: ...


def env_prefix(environ=None) -> list[str]:
    """`env -i` plus the allowlisted variables, as an argv prefix: the agent starts with nothing else."""
    environ = os.environ if environ is None else environ
    pairs = [f"{k}={environ[k]}" for k in ENV_ALLOW if isinstance(environ.get(k), str) and environ[k]
             and "\n" not in environ[k] and "\x00" not in environ[k]]
    return ["env", "-i", *pairs]


def work_prompt(key: str) -> str | None:
    """The built-in work prompt for `key`. Never the workspace config's prompt or any ticket text: an agent can edit
    those, and this text starts another agent."""
    from orch.config.load import DEFAULTS
    from orch.dashboard.data.agent_start import KEY_RE
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        return None
    return DEFAULTS["agents"]["prompts"]["work"].replace("{key}", key)


def start_dir(ws, t) -> str:
    """The child's own worktree when it names exactly one that resolves to a folder strictly inside the workspace
    and carries no harness settings of its own; else the workspace root."""
    root = Path(ws.root).resolve()
    wts = t.meta.get("worktrees")
    vals = list(wts.values()) if isinstance(wts, dict) else []
    if len(vals) == 1 and isinstance(vals[0], str) and vals[0] and "\x00" not in vals[0]:
        try:
            p = (root / vals[0]).resolve()
            if p != root and root in p.parents and p.is_dir() and not any((p / f).exists() for f in _HARNESS_FILES):
                return str(p)
        except (OSError, RuntimeError, ValueError):
            pass
    return str(root)


def _gate(ws, epic_id: str, did: str) -> bool:
    """Everything a launch needs, read fresh right before it: the factory on, the ledger whole, the signed charter live
    and current, not paused, not edited, not out of time, the epic not done, and the human's Start."""
    try:
        if not permits.enabled(ws) or not ledger.head_ok():
            return False
        epic = _ticket(ws, epic_id)
        if epic is None or epic.status == "done":
            return False
        d = permits.factory_delegation(ws, epic, ledger.entries(ws))
        return d is not None and d["id"] == did and d["active"] and fs.armed(ws, did)
    except Exception:
        return False


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
    try:
        permits.require_budget(ws, t)  # the same budget refusal an agent's claim meets
    except OrchError:
        return False
    return (t.status in RUNNABLE and not epics.is_epic(t) and epics.within_limits(t, d) is None
            and not epics.hidden_in(t) and epics.child_state(ws, epic, t, signed) in ("delegated", "covered"))


def _launch(ws, actor, launcher, settings, epic, d, t, token, lines) -> dict | None:
    """Start one session. The command is the user's launch setting with the generated id and the built-in prompt put
    in as whole argv elements (never a shell, never ticket text), under `env -i` with the allowlisted variables."""
    prompt = work_prompt(t.id)
    if prompt is None or not _gate(ws, epic.id, d["id"]):
        return None
    sid = fs.new_session_id()
    name = f"fx-{t.id}-{secrets.token_hex(3)}"  # unrelated to the session id: names are shown in Mission Control
    argv = [*env_prefix(), *(a.replace("{session}", sid).replace("{prompt}", prompt) for a in settings["factory_command"])]
    b = fs.bind(ws, actor, session=sid, epic=epic.id, delegation=d["id"], child=t.id, name=name, wake=token)
    try:
        pid = launcher.start(name, start_dir(ws, t), argv)
        fs.set_pid(ws, actor, sid, pid)
    except (OrchError, OSError, ValueError) as e:
        try:
            launcher.stop(name)
        except (OrchError, OSError):
            pass
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

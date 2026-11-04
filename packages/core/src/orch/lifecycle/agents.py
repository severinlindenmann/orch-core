"""End the running agents of one workspace the way the runner's own stop does: SIGHUP and SIGTERM to the leader, then
wait. Never SIGKILL: an agent that does not end is reported and left. Only a process that is (still) a child of the
runner and born when we looked is signalled, so a stale run record can never hit another program."""

from __future__ import annotations

import json
import re
import signal
from pathlib import Path
from typing import Any

from .units import Env, Svc

__all__ = ["end_agents", "live_runs", "runner_pid"]

LIVE = ("starting", "running")
GRACE = 20.0


def live_runs(state: Path, wid: str | None) -> list[dict[str, Any]]:
    """Run records of ``wid`` (all workspaces when ``None``) that say they are live: ``[{run, workspace_id, pid}]``."""
    out: list[dict[str, Any]] = []
    hosts = Path(state) / "hosts"
    try:
        dirs = [hosts / wid] if wid else sorted(hosts.iterdir())
    except OSError:
        return out
    for d in dirs:
        if not re.fullmatch("[0-9a-f]{32}", d.name):
            continue
        for f in sorted((d / "runs").glob("r_*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            pid = rec.get("pid")
            if isinstance(rec, dict) and rec.get("state") in LIVE and isinstance(pid, int) and pid > 1:
                out.append({"run": rec.get("run") or f.stem, "workspace_id": d.name, "pid": pid})
    return out


def runner_pid(e: Env, s: Svc | None) -> int | None:
    """The process id the service manager runs the agent runner as (``None``: not running or cannot tell)."""
    if s is None or not s.installed or s.state != "running":
        return None
    if s.kind == "launchd":
        rc, out = e.run(["launchctl", "print", f"gui/{e.uid}/{s.name}"])
        m = re.search(r"^(?:\t| {4})pid = ([0-9]+)\s*$", out, re.M) if rc == 0 else None
    else:
        rc, out = e.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", s.name])
        m = re.fullmatch(r"\s*([1-9]\d*)\s*", out)
    return int(m.group(1)) if m else None


def _procs(e: Env) -> Any:
    if e.procs is not None:
        return e.procs
    from orch.runner import procs

    return procs


def end_agents(e: Env, runs: list[dict[str, Any]], rpid: int | None, grace: float = GRACE) -> tuple[int, list[str]]:
    """Ask the leaders of ``runs`` to end. Returns ``(ended, still_running_notes)``."""
    procs = _procs(e)
    asked: list[tuple[int, int, str]] = []
    notes: list[str] = []
    for r in runs:
        info = procs.info(r["pid"])
        if info is None or getattr(info, "zombie", False):
            continue  # already gone
        if rpid is None or info.ppid != rpid:
            notes.append(f"agent {r['run']} (process {r['pid']}) is not a child of the runner: left running")
            continue
        for sig in (signal.SIGHUP, signal.SIGTERM):
            procs.signal_verified(r["pid"], info.birth, sig)
        asked.append((r["pid"], info.birth, r["run"]))
    end = e.clock() + grace
    pending = list(asked)
    while pending and e.clock() < end:
        pending = [(p, b, run) for p, b, run in pending if procs.alive(p, b)]
        if pending:
            e.sleep(0.25)
    left = [f"agent {run} (process {p}) did not end within {int(grace)} seconds: left running" for p, _, run in pending]
    return len(asked) - len(pending), notes + left

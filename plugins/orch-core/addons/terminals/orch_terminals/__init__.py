"""terminals: the switch for Mission Control's Terminals (issue #40), plus a Today tile with how many run.

The terminal itself (the Terminals page, the live screens, typing) is core's, because addons render widgets only;
core serves it only while this addon is enabled in the workspace. This addon counts the sessions in orch's tmux
server (`tmux -L orch`) that started inside this workspace.
"""
from __future__ import annotations

from pathlib import Path

from orch.addons.api import Snapshot
from orch.addons.runner import AddonRunError
from orch.addons.widgets import Tile

ARGV = ["tmux", "-L", "orch", "list-sessions", "-F", "#{session_path}"]


def count(stdout: str, root) -> int:
    base = Path(root).resolve()
    n = 0
    for line in stdout.splitlines():
        path = Path(line.strip()).resolve() if line.strip() else None
        if path is not None and (path == base or base in path.parents):
            n += 1
    return n


class SessionsProvider:
    id = "sessions"
    kind = "status"
    interval_s = 10

    def scopes(self, ctx):
        return ["workspace"]

    def fetch(self, ctx, scope, previous):
        try:
            r = ctx.run(ARGV, timeout=5)
        except AddonRunError as e:
            return Snapshot(self.id, scope, ctx.now(), health="error", message=f"tmux is not installed ({e.message})")
        # no server yet (nothing ever started) is exit 1: that is 0 sessions, not an error
        n = count(r.stdout, ctx.root) if r.returncode == 0 else 0
        return Snapshot(self.id, scope, ctx.now(), items=(
            {"id": "running", "label": "Running", "role": "info" if n else "neu", "text": str(n), "count": n},))


class Terminals:
    def __init__(self, ctx):
        self.providers = [SessionsProvider()]

    def widgets(self, slot, view):
        if slot != "today.summary":
            return []
        snaps = view.snapshots("sessions")
        items = snaps[0].items if snaps else ()
        n = items[0].get("count") if items else None
        return [Tile("Terminals", n, "info" if n else "neu", href="/terminals", sub="running")]


def create(ctx):
    return Terminals(ctx)

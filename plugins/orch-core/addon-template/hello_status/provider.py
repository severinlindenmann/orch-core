"""A status provider: one `git status` per refresh, through ctx.run (never subprocess)."""
from __future__ import annotations

from orch.addons.api import Snapshot
from orch.addons.runner import AddonRunError

ARGV = ["git", "status", "--porcelain=v2", "--branch"]


def parse(stdout: str) -> tuple:
    head, upstream, changed = "(unknown)", None, 0
    for line in stdout.splitlines():
        if line.startswith("# branch.head "):
            head = line.removeprefix("# branch.head ").strip()
        elif line.startswith("# branch.ab "):
            ahead, behind = line.removeprefix("# branch.ab ").split()
            upstream = f"ahead {ahead.lstrip('+')}, behind {behind.lstrip('-')}"
        elif line and not line.startswith("#"):
            changed += 1
    items = [{"id": "branch", "label": "Branch", "role": "info", "text": head},
             {"id": "changes", "label": "Changed files", "role": "warn" if changed else "ok", "text": str(changed)}]
    if upstream:
        items.append({"id": "upstream", "label": "Upstream", "role": "neu", "text": upstream})
    return tuple(items)


class GitStatusProvider:
    id = "git-status"
    kind = "status"
    interval_s = 120

    def scopes(self, ctx):
        return ["harness"]

    def fetch(self, ctx, scope, previous):
        try:
            r = ctx.run(ARGV, timeout=10)
        except AddonRunError as e:
            return Snapshot(self.id, scope, ctx.now(), health="error", message=e.message)  # core keeps the previous items
        if r.returncode != 0:
            first = (r.stderr.strip().splitlines() or ["git status failed"])[0]
            return Snapshot(self.id, scope, ctx.now(), health="error", message=first)
        return Snapshot(self.id, scope, ctx.now(), items=parse(r.stdout))

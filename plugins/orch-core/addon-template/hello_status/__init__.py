"""hello-status: a minimal orch addon (API 2). Copy this folder to start your own addon; see ADDONS.md and
DESIGN.md (which widget for which data, copy rules, `orch addon check --strict`)."""
from __future__ import annotations

from orch.addons.widgets import KV, Badge, Card, Text

from .provider import GitStatusProvider


class HelloStatus:
    def __init__(self, ctx):
        self.page = f"page.{ctx.name}"  # follows the manifest name, so a renamed copy keeps working
        self.providers = [GitStatusProvider()]

    def widgets(self, slot, view):
        if slot != self.page:
            return []
        title = f"{view.settings.get('greeting') or 'Hello'}, {view.workspace_name}"
        snaps = view.snapshots("git-status")
        if not snaps or not snaps[0].items:  # an empty state is plain text; core's health line says why (DESIGN.md)
            return [Card(title, (Text("No git status yet. It shows up after the first fetch; press Refresh."),))]
        rows = tuple((item["label"], Badge(item["role"], item["text"])) for item in snaps[0].items)
        return [Card(title, (KV(rows),))]


def create(ctx):
    return HelloStatus(ctx)

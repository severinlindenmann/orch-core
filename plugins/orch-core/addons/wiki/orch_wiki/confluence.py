"""Confluence pages provider: an interface stub (spec A1 §7.4 and non-goals). The contract is in place, the client
is not; selecting it in settings shows a clear error instead of data."""
from __future__ import annotations

from orch.addons.api import Snapshot


class ConfluencePages:
    id = "confluence"
    kind = "pages"
    interval_s = 900

    def scopes(self, ctx) -> list[str]:
        return ["default"] if ctx.settings.get("provider") == "confluence" else []

    def fetch(self, ctx, scope, previous):
        return Snapshot(self.id, scope, ctx.now(), health="error",
                        message="The Confluence provider is not available yet; set the wiki provider to github-wiki.")

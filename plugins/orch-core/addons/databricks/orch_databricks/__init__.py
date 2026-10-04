"""databricks: job runs, pipelines, deploy drift and compute per environment, read-only (spec A1 §7.3)."""
from __future__ import annotations

from . import actions, render
from .provider import DatabricksProvider


class DatabricksAddon:
    def __init__(self, ctx):
        self.ctx = ctx
        self.page = f"page.{ctx.name}"
        self.providers = [DatabricksProvider()]

    def widgets(self, slot, view):
        if slot == self.page:
            return render.page(view, self.ctx)
        if slot == "today.summary":
            return render.today_summary(view)
        if slot == "today.from_addons":
            return render.today_from_addons(view, self.ctx)
        return []

    def act(self, action_id, target, ctx):
        return actions.act(action_id, target, ctx)


def create(ctx):
    return DatabricksAddon(ctx)

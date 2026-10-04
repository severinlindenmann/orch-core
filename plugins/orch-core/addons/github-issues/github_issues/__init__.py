"""github-issues: GitHub issues on Board · External and the ticket page (spec v2 §13, addons §7.2)."""
from __future__ import annotations

from .actions import act
from .provider import IssuesProvider
from .views import board, ticket_panel


class GitHubIssues:
    def __init__(self, ctx):
        self.providers = [IssuesProvider()]

    def widgets(self, slot, view):
        if slot == "board.external":
            return board(view)
        if slot == "ticket.external":
            return ticket_panel(view)
        return []

    def act(self, action_id, target, ctx):
        return act(action_id, target, ctx)


def create(ctx):
    return GitHubIssues(ctx)

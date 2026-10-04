"""github-reviews: open pull requests across the harness repo and its sub-repos (spec v2 §12, addons §7.1)."""
from __future__ import annotations

from .actions import act
from .github import GitHubProvider
from .localgit import LocalGitProvider
from .views import widgets


class GitHubReviews:
    def __init__(self, ctx):
        self.providers = [GitHubProvider(), LocalGitProvider()]

    def widgets(self, slot, view):
        return widgets(slot, view)

    def act(self, action_id, target, ctx):
        return act(action_id, target, ctx)


def create(ctx):
    return GitHubReviews(ctx)

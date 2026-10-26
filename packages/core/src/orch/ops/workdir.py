"""Where a ticket's outputs go (design r3 part C): a ticket that links a declared repo has a codebase (its worktree or
working copy); every other ticket has a work folder ``<project>/.orch-work/<KEY>/``, results of which become ticket
artifacts (``orch artifact add``). The host decides the same way for a launch (``orch.host.agents``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orch.layout import LayoutError, ensure_workdir, workdir_stat
from orch.store import observe

__all__ = ["has_codebase", "workdir_of"]


def has_codebase(store: Any, view: Any) -> bool:
    """THE rule (the host's launch and every op use only this): a ticket has a codebase when its PRIMARY repo (the first
    of ``links.repos``) is declared in the workspace settings. Any other ticket has a work folder. Whether a declared
    repo can be read is a separate question: the launch refuses (fail closed), it never falls back to a work folder."""
    repos = view.fields["links"]["repos"]
    return bool(repos) and observe.repo_path(store.layout, store.state.workspace.repos, repos[0]) is not None


def workdir_of(store: Any, view: Any, *, create: bool = False) -> Path | None:
    """The work folder of a ticket without a codebase, or ``None`` (it has a codebase, the folder is absent and
    ``create`` is false, or the folder cannot be used: a link, a foreign owner)."""
    if has_codebase(store, view):
        return None
    try:
        if create:
            ensure_workdir(store.layout, view.key)
        elif workdir_stat(store.layout, view.key) is None:
            return None
        return store.layout.workdir(view.key)
    except (LayoutError, OSError):
        return None

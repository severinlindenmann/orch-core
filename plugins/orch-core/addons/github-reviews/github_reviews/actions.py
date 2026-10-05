"""Rerun failed and Mark ready (spec v2 §12): declared in the manifest, run only from orch's human POST. The target
is checked against the current cache first, so a stale page cannot act on a PR that changed. Both are external
actions through ctx.run and change nothing in orch, so act() returns a `none` Intent carrying the message."""
from __future__ import annotations

import re

from orch.addons.api import Intent
from orch.errors import ValidationError

from .gh import classify
from .pending import record
from .views import failed_runs

_REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def _find(ctx, target) -> dict:
    repo, sep, number = str(target).rpartition("#")
    if not sep or not _REPO.fullmatch(repo) or not number.isdigit():
        raise ValidationError(f"not a pull request: {target!r}")
    for snap in ctx.snapshots("github"):
        for item in snap.items:
            if isinstance(item, dict) and item.get("repo") == repo and item.get("number") == int(number):
                return item
    raise ValidationError(f"{target} is not in the cache any more; refresh the page and try again")


def _gh(ctx, argv: list[str]) -> None:
    r = ctx.run(argv, timeout=60)  # AddonRunError (missing gh, timeout) is an OrchError: the dashboard shows it
    if r.returncode != 0:
        raise ValidationError(classify(r.returncode, r.stderr)[1])


def act(action_id, target, ctx) -> Intent:
    if action_id not in ("rerun_failed", "mark_ready"):
        raise ValidationError(f"unknown action {action_id!r}")
    item = _find(ctx, target)
    repo = item["repo"]
    if action_id == "rerun_failed":
        runs = failed_runs(item)
        if not runs:
            raise ValidationError(f"{target} has no failed GitHub Actions run to rerun")
        for run in runs:
            _gh(ctx, ["gh", "run", "rerun", run, "--failed", "--repo", repo])
        record(ctx.addon.state_dir, target)
        return Intent("none", reason=f"Rerun started for {len(runs)} failed run(s) on {target}")
    if item.get("draft") is not True:
        raise ValidationError(f"{target} is not a draft")
    _gh(ctx, ["gh", "pr", "ready", str(item["number"]), "--repo", repo])
    record(ctx.addon.state_dir, target)
    return Intent("none", reason=f"{target} is ready for review")

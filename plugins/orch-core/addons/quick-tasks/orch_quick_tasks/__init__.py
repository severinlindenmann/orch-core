"""quick-tasks: the switch for quick tasks (orch.core.quick), like `graph` and `terminals`.

Quick tasks themselves are core's (`orch quick`, the commit-msg hook, Mission Control's /quick pages), because addons
render widgets only and the CLI never imports addon code. Core reads this addon's enabled flag and settings from the
user's own files; while it is off, `orch quick` refuses changes and /quick answers 404. The addon runs nothing.
"""
from __future__ import annotations


class QuickTasks:
    def __init__(self, ctx):
        self.ctx = ctx


def create(ctx):
    return QuickTasks(ctx)

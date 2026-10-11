"""One declaration module per command of format doc 10.3, in command-table order."""

from __future__ import annotations

import importlib

from orch.ops.base import Operation

MODULES = (
    "status",
    "describe",
    "help",
    "show",
    "list",
    "search",
    "next",
    "inbox",
    "new",
    "claim",
    "release",
    "handoff",
    "submit",
    "ask",
    "wait",
    "set",
    "section_set",
    "ac_add",
    "ac_edit",
    "task_list",
    "task_next",
    "task_add",
    "task_start",
    "task_done",
    "task_skip",
    "task_block",
    "task_reopen",
    "artifact_add",
    "artifact_replace",
    "artifact_list",
    "log",
    "apply",
    "approve",
    "request_changes",
    "verdict",
    "answer",
    "close",
    "reopen",
    "grant",
    "grant_revoke",
    "member_add",
    "member_remove",
    "member_role",
    "init",
    "doctor",
    "check",
    "instructions_sync",
    "instructions_hook",
    "import_v1",
    "addon_list",
    "addon_grant",
    "addon_disable",
    "addon_purge",
)


def collect() -> list[Operation]:
    return [importlib.import_module(f"{__name__}.{m}").OP for m in MODULES]

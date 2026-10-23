"""Ticket work folders (design r3 part C): ``workdir.get`` and ``workdir.delete``.

A ticket without a codebase works in ``<project>/.orch-work/<KEY>/`` (:mod:`orch.host.agents` decides; the launch makes
the folder). Its files are the person's: they stay after the ticket closes, until the person deletes them.

* ``workdir.get {ticket}`` (column P): ``{codebase, exists, files, bytes, truncated, folder}``; ``folder`` is the path
  below the project folder (never the full path). A ticket with a codebase answers ``codebase: true`` and nothing else.
* ``workdir.delete {ticket}`` (column P, idem, **no log event**): deletes exactly ``<project>/.orch-work/<KEY>`` and
  what is in it, through descriptors and without following a link (:func:`orch.layout.delete_workdir`). Who: a
  person-class owner or maintainer, the ticket's owner or an assignee, who can see the ticket. A live run that works in
  the folder refuses (``worktree.in_use``). A ticket with a codebase has no work folder: ``invalid.input``. Gone
  already: ``deleted: false`` (so a retry after a crash is the same answer).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["WORKDIR_OPS", "WRITE_OPS", "serve"]

WORKDIR_OPS: dict[str, int] = {"workdir.get": 1, "workdir.delete": 1}
WRITE_OPS = frozenset({"workdir.delete"})
CAPS = ["workdir/1"]
_SCHEMA = {
    "type": "object",
    "properties": {"ticket": {"type": "string", "minLength": 1, "maxLength": 80}},
    "required": ["ticket"],
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(_SCHEMA)


def _may_delete(viewer: Viewer, view: Any) -> bool:
    return viewer.manager or (
        viewer.person is not None
        and (viewer.person == view.owner or viewer.person in (view.people.get("assignees") or ()))
    )


def serve(call: Call) -> dict[str, Any]:
    from orch.layout import LayoutError, delete_workdir, workdir_summary
    from orch.ops.workdir import has_codebase

    from .outcomes import request_idem
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    if next(iter(_VALIDATOR.iter_errors(args)), None) is not None:
        raise HostError("invalid.input", f"{op}: args are not valid")
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    opts = call.frame.get("opts") or {}
    if opts.get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    viewer = Viewer.of(call)
    if viewer.person is None or call.column.name != "P" or call.relay is not None:
        raise HostError("op.not_allowed", f"{op} is a person's own, from the host")
    store = service.store
    store.refresh()
    view = viewer.ticket(store, args["ticket"])
    layout = store.layout
    if op == "workdir.get":
        if has_codebase(store, view):
            return ok_result({"codebase": True, "exists": False})
        try:
            found = workdir_summary(layout, view.key)
        except LayoutError as exc:
            raise HostError("invalid.input", f"the work folder cannot be read: {exc}"[:300]) from None
        out: dict[str, Any] = {"codebase": False, "exists": found is not None, "folder": f".orch-work/{view.key}"}
        if found is not None:
            out.update(files=found["files"], bytes=found["bytes"], truncated=bool(found["truncated"]))
        return ok_result(out)
    from .auth import Redactor

    idem = request_idem(op, opts)  # required: a write with an outcome record, like the other host-side writes
    if service.state == "degraded":
        raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    if not _may_delete(viewer, view):
        raise HostError("op.not_allowed", "only an owner, a maintainer, the ticket's owner or an assignee deletes it")
    if has_codebase(store, view):
        raise HostError("invalid.input", f"{view.key} has a codebase: it has no work folder")

    def apply(_intent: Any) -> dict[str, Any]:
        hub = getattr(service, "runners", None)
        for rec in hub.store.records.values() if hub is not None else ():
            cwd = rec.get("cwd") or {}
            if (
                rec.get("workspace_id") == service.workspace_id
                and rec.get("state") in _live()
                and cwd.get("kind") == "workdir"
                and cwd.get("ticket") == view.key
            ):
                raise HostError("worktree.in_use", f"run {rec.get('run')} works in this folder; stop it first")
        try:
            gone = delete_workdir(layout, view.key)
        except (LayoutError, OSError) as exc:
            raise HostError("invalid.input", f"the work folder could not be deleted: {exc}"[:300]) from None
        if gone:  # the Claude Code trust recorded for the folder goes with it (best effort, the person's own config)
            from orch.runner.claude_trust import forget

            forget(os.environ, str(service.entry.root), os.fspath(layout.workdir(view.key)))
        return ok_result({"deleted": gone, "folder": f".orch-work/{view.key}"})

    return service.outcomes.execute(
        call.principal, str(idem), op, dict(args), apply, planned=None,
        session=(call.frame.get("auth") or {}).get("session"),
        redactor=Redactor((call.frame.get("auth") or {}).get("grant")),
    )  # fmt: skip


def _live() -> frozenset[str]:
    from .runners import LIVE

    return frozenset(LIVE)

"""Ticket work folders (design r3 part C): ``workdir.get``, ``workdir.read`` and ``workdir.delete``.

A ticket without a codebase works in ``<project>/.orch-work/<KEY>/`` (:mod:`orch.host.agents` decides; the launch makes
the folder). Its files are the person's: they stay after the ticket closes, until the person deletes them.

* ``workdir.get {ticket}`` (column P): ``{codebase, exists, files, bytes, truncated, folder}``; ``folder`` is the path
  below the project folder (never the full path). A ticket with a codebase answers ``codebase: true`` and nothing else.
* ``workdir.read {ticket, name}``: one currently listed regular file, capped at 256 KiB, for the same people who
  may list files. Returns a content kind, digest and text/base64, or only size and ``too_big``.
* ``workdir.delete {ticket}`` (column P, idem, **no log event**, but a host-written ``workdir.delete`` line in
  ``audit-security.jsonl``): deletes exactly ``<project>/.orch-work/<KEY>`` and
  what is in it, through descriptors and without following a link (:func:`orch.layout.delete_workdir`). Who: a
  person-class owner or maintainer, the ticket's owner or an assignee, who can see the ticket. A live run that works in
  the folder refuses (``worktree.in_use``). A ticket with a codebase has no work folder: ``invalid.input``. Gone
  already: ``deleted: false`` (so a retry after a crash is the same answer).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["WORKDIR_OPS", "WRITE_OPS", "may_manage_workdir", "serve"]

WORKDIR_OPS: dict[str, int] = {"workdir.get": 1, "workdir.read": 1, "workdir.delete": 1}
WRITE_OPS = frozenset({"workdir.delete"})
CAPS = ["workdir/1"]
_SCHEMA = {
    "type": "object",
    "properties": {
        "ticket": {"type": "string", "minLength": 1, "maxLength": 80},
        "list": {"type": "boolean"},  # workdir.get: also name the files directly in the folder (P03, at most 20)
    },
    "required": ["ticket"],
    "additionalProperties": False,
}
_VALIDATOR = Draft202012Validator(_SCHEMA)


def _spoofing(ch: str) -> bool:
    """A character that is not plain text to show before a verdict: control characters (C0 except tab/CR/LF, DEL,
    C1) and the bidi / zero-width marks that can make a file read differently from what it holds."""
    o = ord(ch)
    if ch in "\t\r\n":
        return False
    return (
        o < 32
        or 0x7F <= o <= 0x9F
        or o in (0x061C, 0x180E, 0x2028, 0x2029, 0xFEFF)
        or 0x200B <= o <= 0x200F
        or 0x202A <= o <= 0x202E
        or 0x2060 <= o <= 0x2069
    )


def may_manage_workdir(viewer: Viewer, view: Any) -> bool:
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
    validator = (
        _VALIDATOR
        if op != "workdir.read"
        else Draft202012Validator(
            {
                "type": "object",
                "properties": {
                    "ticket": _SCHEMA["properties"]["ticket"],
                    "name": {"type": "string", "minLength": 1, "maxLength": 100},
                },
                "required": ["ticket", "name"],
                "additionalProperties": False,
            }
        )
    )
    if next(iter(validator.iter_errors(args)), None) is not None:
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
    if op == "workdir.read":
        import base64
        import hashlib

        from orch.layout import WorkdirTooBig, workdir_read

        name = args["name"]
        if has_codebase(store, view) or not may_manage_workdir(viewer, view):
            raise HostError("op.not_allowed", "this work file is not available")
        if "/" in name or "\\" in name or ".." in name or name not in {x["name"] for x in _items(layout, view.key)[0]}:
            raise HostError("invalid.input", "not a listed work file")
        try:
            data = workdir_read(layout, view.key, name, 256 * 1024)
        except WorkdirTooBig as exc:
            return ok_result({"name": name, "too_big": True, "bytes": exc.size})
        except (LayoutError, OSError):
            raise HostError("invalid.input", "the work file cannot be read") from None
        kind = "other"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            kind = "png"
        elif data.startswith(b"\xff\xd8\xff"):
            kind = "jpeg"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            kind = "webp"
        # #1878 review: bytes leave the host only in a form the page shows: an image as base64, text as text; any
        # other file answers with its name, size and digest only (never raw bytes of a key, dump or archive)
        content: dict[str, str] = {}
        if kind != "other":
            content = {"base64": base64.b64encode(data).decode("ascii")}
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and not any(_spoofing(ch) for ch in text):
                kind, content = "text", {"text": text}
        return ok_result(
            {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "kind": kind, **content}
        )
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
            if args.get("list") is True and may_manage_workdir(viewer, view):  # the same people who may delete it
                out["items"], out["items_truncated"] = _items(layout, view.key)
        return ok_result(out)
    from .auth import Redactor

    idem = request_idem(op, opts)  # required: a write with an outcome record, like the other host-side writes
    if service.state == "degraded":
        raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    if not may_manage_workdir(viewer, view):
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
        from . import securitylog

        securitylog.record(  # the files are the person's and gone for good: a host-written line, no signed log event
            service.state_dir, service.host_uid, service.clock(), "workdir.delete",
            workspace_id=service.workspace_id, principal=call.principal, person=viewer.person,
            ticket=view.key, deleted=gone,
        )  # fmt: skip
        return ok_result({"deleted": gone, "folder": f".orch-work/{view.key}"})

    return service.outcomes.execute(
        call.principal, str(idem), op, dict(args), apply, planned=None,
        session=(call.frame.get("auth") or {}).get("session"),
        redactor=Redactor((call.frame.get("auth") or {}).get("grant")),
    )  # fmt: skip


def _items(layout: Any, key: str) -> tuple[list[dict[str, Any]], bool]:
    """P03: the plain files directly in the folder a share could take (names the server accepts), 20 at most, and
    whether there were more."""
    from orch.layout import LayoutError, workdir_files
    from orch.publishclient import approval

    def takes(name: str) -> bool:
        try:
            approval.check_name(name)
        except ValueError:
            return False
        return True

    try:
        found, more = workdir_files(layout, key, limit=approval.LIMITS["files"], accept=takes)
    except LayoutError:
        return [], False
    return [{"name": n, "bytes": b} for n, b in found], more


def _live() -> frozenset[str]:
    from .runners import LIVE

    return frozenset(LIVE)

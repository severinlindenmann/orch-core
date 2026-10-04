"""Shared by compare, screens and video (not a type: the leading underscore keeps the registry away). A file of the
ticket is linked as /a/<ticket>/<name> on a page and embedded as a data: URI in a standalone document, whose CSP
loads nothing else."""
from __future__ import annotations

import mimetypes
from urllib.parse import quote

from orch.widgets import artifacts
from orch.widgets.pages import PageKey

SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
REQUIRED = ["path", "sha256"]
VIDEO_TYPES = {"video/mp4", "video/webm", "video/ogg", "video/quicktime"}


def file_schema(extra: dict | None = None) -> dict:
    """The schema of one digest-pinned file: {path under artifacts/<ID>/, sha256}."""
    return {"type": "object", "additionalProperties": False, "required": list(REQUIRED),
            "properties": {"path": {"type": "string", "pattern": "^(artifacts/[^/]+/|artifact:|_files/).+", "maxLength": 500}, "sha256": SHA, **(extra or {})}}


def uri(ctx, file: dict, kinds=artifacts.IMAGE_TYPES) -> str | None:
    """Where the browser gets the pinned `file` ({path, sha256}), or None (missing, outside the ticket, wrong kind,
    too big to embed, or its bytes no longer match the digest the block pins: a widget never shows another file than
    the one its text, which a verdict hashes, names). On a page the /a/ URL carries ?v=<digest> and the route sends
    only bytes it hashed as read; a standalone document embeds bytes read once and hashed (artifacts.data_uri)."""
    tid = getattr(ctx.ticket, "id", None)
    ref, digest = file.get("path"), file.get("sha256")
    if not ctx.standalone and not isinstance(tid, PageKey):  # a page has no /a/ route: its files are embedded
        path = artifacts.resolve(ctx.ws, tid, ref) if tid else None
        kind = mimetypes.guess_type(path.name)[0] if path else None
        if path is None or kind not in kinds or not digest or artifacts.sha256(path) != digest:
            return None
        return f"/a/{quote(tid)}/{quote(artifacts.name_of(tid, ref))}?v={digest}"
    return artifacts.data_uri(ctx.ws, tid, ref, digest, kinds) if tid else None


def unavailable(what: str) -> str:
    return f'<p class="w-note w-note-neu" role="note"><b>Note:</b> {what} cannot be shown here</p>'

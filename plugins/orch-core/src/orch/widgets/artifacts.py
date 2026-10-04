"""Files a block names: `artifacts/<ID>/<name>` inside the ticket's own artifact folder, pinned by sha256
(docs/widgets.md, "Files by digest"). A gate hashes text, not files; the digest makes the text pin the file."""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

MAX_DATA_URI = 5 * 1024 * 1024
MAX_INLINED = 8 * 1024 * 1024  # all images inlined into one document, each counted once
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}


def name_of(ticket_id: str, ref) -> str | None:
    """The file name inside the ticket's own folder that `ref` names: `artifacts/<ticket_id>/<name>` or
    `artifact:<name>` (the ticket artifact reference of orch.core.artifacts), else None. For a wiki page (a
    `PageKey`), `_files/<name>` of its wiki folder."""
    from orch.core.artifacts import safe_name
    from orch.widgets.pages import FILES, PageKey
    if not isinstance(ref, str):
        return None
    if isinstance(ticket_id, PageKey):  # a wiki page names only `_files/<name>` of its own wiki folder
        name = ref[len(FILES) + 1:] if ref.startswith(f"{FILES}/") else ""
        return name if safe_name(name) and "/" not in name else None
    for prefix in (f"artifacts/{ticket_id}/", "artifact:"):
        if ref.startswith(prefix):
            name = ref[len(prefix):]
            return name if safe_name(name) else None
    return None


def resolve(ws, ticket_id: str, ref: str) -> Path | None:
    """The file `ref` names, only when it is under this ticket's own folder (orch.core.query.artifact_file)."""
    from orch.core.query import artifact_file
    from orch.widgets.pages import PageKey, file_path
    name = name_of(ticket_id, ref)
    if isinstance(ticket_id, PageKey):
        return None if name is None else file_path(ticket_id, name)
    return None if ws is None or name is None else artifact_file(ws, ticket_id, name)


def sha256(path: Path) -> str:
    """The file's digest: orch.core.artifacts.file_sha256 (cached by path, mtime, size and inode)."""
    from orch.core.artifacts import file_sha256
    return file_sha256(path) or ""


def state(ws, ticket_id: str, ref: str, digest: str | None) -> str:
    """"ok", "missing" (not there, or outside the ticket's folder) or "changed" (another digest than pinned)."""
    path = resolve(ws, ticket_id, ref)
    if path is None:
        return "missing"
    return "ok" if digest and sha256(path) == digest else "changed"


def refs(data) -> list[dict]:
    """Every pinned file in a block: any object with a `sha256` and a `path` (or `html`, the one-off layer)."""
    out = []
    if isinstance(data, dict):
        ref = data.get("path", data.get("html"))
        if "sha256" in data and isinstance(ref, str):
            out.append({"ref": ref, "sha256": data.get("sha256"), "node": data})
        for v in data.values():
            out += refs(v)
    elif isinstance(data, list):
        for v in data:
            out += refs(v)
    return out


def data_uri(ws, ticket_id: str, ref: str, digest, kinds=IMAGE_TYPES) -> str | None:
    """The file as a `data:` URI (frames and standalone documents load nothing), when it exists, is of `kinds`, is at
    most 5 MB and its bytes have sha256 `digest`. Read once (orch.core.artifacts.read_pinned): the bytes embedded are
    the bytes that were hashed, never a second read after a check."""
    from orch.core.artifacts import read_pinned
    from orch.widgets.pages import PageKey
    path = resolve(ws, ticket_id, ref)
    kind = mimetypes.guess_type(path.name)[0] if path else None
    if path is None or kind not in kinds or not isinstance(digest, str) or len(digest) != 64:
        return None
    root = None
    if ws is not None and not isinstance(ticket_id, PageKey):  # a link inside the ticket's folder is refused, not followed
        from orch.core.query import artifact_root
        root = artifact_root(ws, ticket_id)
        path = root / name_of(ticket_id, ref)
    data = read_pinned(path, digest, MAX_DATA_URI, root=root)
    if data is None:
        return None
    return f"data:{kind};base64,{base64.b64encode(data).decode('ascii')}"


def fill_digests(ws, ticket_id: str, data) -> list[str]:
    """Set `sha256` on every object naming a file of this ticket (`path`, or `html` for a one-off page) whose digest
    is missing or empty (`orch widget add`). Returns the names that are not files of this ticket."""
    missing = []
    if isinstance(data, dict):
        ref = data.get("path", data.get("html"))
        if isinstance(ref, str) and ref.startswith(("artifacts/", "artifact:")) and not data.get("sha256"):
            path = resolve(ws, ticket_id, ref)
            if path is None:
                missing.append(ref)
            else:
                data["sha256"] = sha256(path)
    for v in data.values() if isinstance(data, dict) else data if isinstance(data, list) else []:
        missing += fill_digests(ws, ticket_id, v)
    return missing

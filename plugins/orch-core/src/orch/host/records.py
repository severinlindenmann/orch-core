"""The host's records ops (H11e, addon #308, cap ``records/1``; orch-v2-host D-P2-33/34 as ruled by the tie-break).

The host never runs git (D-P2-19). It names the bytes and decides the export policy; the person-side tool
(``python -I -m orch.records``) reads, commits and pushes.

* ``records.snapshot {page?}`` names an **immutable manifest** under the store lock: every record file's path, byte
  size and SHA-256 (a log as its committed prefix), the workspace ``ws_seq``, the chain heads, and a ``manifest`` id
  (:func:`orch.records.manifest_id`). Pages of :data:`~orch.records.PAGE_FILES` files belong to that manifest and to
  the caller's principal, for :data:`~orch.records.TTL_SECONDS`. Limits: 64 MiB per file, 1 GiB per snapshot
  (``records.too_large``). Digests are cached by (path, size, inode, mtime, ctime), so immutable artifacts are not
  re-hashed while the lock is held.
* ``records.export_check {sha, url, ref}`` runs on **every** push path right before the push: it is bound to the
  workspace, the exact source commit, the exact effective push URL, the destination ref and the current convenience
  policy. A records push counts as adding a reader (D-P2-34): while convenience signing is on for the workspace or its
  history holds ``auth: "convenience"`` events, the push is refused (``sign.convenience_readers``) unless that exact
  URL carries the owner's private-remote mark. Every answer is recorded (the export destination audit).
* ``records.remote_private {url, on}`` sets or clears the workspace's one private-remote mark (owner decision 12 Oct
  2026): "only I read this remote". orch trusts the owner's word; it is not proof of the remote's ACLs. The mark is
  one exact URL: a changed ``pushurl``, ``insteadOf`` rewrite or remote URL resolves to another string and is not
  marked.

All three are column P only (``person_read``: G and N get ``op.not_allowed``) and only for a person who is an owner or
maintainer now (a push exports every ticket, restricted ones included). This is a policy orch enforces on its own
export paths, not information-flow control: manual git can still push.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch import records as rec
from orch.ops.readers import has_convenience_events

from . import convenience
from .custody import ensure_private_dir, read_json, write_private
from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["CAPS", "RECORDS_OPS", "TRUST_TEXT", "serve"]

CAPS = ["records/1"]
RECORDS_OPS: dict[str, int] = {"records.snapshot": 1, "records.export_check": 1, "records.remote_private": 1}
#: manifests kept per workspace (the oldest goes first)
MAX_MANIFESTS = 4
#: export answers kept in the audit list
MAX_EXPORTS = 50
TRUST_TEXT = (
    "orch trusts your word that only you read this remote; if someone else can read it, they get this workspace's "
    "convenience-signed history"
)

_URL = {"type": "string", "minLength": 1, "maxLength": 2048, "pattern": r"^[^\x00-\x1f\x7f]+$"}
_SCHEMAS = {
    "records.snapshot": {
        "type": "object",
        "properties": {"page": {"type": "string", "minLength": 1, "maxLength": 200}},
        "additionalProperties": False,
    },
    "records.export_check": {
        "type": "object",
        "properties": {
            "sha": {"type": "string", "pattern": r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"},
            "url": _URL,
            "ref": {"type": "string", "pattern": r"^refs/heads/[A-Za-z0-9._/-]{1,200}$"},
        },
        "required": ["sha", "url", "ref"],
        "additionalProperties": False,
    },
    "records.remote_private": {
        "type": "object",
        "properties": {"url": _URL, "on": {"type": "boolean"}},
        "required": ["on"],
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


def _validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")
    for name in ("url", "ref", "sha", "page"):  # a schema ``$`` also matches before a final newline
        value = args.get(name)
        if isinstance(value, str) and any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise HostError("invalid.input", f"{op}: {name} is not valid")
    url = args.get("url")
    if isinstance(url, str):
        from orch.records.git import has_credentials

        if has_credentials(url):  # never stored, never echoed: the mark and the audit hold no secret
            raise HostError("invalid.input", f"{op}: url carries credentials; use a credential helper")
    if op == "records.remote_private" and args["on"] and "url" not in args:
        raise HostError("invalid.input", "records.remote_private: url is required to set the mark")


def _host_error(exc: rec.RecordsError) -> HostError:
    # the records codes are the tool's catalog (orch.records.RecordsError), not rows of §0.6's host table
    out = HostError(exc.code, exc.message, hint=exc.hint)
    out.exit, out.retryable = exc.exit, exc.code == "records.snapshot_changed"
    return out


# --- the per-workspace record (marks and export audit) ------------------------------------------------------------


def record_path(state_dir: Path, workspace_id: str) -> Path:
    return Path(state_dir) / "hosts" / workspace_id / "records.json"


def read_record(state_dir: Path, workspace_id: str, host_uid: int) -> dict[str, Any]:
    doc = read_json(record_path(state_dir, workspace_id), host_uid)
    if doc is None:
        return {"v": 1, "private_remote": None, "exports": []}
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("exports"), list):
        raise HostError("internal", "the records file is corrupt; refusing to decide an export")
    return doc


def _write_record(state_dir: Path, workspace_id: str, host_uid: int, doc: dict[str, Any]) -> None:
    ensure_private_dir(Path(state_dir) / "hosts" / workspace_id, host_uid)
    write_private(record_path(state_dir, workspace_id), doc)


def _stamp(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(now)))


# --- serving ------------------------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    """One records op on the worker thread (after the column): args, files, the workspace state, the role, the op."""
    from .outcomes import request_idem
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    _validate(op, args)
    request_idem(op, call.frame.get("opts"))  # dry_run is invalid.input here; idem is ignored
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    viewer = Viewer.of(call)
    if not viewer.manager:
        raise HostError("op.not_allowed", f"{op} is for a workspace owner or maintainer at the terminal or dashboard")
    try:
        if op == "records.snapshot":
            return ok_result(_snapshot(call, args))
        if op == "records.export_check":
            return ok_result(_export_check(call, viewer, args))
        return ok_result(_remote_private(call, viewer, args))
    except rec.RecordsError as exc:
        raise _host_error(exc) from None


def _state(service: Any) -> dict[str, Any]:
    st = getattr(service, "records_state", None)
    if st is None:
        st = {"manifests": OrderedDict(), "hashes": {}}
        service.records_state = st
    return st


def _take(service: Any) -> dict[str, Any]:
    """A new manifest of the record files, under the store lock."""
    from orch.model import WORKSPACE

    store = service.store
    st = _state(service)
    with store.locked():
        store.load_all()  # the chain heads of every ticket log, verified
        root_fd = rec.open_root(Path(store.root))
        try:
            paths = rec.list_records(root_fd)
            entries = rec.hash_records(root_fd, paths, st["hashes"])
        finally:
            os.close(root_fd)
        ws_seq = int(store.head_seq(WORKSPACE))
        heads: dict[str, str | None] = {"workspace": store.log_head(WORKSPACE)}
        for p in paths:
            if p.startswith("tickets/") and p.endswith("/events.jsonl"):
                uid = p.split("/")[1]
                heads[uid] = store.log_head(uid)
    live = {(e.path, e.size) for e in entries}
    st["hashes"] = {k: v for k, v in st["hashes"].items() if (k[0], k[1]) in live}  # keep the cache bounded
    mid = rec.manifest_id(service.workspace_id, ws_seq, heads, entries)
    return {"id": mid, "ws_seq": ws_seq, "heads": heads, "files": [e.as_dict() for e in entries]}


def _snapshot(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    now = float(service.clock())
    manifests: OrderedDict[str, dict[str, Any]] = _state(service)["manifests"]
    for mid in [m for m, v in manifests.items() if v["expires"] <= now]:
        manifests.pop(mid, None)
    if "page" in args:
        mid, _, pos = args["page"].partition(".")
        held = manifests.get(mid)
        if held is None or not pos.isdigit():
            raise HostError("page.expired", "the records manifest expired or is unknown: take a new snapshot")
        if held["principal"] != call.principal:
            raise HostError("page.expired", "the records manifest belongs to another caller")
        offset = int(pos)
        manifest = held["manifest"]
        taken_at = held["taken_at"]
    else:
        manifest = _take(service)
        taken_at = _stamp(now)
        manifests[manifest["id"]] = {
            "principal": call.principal,
            "expires": now + rec.TTL_SECONDS,
            "manifest": manifest,
            "taken_at": taken_at,
        }
        manifests.move_to_end(manifest["id"])
        while len(manifests) > MAX_MANIFESTS:
            manifests.popitem(last=False)
        offset = 0
    files = manifest["files"]
    end = min(offset + rec.PAGE_FILES, len(files))
    nxt = f"{manifest['id']}.{end}" if end < len(files) else None
    return {
        "workspace": service.workspace_id,
        "manifest": manifest["id"],
        "ws_seq": manifest["ws_seq"],
        "heads": manifest["heads"],
        "taken_at": taken_at,
        "count": len(files),
        "bytes": sum(f["size"] for f in files),
        "files": files[offset:end],
        "next_page": nxt,
    }


def convenience_on(service: Any) -> bool:
    """The convenience-readers condition (§0.8.6): the flag is on, or the history holds convenience events."""
    if convenience.enabled(service.state_dir, service.workspace_id, service.host_uid):
        return True
    store = service.store
    with store.locked():
        return has_convenience_events(store)


def _export_check(call: Call, viewer: Viewer, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    now = float(service.clock())
    doc = read_record(service.state_dir, service.workspace_id, service.host_uid)
    conv = convenience_on(service)
    mark = doc.get("private_remote")
    marked = isinstance(mark, dict) and mark.get("url") == args["url"]
    policy = "none"
    if conv:
        if not marked:
            entry = {**args, "policy": "refused", "at": _stamp(now), "by": viewer.person}
            doc["exports"] = [*doc["exports"], entry][-MAX_EXPORTS:]
            _write_record(service.state_dir, service.workspace_id, service.host_uid, doc)
            why = (
                "the private-remote mark names another URL; the push destination changed since it was marked"
                if isinstance(mark, dict)
                else "no private-remote mark for this URL"
            )
            raise HostError(
                "sign.convenience_readers",
                f"a records push adds a reader to a workspace with convenience signing ({why})",
                hint="mark this exact push URL private (orch records remote-private), or keep the records local",
            )
        policy = "private_remote"
    body = {"workspace": service.workspace_id, **args, "policy": policy, "convenience": conv}
    check = "x_" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    doc["exports"] = [*doc["exports"], {**args, "policy": policy, "at": _stamp(now), "by": viewer.person}][
        -MAX_EXPORTS:
    ]
    _write_record(service.state_dir, service.workspace_id, service.host_uid, doc)
    return {**body, "check": check}


def _remote_private(call: Call, viewer: Viewer, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    now = float(service.clock())
    doc = read_record(service.state_dir, service.workspace_id, service.host_uid)
    if args["on"]:
        doc["private_remote"] = {"url": args["url"], "at": _stamp(now), "by": viewer.person}
    else:
        doc["private_remote"] = None
    _write_record(service.state_dir, service.workspace_id, service.host_uid, doc)
    return {"private_remote": doc["private_remote"], "text": TRUST_TEXT}

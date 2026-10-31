"""The Publish add-on on the host (P5, design-publish-addon §2.2): ``publish.prepare``, ``publish.send``,
``publish.revoke``, ``publish.list``, ``publish.discard`` and ``publish.register``; the human op ``publish.share``
(through ``human.prepare``/``human.submit``) is the signature that lets one staged copy leave.

**The flow.** An agent or a person ``prepare``s a share of named artifacts of a ticket: the host copies them (by the
ticket manifest and digest, as ``artifact.get``) into a host-private stage and nothing leaves the machine. A person
then signs ``publish.share {staged_id}``: a ``log.added`` note on the ticket whose words the host builds from the stage
(:func:`orch.publishclient.approval.approval_text`: ticket, kind, days, views, **server**, **every file with size and
hash**). ``publish.send {staged_id}`` (column P only) builds the same words again from the stage it is about to upload
and refuses unless a person's signed note with exactly those words is in the ticket log, so the signature covers the
bytes that go. Only then does the host sign the requests to the server with the workspace key
(:class:`orch.wsproto.pubsign.PublishSigner`) and upload.

**The link.** The token is made in ``publish.send`` (``secrets.token_bytes(32)``, base64url, no padding) and returned
to that one call only. It is kept in a 0600 file of the stage until the server has committed, then wiped; the server
gets only ``sha256(token)``. It is never in the log, the outcome store, the audit file or an error. A retry before
the wipe resends the same ``token_hash`` (idempotent); if the answer is lost after the wipe the link is gone and the
person revokes and shares again.

**Invariants** (the design's I1, I2, I12): human egress (the approval is exact and the send is column P); agents never
get a token or a key (``prepare`` and ``list`` carry none; an agent cannot send); the bytes uploaded are the staged
bytes, hashed again as they are read; the stage is 0700 and host-owned.

**Server.** ``settings.publish`` is signed by the owner (``settings.set``): the API and share origins, never an agent's
choice. The workspace registers card-free (design §2.6b): the genesis delegation and the owner key, in a request signed
with the publish label.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import calendar
import collections
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch.publishclient import approval

from .artifacts import artifact_file
from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["PUBLISH_CAPS", "PUBLISH_OPS", "WRITE_OPS", "execute", "facts_for", "serve"]

PUBLISH_OPS: dict[str, int] = {
    "publish.stage_upload": 1,
    "publish.prepare": 1,
    "publish.send": 1,
    "publish.revoke": 1,
    "publish.list": 1,
    "publish.discard": 1,
    "publish.register": 1,
    "publish.extend": 1,
    "publish.password": 1,
}
REGISTER_PER_MINUTE = 3
WRITE_OPS = frozenset(
    {
        "publish.stage_upload",
        "publish.prepare",
        "publish.send",
        "publish.revoke",
        "publish.discard",
        "publish.register",
        "publish.extend",
        "publish.password",
    }
)
#: the ops that talk to the server (their network half runs off the workspace worker, in :func:`execute`)
NETWORK_OPS = frozenset({"publish.send", "publish.revoke", "publish.register", "publish.extend", "publish.password"})
#: M4 S2 (#1453): the longest a link can be extended to, counted from now (orch-publish enforces the same bound)
EXTEND_MAX_DAYS = 30
PASSWORD_WORDS = 5
PUBLISH_CAPS = ["publish/1"]
ADDON = "publish"
PER_MINUTE = 10
MAX_STAGED = 20
AGENT_STAGES = 3
SCAN_CHUNK = 200
STAGE_BYTES = 200 * 1024 * 1024
#: P05 (J07-02): a file a person picks from this computer in the dashboard waits (0700, outside any stage) until the
#: next ``publish.prepare`` takes it into the stage that is hashed and signed. One request carries the whole file
#: (the host frame is 2 MiB), so a pick is at most this big; the pending room is bounded and the oldest is dropped.
UPLOAD_BYTES = 1024 * 1024
MAX_UPLOADS = 20
_UPLOAD_ID = re.compile(r"pu_[0-9a-f]{32}")
MAX_RECORDS = 500
#: a staged copy no one finished with is dropped after this; a signature is good for a shorter time
STAGE_KEEP = 24 * 3600
APPROVAL_VALID = 3600
_REF = {"type": "string", "minLength": 1, "maxLength": 80}
_STAGED = {"type": "string", "pattern": r"^ps_[0-9a-f]{32}$"}
_SHARE = {"type": "string", "pattern": r"^[0-9a-f]{32}$"}
_UPLOAD = {"type": "string", "pattern": r"^pu_[0-9a-f]{32}$"}
_SCHEMAS = {
    # a picked file's bytes, base64, in the request itself; the name is checked again by approval.check_name
    "publish.stage_upload": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 128},
            "b64": {"type": "string", "minLength": 4, "maxLength": (UPLOAD_BYTES + 2) // 3 * 4},
        },
        "required": ["name", "b64"],
        "additionalProperties": False,
    },
    "publish.prepare": {
        "type": "object",
        "properties": {
            "ref": _REF,
            "artifacts": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "minItems": 0,
                "maxItems": approval.LIMITS["files"],
                "uniqueItems": True,
            },
            "uploads": {"type": "array", "items": _UPLOAD, "maxItems": approval.LIMITS["files"], "uniqueItems": True},
            # P03: plain names of files directly in the ticket's own work folder (the host resolves them, never a path)
            "workfiles": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "maxItems": approval.LIMITS["files"],
                "uniqueItems": True,
            },
            "kind": {"enum": list(approval.KINDS)},
            "days": {"enum": list(approval.DAYS)},
            "views": {"enum": list(approval.VIEWS)},
        },
        "required": ["ref"],
        "additionalProperties": False,
    },
    "publish.send": {
        "type": "object",
        "properties": {"staged_id": _STAGED},
        "required": ["staged_id"],
        "additionalProperties": False,
    },
    "publish.revoke": {
        "type": "object",
        "properties": {"share_id": _SHARE},
        "required": ["share_id"],
        "additionalProperties": False,
    },
    # `days` is a JSON integer 1..30 and nothing else (no bool, no float); the server bounds it again
    "publish.extend": {
        "type": "object",
        "properties": {"share_id": _SHARE, "days": {"type": "integer", "minimum": 1, "maximum": EXTEND_MAX_DAYS}},
        "required": ["share_id", "days"],
        "additionalProperties": False,
    },
    # no `password`, no `record`, no `remove`: the host draws the password and builds the record itself (C5), and
    # taking a password away would widen access, so there is no such request
    "publish.password": {
        "type": "object",
        "properties": {"share_id": _SHARE},
        "required": ["share_id"],
        "additionalProperties": False,
    },
    "publish.list": {
        "type": "object",
        "properties": {"ref": _REF, "live": {"type": "boolean"}},
        "additionalProperties": False,
    },
    "publish.discard": {
        "type": "object",
        "properties": {"staged_id": _STAGED},
        "required": ["staged_id"],
        "additionalProperties": False,
    },
    "publish.register": {"type": "object", "properties": {}, "additionalProperties": False},
    "publish.share": {
        "type": "object",
        "properties": {"staged_id": _STAGED},
        "required": ["staged_id"],
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}
_RATE: dict[tuple[str, str, str], collections.deque[float]] = {}
_MU = threading.Lock()  # the rate table, the sending set and the state files of every workspace this process serves
_SENDING: set[tuple[str, str]] = set()
_STAGE_ID = re.compile(r"ps_[0-9a-f]{32}")


def _validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


# -- places and files --------------------------------------------------------------------------------------------------


def _base(service: WorkspaceService) -> Path:
    return Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "publish"


def _private(service: WorkspaceService, *parts: str) -> Path:
    """``<state>/hosts/<ws>/publish[/parts]`` with every level created 0700 and checked (a link, another owner or a
    writable directory is ``custody.unsafe_mode``)."""
    from .custody import ensure_private_dir

    path = _base(service)
    for level in (path.parent, path, *(path.joinpath(*parts[: i + 1]) for i in range(len(parts)))):
        ensure_private_dir(level, service.host_uid)
    return path.joinpath(*parts)


def _write(path: Path, data: bytes) -> None:
    """A new 0600 file next to ``path``, flushed, then renamed over it (``O_EXCL|O_NOFOLLOW``)."""
    from orch.store.fsio import write_atomic

    write_atomic(path, data, mode=0o600)


def _read_json(service: WorkspaceService, path: Path) -> Any | None:
    from .custody import read_json

    return read_json(path, service.host_uid, limit=1 << 20)


def _put_json(path: Path, doc: Any) -> None:
    _write(path, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())


def _now_ms(service: WorkspaceService) -> int:
    """The host's clock in milliseconds: what the server's expiry is measured from (a seam for the real-server test,
    whose lab clock is made up)."""
    return int(service.clock() * 1000)


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _epoch(text: Any) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


# -- gate, config, columns ---------------------------------------------------------------------------------------------


def _gate(call: Call) -> None:
    """The ``publish`` add-on is granted and on, else ``runner.disabled`` (``missing: addon``) like the worktrees."""
    from . import hubpolicy

    if not hubpolicy.running(call.service, ADDON):  # granted, on, and not paused by the hub policy
        raise HostError(
            "runner.disabled",
            f"Publish is off: turn it on in Settings, Add-ons (missing: addon){hubpolicy.why(call.service, ADDON)}",
            detail={"missing": "addon"},
        )


def _config(service: WorkspaceService) -> dict[str, str]:
    from orch.store.render import thaw

    cfg = thaw(service.store.state.workspace.settings.get("publish"))
    if not isinstance(cfg, dict) or not isinstance(cfg.get("server"), str) or not isinstance(cfg.get("share"), str):
        raise HostError(
            "publish.not_configured",
            "no publish server is set up yet: a person runs orch publish setup --server ADDRESS --share ADDRESS",
        )
    return {"server": cfg["server"], "share": cfg["share"]}


def _person(call: Call) -> str:
    """Column P only (a person at their own socket); ``op.not_allowed`` for an agent, whatever its grant."""
    if call.column.name != "P" or call.viewer is None:
        raise HostError("op.not_allowed", "a person does this; an agent only prepares")
    return call.viewer


def _display(text: Any, limit: int) -> str | None:
    """One line of display text for the share list: white space collapsed, every character the contract refuses
    (hidden, bidi, control) dropped, cut to ``limit`` UTF-8 bytes on a character boundary; ``None`` if empty."""
    from orch.hubclient.sharefmt import text_ok

    if not isinstance(text, str):
        return None
    words = ("".join(c for c in word if text_ok(c, 4)) for word in text.split())
    line = " ".join(w for w in words if w)
    while line and len(line.encode("utf-8")) > limit:
        line = line[:-1].rstrip()
    return line or None


def _by_name(call: Call) -> str | None:
    """The signed-in person's name from the host's own member record (never from the request)."""
    call.service.store.refresh()
    member = call.service.store.state.workspace.members.get(call.viewer)
    return _display(member.name, 60) if member is not None else None


def _owner(call: Call) -> None:
    """Registering sends the workspace's delegation to the signed server: the owner's act (the role as it is now)."""
    call.service.store.refresh()
    member = call.service.store.state.workspace.members.get(call.viewer)
    if member is None or member.role != "owner":
        raise HostError("role.denied", "only a workspace owner registers this workspace with the publish server")


def _rate(call: Call, what: str, limit: int | None = None) -> None:
    key = (call.service.workspace_id, str(call.viewer), what)
    now = time.monotonic()
    with _MU:
        q = _RATE.setdefault(key, collections.deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= (limit or PER_MINUTE):
            raise HostError(
                "rate.limited",
                f"at most {limit or PER_MINUTE} publish requests per minute",
                retry_after_ms=int((60 - (now - q[0])) * 1000) + 1,
            )
        q.append(now)


def _audit(call: Call, event: str, **more: Any) -> None:
    from . import securitylog

    s = call.service
    securitylog.record(
        s.state_dir, s.host_uid, s.clock(), event, workspace_id=s.workspace_id, principal=call.principal, **more
    )


# -- the stage ---------------------------------------------------------------------------------------------------------


def _stage_dir(service: WorkspaceService, staged_id: str) -> Path:
    if _STAGE_ID.fullmatch(staged_id or "") is None:
        raise HostError("not_found", "no such staged copy")
    return _base(service) / "stage" / staged_id


def _load_stage(service: WorkspaceService, staged_id: str) -> dict[str, Any]:
    meta = _read_json(service, _stage_dir(service, staged_id) / "meta.json")
    if not isinstance(meta, dict) or meta.get("staged_id") != staged_id or meta.get("ws") != service.workspace_id:
        raise HostError("not_found", "no such staged copy")
    return meta


def _stage_read(service: WorkspaceService, staged_id: str, entry: dict[str, Any]) -> bytes:
    """One staged file, opened without following a link, and held to its recorded size and hash: the bytes returned are
    the bytes that were hashed, so what is uploaded is what was shown (I1)."""
    try:
        path = _stage_dir(service, staged_id) / "files" / approval.check_name(entry["name"])
    except (ValueError, KeyError, TypeError):
        raise HostError("publish.changed", "the staged copy is not valid: prepare the share again") from None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise HostError(
            "publish.changed", "a staged file is missing or was replaced: prepare the share again"
        ) from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != service.host_uid
            or info.st_size != entry["size"]
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise HostError("publish.changed", "a staged file changed: prepare the share again")
        data = handle.read(entry["size"] + 1)
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise HostError("publish.changed", "a staged file changed: prepare the share again")
    return data


def _manifest(service: WorkspaceService, meta: dict[str, Any], cfg: dict[str, str]) -> str:
    return approval.manifest_hash(
        service.workspace_id,
        meta["ticket"]["uid"],
        meta["kind"],
        meta["days"],
        meta["views"],
        meta["origin"],
        meta["files"],
        cfg["server"],
        cfg["share"],
    )


def _text(service: WorkspaceService, meta: dict[str, Any], cfg: dict[str, str]) -> str:
    try:
        return approval.approval_text(
            staged_id=meta["staged_id"],
            ticket_key=meta["ticket"]["key"],
            origin=meta["origin"],
            kind=meta["kind"],
            days=meta["days"],
            views=meta["views"],
            files=meta["files"],
            server=cfg["server"],
            share=cfg["share"],
            manifest=_manifest(service, meta, cfg),
        )
    except (ValueError, KeyError, TypeError):
        raise HostError("publish.changed", "the staged copy is not valid: prepare the share again") from None


def _verify_stage(service: WorkspaceService, meta: dict[str, Any]) -> list[bytes]:
    """Every staged file read and checked now; returns their bytes (for the callers that upload)."""
    return [_stage_read(service, meta["staged_id"], f) for f in meta["files"]]


def _sweep(service: WorkspaceService) -> None:
    """Drop staged copies no one finished with (older than :data:`STAGE_KEEP`). Never raises."""
    with contextlib.suppress(Exception):
        root = _base(service) / "stage"
        now = service.clock()
        for name in os.listdir(root):
            path = root / name
            if _STAGE_ID.fullmatch(name) and now - os.lstat(path).st_mtime > STAGE_KEEP:
                shutil.rmtree(path, ignore_errors=True)


def _stages(service: WorkspaceService) -> list[dict[str, Any]]:
    root = _base(service) / "stage"
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        if _STAGE_ID.fullmatch(name):
            with contextlib.suppress(HostError):
                out.append(_load_stage(service, name))
    return out


# -- the share records (no token, ever) --------------------------------------------------------------------------------


def _records(service: WorkspaceService) -> list[dict[str, Any]]:
    doc = _read_json(service, _base(service) / "shares.json")
    rows = doc.get("shares") if isinstance(doc, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _save_records(service: WorkspaceService, rows: list[dict[str, Any]]) -> None:
    _private(service)
    _put_json(_base(service) / "shares.json", {"v": 1, "shares": rows[-MAX_RECORDS:]})


# -- uploads (a file picked on this computer, P05) ---------------------------------------------------------------------


def _upload_dir(service: WorkspaceService, upload_id: str) -> Path:
    if _UPLOAD_ID.fullmatch(upload_id or "") is None:
        raise HostError("not_found", "no such picked file")
    return _base(service) / "upload" / upload_id


def _load_upload(service: WorkspaceService, upload_id: str, by: str) -> dict[str, Any]:
    """A pick's record, only for the person who made it (another person's pick is as good as absent)."""
    meta = _read_json(service, _upload_dir(service, upload_id) / "meta.json")
    if (
        not isinstance(meta, dict)
        or meta.get("upload_id") != upload_id
        or meta.get("ws") != service.workspace_id
        or meta.get("by") != by
        or not isinstance(meta.get("size"), int)
        or not isinstance(meta.get("sha256"), str)
    ):
        raise HostError("not_found", "a picked file is gone: pick it again")
    try:
        approval.check_name(meta["name"])
    except (ValueError, KeyError, TypeError):
        raise HostError("not_found", "a picked file is gone: pick it again") from None
    return meta


def _upload_read(service: WorkspaceService, meta: dict[str, Any], room: int) -> bytes:
    """A picked file, opened without following a link and held to its recorded size and hash (like a stage file)."""
    try:
        fd = os.open(
            _upload_dir(service, meta["upload_id"]) / "data", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        )
    except OSError:
        raise HostError("not_found", "a picked file is gone: pick it again") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != service.host_uid
            or info.st_size != meta["size"]
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise HostError("not_found", "a picked file changed: pick it again")
        if info.st_size > room:
            raise HostError("quota.host", "the staged copies would use too much room: discard some")
        data = handle.read(meta["size"] + 1)
    if len(data) != meta["size"] or hashlib.sha256(data).hexdigest() != meta["sha256"]:
        raise HostError("not_found", "a picked file changed: pick it again")
    return data


def _uploads(service: WorkspaceService) -> list[tuple[float, str]]:
    root = _base(service) / "upload"
    out = []
    with contextlib.suppress(OSError):
        for name in os.listdir(root):
            if _UPLOAD_ID.fullmatch(name):
                with contextlib.suppress(OSError):
                    out.append((os.lstat(root / name).st_mtime, name))
    return sorted(out)


def _stage_upload(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    """``publish.stage_upload {name, b64}``: a person's pick from this computer, kept aside (0700, 0600) and hashed.
    Nothing is shared: the file goes into the share only when ``publish.prepare`` names it, and it is the stage's copy
    that the person signs. Column P only; an agent never adds bytes of its own to a share."""
    service = call.service
    _person(call)
    _gate(call)
    try:
        name = approval.check_name(args["name"])
    except ValueError as exc:
        raise HostError("invalid.input", f"{args['name'][:60]!r}: {exc}"[:300]) from None
    try:
        data = base64.b64decode(args["b64"], validate=True)
    except (binascii.Error, ValueError):
        raise HostError("invalid.input", "the file is not valid base64") from None
    if not data:
        raise HostError("invalid.input", f"{name} is empty")
    if len(data) > UPLOAD_BYTES:
        raise HostError("quota.host", f"a file picked here is at most {approval.human_size(UPLOAD_BYTES)}")
    _rate(call, "upload", 30)
    with _MU:
        _private(service, "upload")
        now = service.clock()
        pending = _uploads(service)
        for mtime, old in pending:  # stale ones go; at the cap the oldest makes room
            if now - mtime > STAGE_KEEP:
                shutil.rmtree(_upload_dir(service, old), ignore_errors=True)
        mine = []  # the cap is per person: one person's picks never push out another's
        for mtime, old in _uploads(service):
            meta = _read_json(service, _upload_dir(service, old) / "meta.json")
            if isinstance(meta, dict) and meta.get("by") == str(call.viewer):
                mine.append((mtime, old))
        for _, old in mine[: max(0, len(mine) - MAX_UPLOADS + 1)]:
            shutil.rmtree(_upload_dir(service, old), ignore_errors=True)
        upload_id = "pu_" + secrets.token_hex(16)
        digest = hashlib.sha256(data).hexdigest()
        tmp = _base(service) / "upload" / f".new-{secrets.token_hex(8)}"
        try:
            os.mkdir(tmp, 0o700)
            _write(tmp / "data", data)
            _put_json(
                tmp / "meta.json",
                {
                    "v": 1,
                    "upload_id": upload_id,
                    "ws": service.workspace_id,
                    "name": name,
                    "size": len(data),
                    "sha256": digest,
                    "by": str(call.viewer),
                },
            )
            os.rename(tmp, _upload_dir(service, upload_id))
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    _audit(call, "publish.stage_upload", upload=upload_id, size=len(data))
    return {"upload_id": upload_id, "name": name, "size": len(data), "sha256": digest}


# -- prepare -----------------------------------------------------------------------------------------------------------


def _ticket(call: Call, ref: str) -> Any:
    from .visibility import Viewer

    return Viewer.of(call).ticket(call.service.store, ref)


def _artifact_bytes(call: Call, view: Any, name: str, room: int) -> tuple[bytes, str]:
    """One artifact of the ticket, by the ticket manifest and its digest (as ``artifact.get``): opened without following
    a link, one hard link, a regular file, hashed to the digest the log names."""
    from .visibility import Viewer

    art = Viewer.artifact(view, name)
    if not art.digest:
        raise HostError("not_found", "no such artifact on this ticket")
    service = call.service
    path = artifact_file(service, view.uid, art.name)
    try:
        fd = service.capabilities().files.open_fd(path, os.O_RDONLY)
    except OSError:
        raise HostError("not_found", f"the file of artifact {art.name} is not there") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if info.st_size > approval.LIMITS["file_bytes"]:
            raise HostError("quota.host", f"{art.name} is over {approval.human_size(approval.LIMITS['file_bytes'])}")
        if info.st_size > room:  # before a byte is read: the stage cap (workspace) and the share cap
            raise HostError("quota.host", "the staged copies would use too much room: discard some")
        data = handle.read(approval.LIMITS["file_bytes"] + 1)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if digest != art.digest:
        raise HostError("not_found", f"the file of artifact {art.name} does not match the digest the ticket records")
    return data, digest.removeprefix("sha256:")


def _work_files(
    call: Call, view: Any, names: list[str], files: list[dict[str, Any]], blobs: list[bytes], used: int, total: int
) -> None:
    """P03: files of the ticket's own work folder, by plain name. The host resolves each below
    ``<project>/.orch-work/<KEY>`` through descriptors (no link followed, no path), checks the name like any share
    file, and reads and hashes the bytes now; the stage's copy is what the person signs."""
    from orch.layout import LayoutError, workdir_read
    from orch.ops.workdir import has_codebase

    service = call.service
    if has_codebase(service.store, view):
        raise HostError("invalid.input", f"{view.key} has a codebase: it has no work folder")
    for name in names:
        try:
            approval.check_name(name)
        except ValueError as exc:
            raise HostError("invalid.input", f"{name[:60]!r}: {exc}"[:300]) from None
        room = min(STAGE_BYTES - used - total, approval.LIMITS["share_bytes"] - total, approval.LIMITS["file_bytes"])
        try:
            data = workdir_read(service.store.layout, view.key, name, max(room, 0))
        except LayoutError as exc:
            raise HostError("not_found", str(exc)[:300]) from None
        if not data:
            raise HostError("invalid.input", f"{name} is empty")
        total += len(data)
        if total > approval.LIMITS["share_bytes"]:
            raise HostError(
                "quota.host", f"a share holds at most {approval.human_size(approval.LIMITS['share_bytes'])}"
            )
        files.append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        blobs.append(data)


def _claim_ok(call: Call, view: Any) -> bool:
    from .worktrees import _claim_ok as claim_ok

    return claim_ok(call, view)


def _prepare(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    view = _ticket(call, args["ref"])
    if not _claim_ok(call, view):
        raise HostError("op.not_allowed", "an agent prepares shares only on the ticket its session claims")
    _gate(call)
    cfg = _config(service)
    _hub_route(service)  # a hub that cannot carry the share is said now, before anything is copied or signed
    kind = args.get("kind", "secret")
    days = args.get("days", 7)
    views = args.get("views", 1 if kind == "once" else 0)
    if (kind == "once") != (views != 0) or (kind == "secret" and "views" in args):
        raise HostError("invalid.input", "views belong to a show-once link only")
    files: list[dict[str, Any]] = []
    blobs: list[bytes] = []
    total = 0
    stages = _stages(service)
    if call.column.name != "P" and sum(1 for m in stages if m.get("origin") == "agent") >= AGENT_STAGES:
        raise HostError("quota.host", f"an agent may have {AGENT_STAGES} staged copies at most: ask a person")
    if len(stages) >= MAX_STAGED:
        raise HostError("quota.host", f"at most {MAX_STAGED} staged copies wait at once: discard some")
    used = sum(f["size"] for m in stages for f in m.get("files", []))
    names = list(args.get("artifacts", []))
    upload_ids = list(args.get("uploads", []))
    work_names = list(args.get("workfiles", []))
    if not names and not upload_ids and not work_names:
        raise HostError("invalid.input", "publish.prepare: pick at least one file")
    if work_names and call.column.name != "P":
        raise HostError("op.not_allowed", "only a person adds a file from the ticket's work folder")
    if upload_ids and call.column.name != "P":
        raise HostError("op.not_allowed", "only a person adds a file from this computer")
    picked = [_load_upload(service, u, str(call.viewer)) for u in upload_ids]
    lowered = [n.lower() for n in [*names, *work_names, *(m["name"] for m in picked)]]
    if len(set(lowered)) != len(lowered):
        raise HostError(
            "invalid.input", "two file names differ only in upper and lower case: the server would merge them"
        )
    if len(lowered) > approval.LIMITS["files"]:
        raise HostError("invalid.input", f"a share holds at most {approval.LIMITS['files']} files")
    for name in names:
        try:
            approval.check_name(name)
        except ValueError as exc:
            raise HostError("invalid.input", f"{name[:60]!r}: {exc}"[:300]) from None
        room = min(STAGE_BYTES - used - total, approval.LIMITS["share_bytes"] - total)
        data, digest = _artifact_bytes(call, view, name, room)
        total += len(data)
        if total > approval.LIMITS["share_bytes"]:
            raise HostError(
                "quota.host", f"a share holds at most {approval.human_size(approval.LIMITS['share_bytes'])}"
            )
        files.append({"name": name, "size": len(data), "sha256": digest})
        blobs.append(data)
    if work_names:
        _work_files(call, view, work_names, files, blobs, used, total)
        total = sum(f["size"] for f in files)
    for meta_up in picked:  # the picked files: read and hashed again now; the stage's hash is what is signed
        room = min(STAGE_BYTES - used - total, approval.LIMITS["share_bytes"] - total)
        data = _upload_read(service, meta_up, room)
        total += len(data)
        files.append({"name": meta_up["name"], "size": len(data), "sha256": meta_up["sha256"]})
        blobs.append(data)
    _rate(call, "prepare")
    _sweep(service)
    origin = "person" if call.column.name == "P" else "agent"
    staged_id = "ps_" + secrets.token_hex(16)
    meta = {
        "v": 1,
        "staged_id": staged_id,
        "ws": service.workspace_id,
        "ticket": {"uid": view.uid, "key": view.key},
        "kind": kind,
        "days": days,
        "views": views,
        "origin": origin,
        "created": service.clock(),
        "files": files,
        "share_id": secrets.token_hex(16),
        "by": str(call.viewer),
        # display members of the server's list (#1636): from the host's own records, never from the request
        "label": _display(view.title, 80) or view.key,
        "by_kind": origin,
        "by_name": _by_name(call),
    }
    _private(service, "stage")
    if len(_stages(service)) >= MAX_STAGED:
        raise HostError("quota.host", f"at most {MAX_STAGED} staged copies wait at once: discard some")
    tmp = _base(service) / "stage" / f".new-{secrets.token_hex(8)}"
    try:
        os.mkdir(tmp, 0o700)
        os.mkdir(tmp / "files", 0o700)
        for entry, data in zip(files, blobs, strict=True):
            _write(tmp / "files" / entry["name"], data)
        text = _text(service, meta, cfg)  # the words exist and are valid before anything is kept
        _put_json(tmp / "meta.json", meta)
        os.rename(tmp, _stage_dir(service, staged_id))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    for meta_up in picked:  # taken into the stage: the pending copy is gone
        shutil.rmtree(_upload_dir(service, meta_up["upload_id"]), ignore_errors=True)
    _audit(call, "publish.prepare", ticket=view.key, staged=staged_id, files=len(files), kind=kind, origin=origin)
    return {
        "staged_id": staged_id,
        "ticket": view.key,
        "kind": kind,
        "days": days,
        "views": views,
        "origin": origin,
        "files": files,
        "server": cfg["server"],
        "share": cfg["share"],
        "needs": "a person's signature",
        "words": text,
    }


# -- the signature -----------------------------------------------------------------------------------------------------


def facts_for(service: WorkspaceService, args: dict[str, Any], call: Call) -> approval.ApprovalFacts:
    """``human.prepare {op: publish.share}``: the staged copy's own words, built here from the stage and the signed
    server setting (never from the request, which names only the staged id). The stage is read and hashed now."""
    _validate("publish.share", args)
    if call.column.name != "P":
        raise HostError("op.not_allowed", "only a person signs a share")
    _gate(call)
    cfg = _config(service)
    meta = _load_stage(service, args["staged_id"])
    _verify_stage(service, meta)
    view = _ticket(call, meta["ticket"]["uid"])
    _may_share(call, view)
    _hub_route(service)  # refuse BEFORE the person signs when the hub path is not usable: no signature for nothing
    return approval.ApprovalFacts(meta["staged_id"], view.key, _text(service, meta, cfg))


def _may_share(call: Call, view: Any) -> None:
    """Who may sign a share: the ticket owner, workspace owners and maintainers (as ``policy.set`` on a ticket)."""
    from orch.model import claims

    core = call.service.store.state._core
    if call.viewer is None or not claims.may_manage(core.ws, core.tickets[view.uid], call.viewer):
        raise HostError("role.denied", "sharing is for the ticket owner, owners and maintainers")


def _signed_approval(call: Call, meta: dict[str, Any], text: str) -> bool:
    """Is there a ``log.added`` note on the ticket, signed by this person (actor kind person), with exactly ``text``,
    not older than :data:`APPROVAL_VALID`? Read from the verified log, newest first."""
    store = call.service.store
    uid = meta["ticket"]["uid"]
    head = store.log_seq(uid)
    if head is None:
        return False
    now = call.service.clock()
    top = head
    while top > 0:  # newest first, in chunks, until the notes are older than a signature stays good
        low = max(top - SCAN_CHUNK, 0)
        for ev in reversed(store.events(uid, after=low, limit=top - low)):
            at = _epoch(ev.get("at"))
            if at is not None and now - at > APPROVAL_VALID:
                return False
            if ev.get("type") != "log.added" or ev.get("text") != text:
                continue
            actor = ev.get("actor") or {}
            if ev.get("auth") == "hub" or "via" in actor:  # a signature made at this computer only, never over a hub
                continue
            if (
                actor.get("kind") == "person"
                and actor.get("id") == call.viewer
                and at is not None
                and 0 <= now - at <= APPROVAL_VALID
                and ev.get("sig")
            ):
                return True
        top = low
    return False


# -- the worker half ---------------------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    """The worker half. ``prepare``, ``list`` (local) and ``discard`` answer here; ``send``, ``revoke``, ``register``
    answer a plan that :func:`execute` runs off the worker (the network), with the signature and gate checked here."""
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    _validate(op, args)
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    opts = call.frame.get("opts") or {}
    if opts.get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    if op in WRITE_OPS and service.state == "degraded":
        raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    service.store.refresh()
    if op == "publish.stage_upload":
        return ok_result(_stage_upload(call, args))
    if op == "publish.prepare":
        return ok_result(_prepare(call, args))
    if op == "publish.list":
        return _list(call, args)
    if op == "publish.discard":
        return ok_result(_discard(call, args))
    return {"plan": _plan(call, op, args)}


def _plan(call: Call, op: str, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    person = _person(call)
    if op == "publish.register":
        _owner(call)
    _gate(call)
    cfg = _config(service)
    plan: dict[str, Any] = {"op": op, "cfg": cfg, "person": person}
    if op == "publish.register":
        _rate(call, "register", REGISTER_PER_MINUTE)
        plan["reg"] = _delegation(call)
        plan["hub"] = _hub_route(service)
        _plan_card(call, plan)
        return plan
    if op == "publish.revoke":
        rec = next((r for r in _records(service) if r.get("id") == args["share_id"]), None)
        if rec is None:
            raise HostError("not_found", "no such share")
        _ticket(call, rec["ticket"])  # a ticket the person may not see is not theirs to act on
        _rate(call, "revoke")
        plan["record"] = rec
        plan["hub"] = _hub_route(service)
        return plan
    if op in ("publish.extend", "publish.password"):
        return _plan_link(call, plan, op, args)
    meta = _load_stage(service, args["staged_id"])
    view = _ticket(call, meta["ticket"]["uid"])
    _may_share(call, view)
    if view.uid != meta["ticket"]["uid"]:
        raise HostError("publish.changed", "the staged copy belongs to another ticket")
    text = _text(service, meta, cfg)
    if not _signed_approval(call, meta, text):
        raise HostError(
            "publish.not_approved",
            "a person must sign this share first (orch publish share " + meta["staged_id"] + ") and the signature "
            "must match this staged copy and this server exactly",
        )
    _rate(call, "send")
    plan.update(meta=meta, text_hash=hashlib.sha256(text.encode()).hexdigest(), hub=_hub_route(service))
    registered = _registered(service, cfg) is not None
    try:  # P05: the card is at hand even when registered, so a server that lost the workspace is registered again once
        plan["reg"] = _delegation(call)
        _plan_card(call, plan)
    except HostError:
        if not registered:
            raise
        plan.pop("reg", None)
    plan["registered"] = registered
    return plan


def _plan_link(call: Call, plan: dict[str, Any], op: str, args: dict[str, Any]) -> dict[str, Any]:
    """``publish.extend`` / ``publish.password`` on a link the host itself shared (M4 S2, #1453). Column P only (the
    caller ran :func:`_person`). Extending a link makes it reachable for longer, so it needs what sharing needs: the
    ticket owner, a workspace owner or a maintainer (:func:`_may_share`). A password replaces the one in use, so
    it needs the same. Neither op takes a password or a record from the caller."""
    rec = next((r for r in _records(call.service) if r.get("id") == args["share_id"]), None)
    if rec is None:
        raise HostError("not_found", "no such share")
    view = _ticket(call, rec["ticket"])  # a ticket the person may not see is not theirs to act on
    if rec.get("state") != "live":
        raise HostError("publish.changed", "this link is not live: share the files again")
    _may_share(call, view)  # extend and password alike: seeing the ticket is not enough to change a live link
    if op == "publish.extend":
        _rate(call, "extend")
        plan["days"] = args["days"]
    else:
        _rate(call, "password")
        plan["password"], plan["pw"] = _new_password()  # drawn here, in the host; the plain text never leaves the call
    plan["record"] = rec
    plan["hub"] = _hub_route(call.service)
    return plan


_CONSONANTS, _VOWELS = "bdfgklmnprstvz", "aeiou"


def _new_password() -> tuple[str, dict[str, Any]]:
    """A generated password (never typed, never chosen by a caller) and its C5 record. Five words of two pronounceable
    syllables each, about 61 bits; the record is scrypt n=32768 r=8 p=1 with a 16-byte salt (contracts/hub/protocol.md
    11.4.1). Only the record is sent to orch-publish."""
    from orch.publishclient import approval as ap

    words = [
        "".join(secrets.choice(_CONSONANTS) + secrets.choice(_VOWELS) for _ in range(2)) for _ in range(PASSWORD_WORDS)
    ]
    password = "-".join(words)
    return password, ap.password_record(password, secrets.token_bytes(16))


def _plan_card(call: Call, plan: dict[str, Any]) -> None:
    """In hub mode the workspace reaches orch-publish as its v2 card through the hub (``card_put``, V3-D36): read it
    here, on the worker, for the network half."""
    from . import hubops

    card = hubops.workspace_card(call.service)
    if card is None:
        raise HostError("hub.no_card", "this workspace's directory card could not be had")
    plan["card"] = card


def _hub_route(service: WorkspaceService, *, required: bool = True) -> dict[str, Any] | None:
    """The hub this workspace publishes through. A workspace without a hub is local only (D69, V3-D36): there is no
    direct path, so a standalone workspace is refused here (``required``) or gets ``None`` (``required=False``, a read
    that degrades). Worker thread."""
    from . import hubops

    route = hubops.publish_route(service)
    if route is None and required:
        raise HostError(
            "hub.not_joined",
            "Publishing needs a hub, and this workspace has not joined one. Join a hub in Settings, Hub, then try "
            "again.",
        )
    return route


def _discard(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    meta = _load_stage(service, args["staged_id"])
    view = _ticket(call, meta["ticket"]["uid"])
    if call.column.name != "P" and (meta["origin"] != "agent" or not _claim_ok(call, view)):
        raise HostError("op.not_allowed", "an agent discards only the copies it prepared, on its own ticket")
    _gate(call)
    if os.path.lexists(_stage_dir(service, meta["staged_id"]) / "send.json"):
        raise HostError(
            "publish.changed",
            "this copy was already being sent and may be live on the server: revoke the share "
            f"{meta['share_id']} (orch publish revoke), then discard",
        )
    with _MU:
        if (service.workspace_id, meta["staged_id"]) in _SENDING:
            raise HostError("lock.busy", "this copy is being sent", retry_after_ms=1000)
        shutil.rmtree(_stage_dir(service, meta["staged_id"]), ignore_errors=True)
    _audit(call, "publish.discard", ticket=view.key, staged=meta["staged_id"])
    return {"discarded": True}


# -- list --------------------------------------------------------------------------------------------------------------


def _row(rec: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "ticket", "kind", "state", "expires_at", "view_limit", "views", "files", "created_at", "server")
    return {**{k: rec.get(k) for k in keys}, "has_password": rec.get("has_password") is True}


def _list(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    from .visibility import Viewer
    from .workspace import ok_result

    service = call.service
    _gate(call)
    viewer = Viewer.of(call)
    wanted = _ticket(call, args["ref"]).key if args.get("ref") else None
    store = service.store
    shares = []
    for rec in _records(service):
        view = store.ticket(rec.get("ticket", ""))
        if view is None or not viewer.sees(view) or (wanted is not None and view.key != wanted):
            continue
        if call.column.name == "G" and not _claim_ok(call, view):
            continue
        if rec.get("state") == "failed":  # a refused send leaves nothing to show (P02)
            continue
        shares.append(_row({**rec, "ticket": view.key}))
    staged = []
    for meta in _stages(service):
        view = store.ticket(meta["ticket"]["uid"])
        if view is None or not viewer.sees(view) or (wanted is not None and view.key != wanted):
            continue
        if call.column.name == "G" and not _claim_ok(call, view):
            continue
        staged.append(
            {k: meta[k] for k in ("staged_id", "kind", "days", "views", "origin", "files")}
            | {"ticket": view.key, "created_at": _iso(meta["created"])}
        )
    ws = store.state.workspace
    from orch.store.render import thaw

    cfg = thaw(ws.settings.get("publish"))
    out: dict[str, Any] = {
        "configured": isinstance(cfg, dict),
        "server": cfg.get("server") if isinstance(cfg, dict) else None,
        "share": cfg.get("share") if isinstance(cfg, dict) else None,
        "workspace": service.workspace_id,
        "owner": ((store.genesis_event() or {}).get("delegation", {}).get("o", {}) or {}).get("owner_person_id"),
        "shares": shares,
        "staged": staged,
    }
    if args.get("live"):
        if call.column.name != "P":
            raise HostError("op.not_allowed", "a person looks at the server's numbers")
        if not out["configured"]:
            # No server is set up yet: there is nothing to ask. The local answer says so (`configured` false, `live`
            # false) instead of failing, so a page can tell the person what to do next.
            return ok_result({**out, "live": False})
        hub = _hub_route(service, required=False)
        if hub is None:  # local only: there is no server to ask
            return ok_result({**out, "live": False})
        return {"plan": {"op": "publish.list", "cfg": _config(service), "out": out, "hub": hub}}
    return ok_result(out)


# -- the registration -------------------------------------------------------------------------------------


def _registered(service: WorkspaceService, cfg: dict[str, str]) -> dict[str, Any] | None:
    doc = _read_json(service, _base(service) / "registered.json")
    ok = isinstance(doc, dict) and doc.get("server") == cfg["server"] and doc.get("workspace") == service.workspace_id
    return doc if ok else None


def _delegation(call: Call) -> dict[str, Any]:
    """The body of ``register`` (design §2.6b): exactly the genesis ``delegation`` and the owner's ``pk_pub``, copied
    from the workspace's pinned genesis, checked here with ``members.check_genesis`` first. No card, no WXK or CK, no
    relay URL: the request itself is signed with the publish label, so it is useless at the relay. Runs on the
    worker: it reads the store."""
    from orch.identity import members
    from orch.identity.errors import Refused

    genesis = call.service.store.genesis_event()
    try:
        facts = members.check_genesis(genesis) if genesis is not None else None
    except Refused:
        facts = None
    if facts is None:
        raise HostError(
            "publish.server_refused", "the workspace genesis could not be read", detail={"reason": "delegation"}
        )
    return {
        "delegation": genesis["delegation"],
        "owner_pk": genesis["owner"]["pk_pub"],  # byte for byte; check_genesis has verified it is the delegating key
    }


def _client(service: WorkspaceService, origin: str, hub: dict[str, Any]) -> Any:
    """A :class:`PublishClient` for ``origin`` (the signed server setting) that sends every request through the hub link
    (``publish_out``, hub contract 11), signed with ``WSK`` under the hub-path label. There is no other path (V3-D36).
    No ledger: the server's nonce store already refuses a replay."""
    from orch.custody import FileBackend
    from orch.wsproto.pubsign import PublishContext
    from orch.wsproto.sign import SignerRefused

    keys_dir = Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "keys"
    backend = FileBackend(keys_dir) if keys_dir.is_dir() else None
    if backend is None or not backend.exists("wsk"):
        raise HostError("workspace.unavailable", "the workspace signing key is not in the host's custody")
    try:
        return _hub_client(service, PublishContext(service.workspace_id, origin), hub)
    except SignerRefused:
        raise HostError(
            "publish.server_refused", "the server address is not allowed", detail={"reason": "rejected"}
        ) from None


#: the hub links this worker thread opened for one network op (one link per hub, so its message numbers count up)
_LINKS = threading.local()


def _open_link(service: WorkspaceService, route: dict[str, Any]) -> tuple[Any, Any, bytes, Any]:
    from . import hubops

    links = _LINKS.__dict__.setdefault("open", {})
    if route["hub_id"] not in links:
        links[route["hub_id"]] = hubops.publish_link(service, route)
    return links[route["hub_id"]]  # type: ignore[no-any-return]


def _hub_client(service: WorkspaceService, ctx: Any, route: dict[str, Any]) -> Any:
    from orch.hubclient.publish import HubPublishSigner, HubPublishTransport
    from orch.publishclient import PublishClient

    hub_link, _transport, wsk_pub, sign = _open_link(service, route)
    signer = HubPublishSigner(public_key=wsk_pub, sign=sign, ctx=ctx, hub_id=route["hub_id"])
    return PublishClient(signer, transport=HubPublishTransport(hub_link))


def _close_hub_links() -> None:
    for entry in getattr(_LINKS, "open", {}).values():
        with contextlib.suppress(Exception):
            getattr(entry[1], "close", lambda: None)()
    _LINKS.open = {}


def _hub_error(exc: Any) -> HostError:
    """A hub refusal on the publish path as the host's own error. The hub being down is ``hub.unreachable``; nothing is
    sent any other way."""
    from . import hubops

    reasons = {  # hub code -> (closed detail reason, text); the reasons are the same set as a direct refusal's
        "unknown_workspace": (
            "unknown_workspace",
            "the sharing server does not know this workspace yet: ask the person who runs it to allow it",
        ),
        "owner_not_allowed": ("owner_not_allowed", "the server's admin has not allowed this owner"),
        "workspace_blocked": ("blocked", "the server's admin blocked this workspace"),
        "delegation_changed": (
            "conflict",
            "the server holds another delegation for this workspace: its admin must unbind it",
        ),
        "stale_card": ("rejected", "the server holds a newer card of this workspace than this one"),
    }
    for bad in ("other_person", "other_workspace", "delegation_mismatch", "delegation_refused", "bad_signature",
                "malformed", "wrong_suite"):  # fmt: skip
        reasons[bad] = ("rejected", f"the server refused this workspace's card ({bad})")
    if exc.code in reasons:
        reason, text = reasons[exc.code]
        return HostError("publish.server_refused", text, detail={"reason": reason})
    if exc.code == "unavailable":  # the hub could not reach orch-publish (or its answer was lost)
        return HostError(
            "publish.unreachable", "the publish server could not be reached; nothing was shared", retry_after_ms=2000
        )
    if exc.code == "too_large":
        return HostError(
            "publish.server_refused",
            "this is too large to send through the hub link yet; nothing was shared",
            detail={"reason": "limit"},
        )
    return hubops._refused(exc.code)  # noqa: SLF001 - the one mapping of hub codes


def _refused(exc: Any) -> HostError:
    """A :class:`PublishError` as the host's own error: a closed reason, the server's short code, never a body."""
    code = str(getattr(exc, "code", "rejected"))
    status = int(getattr(exc, "status", 0))
    if getattr(exc, "clock_off", False):
        reason, text = "clock", "this computer's clock is off by more than the server allows: fix the time and retry"
    elif code == "network" or (status == 0 and code != "refused"):
        return HostError(
            "publish.unreachable", "the publish server could not be reached; nothing was shared", retry_after_ms=2000
        )
    elif code == "delegation_changed":
        reason, text = "conflict", "the server holds another delegation for this workspace: its admin must unbind it"
    elif code == "unknown_workspace":
        reason, text = (
            "unknown_workspace",
            "the sharing server no longer knows this workspace; ask the person who runs it to allow it again",
        )
    elif code == "workspace_blocked":
        reason, text = "blocked", "the server's admin blocked this workspace"
    elif code == "owner_not_allowed":
        reason, text = "owner_not_allowed", "the server's admin has not allowed this owner"
    elif code in ("too_large", "quota_exceeded", "share_too_large", "too_many_shares", "rate_limited"):
        reason, text = "limit", f"the server refused: over a limit ({code})"
    elif status == 409:
        reason, text = "conflict", f"the server refused: {code}"
    else:
        reason, text = "rejected", f"the server refused ({code})"
    return HostError("publish.server_refused", text, detail={"reason": reason})


# -- the event-loop half -----------------------------------------------------------------------------------------------


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    """Run a plan's network half in a thread, off the workspace worker; the answer is the op's result."""
    if "plan" not in planned:
        return planned
    plan = planned["plan"]
    return await asyncio.to_thread(_run, call, plan)


def _run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch.hubclient import HubRefused
    from orch.publishclient import PublishError

    from .workspace import ok_result

    try:
        if plan["op"] == "publish.register":
            return ok_result(_register(call, plan))
        if plan["op"] == "publish.revoke":
            return ok_result(_revoke(call, plan))
        if plan["op"] == "publish.extend":
            return ok_result(_extend(call, plan))
        if plan["op"] == "publish.password":
            return ok_result(_set_password(call, plan))
        if plan["op"] == "publish.list":
            return ok_result(_live(call, plan))
        return ok_result(_send(call, plan))
    except PublishError as exc:
        if exc.code == "unknown_workspace":  # the cached "registered" is stale: register again next time
            with contextlib.suppress(OSError):
                os.unlink(_base(call.service) / "registered.json")
        err = _refused(exc)
        _audit(
            call,
            "publish.refused",
            op=plan["op"],
            code=err.code,
            reason=(err.detail or {}).get("reason"),
            status=getattr(exc, "status", 0),
        )
        raise err from None
    except HubRefused as exc:
        if exc.code == "unknown_workspace":  # as above: the cached "registered" is stale
            with contextlib.suppress(OSError):
                os.unlink(_base(call.service) / "registered.json")
        err = _hub_error(exc)
        _audit(call, "publish.refused", op=plan["op"], code=err.code, reason=exc.code, status=0, via="hub")
        raise err from None
    finally:
        _close_hub_links()


def _register(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    cfg = plan["cfg"]
    reg = plan["reg"]
    from orch.hubclient.publish import card_put

    # the card goes to the hub, which hands it to orch-publish (V3-D36: never a direct call)
    got = card_put(_open_link(service, plan["hub"])[0], plan["card"], reg["owner_pk"])
    with _MU:
        _private(service)
        _put_json(
            _base(service) / "registered.json",
            {"server": cfg["server"], "workspace": service.workspace_id, "at": _iso(service.clock())},
        )
    _audit(call, "publish.register", server=cfg["server"], result=str(got.get("result"))[:20])
    return {"registered": True, "server": cfg["server"], "result": str(got.get("result", "registered"))[:20]}


def _token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _send(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch.hubclient import HubRefused
    from orch.publishclient import PublishError, flow

    service = call.service
    cfg, meta = plan["cfg"], plan["meta"]
    staged = meta["staged_id"]
    key = (service.workspace_id, staged)
    with _MU:
        if key in _SENDING:
            raise HostError("lock.busy", "this copy is already being sent", retry_after_ms=1000)
        _SENDING.add(key)
    try:
        # the staged files are read and hashed right before they go; the bytes returned are the bytes sent (I1)
        blobs = _verify_stage(service, meta)
        sdir = _stage_dir(service, staged)
        token_path, send_path = sdir / "token", sdir / "send.json"
        send = _read_json(service, send_path)
        if isinstance(send, dict) and os.path.lexists(token_path):  # a retry: the same token_hash, the same meta
            token = _read_token(service, token_path)
            if flow.token_hash(token) != send.get("token_hash"):
                raise HostError(
                    "publish.changed", "the stored token does not match: discard this copy and prepare again"
                )
        else:
            token = _token()
            send = {
                "token_hash": flow.token_hash(token),
                "expires_ms": _now_ms(service) + meta["days"] * 86_400_000,
            }
            _write(token_path, token.encode())
            _put_json(send_path, send)
        client = _client(service, cfg["server"], plan["hub"])
        meta_args = {
            "ticket": meta["ticket"]["key"],
            "kind": meta["kind"],
            "expires_ms": send["expires_ms"],
            "view_limit": meta["views"],
            "token_hash_hex": send["token_hash"],
            "files": meta["files"],
            "label": meta.get("label"),
            "by_kind": meta.get("by_kind"),
            "by_name": meta.get("by_name"),
        }
        body = flow.meta_bytes(**meta_args)
        rec = {
            "id": meta["share_id"],
            "ticket": meta["ticket"]["uid"],
            "kind": meta["kind"],
            "state": "live",
            "expires_at": _iso(send["expires_ms"] / 1000),
            "view_limit": meta["views"],
            "views": 0,
            "files": [f["name"] for f in meta["files"]],
            "created_at": _iso(service.clock()),
            "server": cfg["server"],
            "origin": meta["origin"],
            "approved_by": plan["person"],
        }
        with _MU:  # a share can go live from here on (a lost commit answer): revoke must find it, with no token in it
            rows = [r for r in _records(service) if r.get("id") != rec["id"]]
            _save_records(service, [*rows, {**rec, "state": "sending"}])
        try:  # P02: a refusal before anything was stored leaves no "Sending" row behind
            if "reg" in plan and not plan.get("registered"):
                _register(call, {**plan, "op": "publish.register"})
            registered_again = relabelled = False
            while True:
                try:
                    flow.put_meta(client, service.workspace_id, meta["share_id"], body)
                    break
                except (PublishError, HubRefused) as exc:
                    if exc.code == "unknown_workspace" and not registered_again and "reg" in plan:
                        registered_again = True
                        _register(call, {**plan, "op": "publish.register"})  # the server lost the binding: once more
                        continue
                    if exc.code == "bad_label" and not relabelled:
                        relabelled = True  # an older server refuses a text the contract allows: the ticket key instead
                        body = flow.meta_bytes(**{**meta_args, "label": meta["ticket"]["key"]})
                        continue
                    raise
        except Exception as exc:
            if _refused_outright(exc):
                with _MU:
                    rows = [r for r in _records(service) if r.get("id") != rec["id"]]
                    _save_records(service, _prune_failed([*rows, {**rec, "state": "failed"}], service.clock()))
            raise
        for entry, data in zip(meta["files"], blobs, strict=True):
            flow.put_file(client, service.workspace_id, meta["share_id"], entry["name"], data)
        flow.commit(client, service.workspace_id, meta["share_id"])
        link = f"{cfg['share']}/s/{meta['share_id']}#t={token}"
        with _MU:
            rows = [r for r in _records(service) if r.get("id") != rec["id"]]
            _save_records(service, [*rows, {**rec, "state": "live"}])
        _audit(
            call,
            "publish.share",
            ticket=meta["ticket"]["key"],
            share=rec["id"],
            kind=rec["kind"],
            days=meta["days"],
            files=len(meta["files"]),
            server=cfg["server"],
            origin=meta["origin"],
            via="hub",
        )
        shutil.rmtree(sdir, ignore_errors=True)  # the token goes with the stage: nothing keeps the link
        return {
            "share_id": rec["id"],
            "ticket": meta["ticket"]["key"],
            "kind": rec["kind"],
            "link": link,
            "expires_at": rec["expires_at"],
            "view_limit": rec["view_limit"],
            "shown_once": True,
        }
    finally:
        with _MU:
            _SENDING.discard(key)


FAILED_KEEP, FAILED_DAYS = 50, 7  # a failed send is hidden; its record is kept this long / this many, then dropped


def _prune_failed(rows: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    from datetime import datetime

    def fresh(r: dict[str, Any]) -> bool:
        try:
            return now - datetime.fromisoformat(str(r.get("created_at"))).timestamp() < FAILED_DAYS * 86400
        except ValueError:
            return False

    failed = [r for r in rows if r.get("state") == "failed" and fresh(r)][-FAILED_KEEP:]
    keep = {id(r) for r in failed}
    return [r for r in rows if r.get("state") != "failed" or id(r) in keep]


def _refused_outright(exc: Exception) -> bool:
    """The server said no to the first request (the meta) in so many words, so nothing of this share exists there. A
    timeout, a 5xx or a lost answer proves nothing and keeps the "sending" record (revoke must still find it)."""
    from orch.hubclient import HubRefused
    from orch.publishclient import PublishError

    if isinstance(exc, HostError):  # the register step turns the server's refusal into this closed code
        return exc.code == "publish.server_refused"
    if isinstance(exc, PublishError):
        # 408 (timed out), 409 (it may exist already) and 429 (try later) prove nothing about the share: never "failed"
        return 400 <= exc.status < 500 and exc.status not in (408, 409, 429)
    return isinstance(exc, HubRefused) and getattr(exc, "code", "") in _REFUSED_CODES


_REFUSED_CODES = frozenset(
    {"unknown_workspace", "owner_not_allowed", "workspace_blocked", "not_bound", "unknown_hub", "other_hub", "revoked"}
)


def _read_token(service: WorkspaceService, path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise HostError("publish.changed", "the staged copy changed: prepare the share again") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != service.host_uid or stat.S_IMODE(info.st_mode) & 0o077:
            raise HostError("publish.changed", "the staged copy changed: prepare the share again")
        raw = handle.read(128)
    text = raw.decode("ascii", "replace")
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", text) is None:
        raise HostError("publish.changed", "the staged copy changed: prepare the share again")
    return text


def _revoke(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch.publishclient import PublishError, flow

    service = call.service
    rec = plan["record"]
    client = _client(service, rec.get("server") or plan["cfg"]["server"], plan["hub"])
    try:
        flow.revoke(client, service.workspace_id, rec["id"])
    except PublishError as exc:
        if exc.status not in (404, 410):  # already gone on the server is revoked
            raise
    with _MU:
        rows = [{**r, "state": "revoked"} if r.get("id") == rec["id"] else r for r in _records(service)]
        _save_records(service, rows)
    _audit(call, "publish.revoke", share=rec["id"], ticket=rec.get("ticket"))
    return {"share_id": rec["id"], "state": "revoked"}


def _extend(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch.publishclient import flow

    service = call.service
    rec = plan["record"]
    client = _client(service, rec.get("server") or plan["cfg"]["server"], plan["hub"])
    got = flow.extend(client, service.workspace_id, rec["id"], plan["days"])
    expires_ms = got.get("expires_ms")
    if type(expires_ms) is not int or expires_ms <= 0:
        from orch.publishclient import PublishError

        raise PublishError("bad_reply", 200, method="POST")
    with _MU:
        rows = [
            {**r, "expires_at": _iso(expires_ms / 1000)} if r.get("id") == rec["id"] else r for r in _records(service)
        ]
        _save_records(service, rows)
    _audit(call, "publish.extend", share=rec["id"], ticket=rec.get("ticket"), days=plan["days"])
    return {"share_id": rec["id"], "expires_at": _iso(expires_ms / 1000)}


def _set_password(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    """Send the record; the plain password goes back to this one call and nowhere else (not the log, the audit file,
    the record store or an error)."""
    from orch.publishclient import flow

    service = call.service
    rec = plan["record"]
    client = _client(service, rec.get("server") or plan["cfg"]["server"], plan["hub"])
    flow.set_password(client, service.workspace_id, rec["id"], plan["pw"])
    with _MU:
        rows = [{**r, "has_password": True} if r.get("id") == rec["id"] else r for r in _records(service)]
        _save_records(service, rows)
    _audit(call, "publish.password", share=rec["id"], ticket=rec.get("ticket"))
    return {"share_id": rec["id"], "password": plan["password"], "shown_once": True}


def _live(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    """``publish.list {live}``: the local rows with the server's state and view count. A server that cannot be reached
    leaves the local rows as they are, said plainly."""
    from orch.hubclient import HubRefused
    from orch.publishclient import PublishError, flow

    service = call.service
    out = plan["out"]
    try:
        remote = {
            r.get("id"): r
            for r in flow.list_shares(_client(service, plan["cfg"]["server"], plan["hub"]), service.workspace_id)
        }
    except (PublishError, HubRefused):  # a hub that is down is a server that is down: the local rows, said plainly
        return {**out, "live": False}
    for row in out["shares"]:
        got = remote.get(row["id"])
        if got is not None:
            row["state"], row["views"] = got.get("state", row["state"]), got.get("views", row["views"])
    return {**out, "live": True}

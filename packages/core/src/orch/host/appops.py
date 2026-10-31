"""Shared apps on the host (#1455, HUB-PU-05; the approval is #1457): ``app.prepare``, ``app.send``, ``app.list`` and
``app.discard``; the human op ``app.release`` (through ``human.prepare``/``human.submit``) is the signature that lets
one staged release go live.

**The flow.** An agent or a person ``prepare``s an app (or a page, or a form: static files, no server code) from the
named artifacts of a ticket: the host copies them (by the ticket manifest and digest) into a host-private stage and
builds the version meta (name, kind, who can open it, what it may reach, every file with size and hash). Nothing
leaves the machine and nothing goes live. A **person** then signs ``app.release {staged_id}``: a ``log.added`` note
whose words :func:`orch.publishclient.approval.app_release_text` builds from the stage. ``app.send {staged_id}``
(column P only) builds the same words again from the stage it is about to upload and refuses unless a person's
signed note with exactly these words is in the ticket log (:func:`signed_release`), so the signature covers the
bytes that go. Then the host uploads the version meta and every file, signed with ``WSK`` through the hub, and
records the version.

**Going live.** orch-publish serves a version only after its ``commit`` carries the owner's ``hub.publish_approval``
(contracts/hub/protocol.md 11.3), a ``dk_sig`` signature over that exact request. The click signer signs events, not
that object, so this module stops at "uploaded, not live" and says so; producing the approval object is the follow-up
(see the PR). An uploaded version that is not committed is never served.

**Invariants.** An agent prepares and lists, never signs or sends; the bytes uploaded are the staged bytes, hashed again
as they are read; the stage is 0700 and host-owned; the meta is checked by the same rules as orch-publish's
(``orch.hubclient.published.meta_problem``) before anything is staged.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch.hubclient import published as pub
from orch.publishclient import approval

from . import publishing as P
from .errors import HostError

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["APP_OPS", "NETWORK_OPS", "WRITE_OPS", "execute", "facts_for", "serve", "signed_release"]

APP_OPS: dict[str, int] = {
    "app.prepare": 1, "app.send": 1, "app.list": 1, "app.discard": 1, "app.commit": 1, "app.commit_request": 1,
}  # fmt: skip
NETWORK_OPS = frozenset({"app.send", "app.commit"})
WRITE_OPS = frozenset({"app.prepare", "app.send", "app.discard", "app.commit", "app.commit_request"})
#: a pending commit request (drawn by the host, approved by the person's client) lives this long and is used once
PENDING_TTL = 90.0
_PENDING: dict[tuple[str, str], dict[str, Any]] = {}  # (workspace, request_id) -> the request; guarded by P._MU
MAX_STAGED = 10
AGENT_STAGES = 2
MAX_RECORDS = 200
STAGE_BYTES = 200 * 1024 * 1024
_STAGE_ID = re.compile(r"pa_[0-9a-f]{32}")
#: (workspace, app, version) -> when it was reserved: a send in flight, from the plan until the record is saved. A
#: second send of the same version is refused at once; a reservation older than :data:`INFLIGHT_MAX` (a crash
#: between the plan and the upload) is ignored. Guarded by ``P._MU``.
_INFLIGHT: dict[tuple[str, str, int, str], float] = {}
INFLIGHT_MAX = 900.0
_REF = {"type": "string", "minLength": 1, "maxLength": 80}
_STAGED = {"type": "string", "pattern": r"^pa_[0-9a-f]{32}$"}
_SCHEMAS = {
    "app.prepare": {
        "type": "object",
        "properties": {
            "ref": _REF,
            "name": {"type": "string", "minLength": 1, "maxLength": 60},
            "kind": {"enum": list(pub.KINDS)},
            "access": {"enum": ["public", "company", "group"]},
            "audience": {"type": "string", "minLength": 1, "maxLength": 60},
            "slug": {"type": "string", "minLength": 1, "maxLength": 40},
            "app_id": {"type": "string", "pattern": r"^[0-9a-f]{32}$"},
            "op": {"enum": list(approval.APP_OPS)},
            "version": {"type": "integer", "minimum": 1, "maximum": 2**31 - 1},
            "reach": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": pub.REACH_MAX},
            "artifacts": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "minItems": 1,
                "maxItems": 100,
                "uniqueItems": True,
            },
        },
        "required": ["ref"],
        "additionalProperties": False,
    },
    "app.send": {
        "type": "object",
        "properties": {"staged_id": _STAGED},
        "required": ["staged_id"],
        "additionalProperties": False,
    },
    "app.discard": {
        "type": "object",
        "properties": {"staged_id": _STAGED},
        "required": ["staged_id"],
        "additionalProperties": False,
    },
    "app.list": {"type": "object", "properties": {"ref": _REF}, "additionalProperties": False},
    # a person's client asks for a fresh commit request of a version already sent (the first one timed out)
    "app.commit_request": {
        "type": "object",
        "properties": {
            "app_id": {"type": "string", "pattern": r"^[0-9a-f]{32}$"},
            "op": {"enum": list(approval.APP_OPS)},
            "version": {"type": "integer", "minimum": 1, "maximum": 2**31 - 1},
        },
        "required": ["app_id", "op", "version"],
        "additionalProperties": False,
    },
    "app.commit": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "pattern": r"^cr_[0-9a-f]{32}$"},
            "approval": {"type": "object"},
        },
        "required": ["request_id", "approval"],
        "additionalProperties": False,
    },
    "app.release": {
        "type": "object",
        "properties": {"staged_id": _STAGED},
        "required": ["staged_id"],
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


def _validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


# -- the stage ---------------------------------------------------------------------------------------------------------


def _root(service: WorkspaceService) -> Path:
    return P._base(service) / "appstage"  # noqa: SLF001


def _stage_dir(service: WorkspaceService, staged_id: str) -> Path:
    if _STAGE_ID.fullmatch(staged_id or "") is None:
        raise HostError("not_found", "no such staged release")
    return _root(service) / staged_id


def _load_stage(service: WorkspaceService, staged_id: str) -> dict[str, Any]:
    meta = P._read_json(service, _stage_dir(service, staged_id) / "meta.json")  # noqa: SLF001
    if not isinstance(meta, dict) or meta.get("staged_id") != staged_id or meta.get("ws") != service.workspace_id:
        raise HostError("not_found", "no such staged release")
    return meta


def _sweep(service: WorkspaceService) -> None:
    """Drop staged releases no one finished with (older than :data:`P.STAGE_KEEP`). Never raises."""
    with contextlib.suppress(Exception):
        now = service.clock()
        for name in os.listdir(_root(service)):
            path = _root(service) / name
            if _STAGE_ID.fullmatch(name) and now - os.lstat(path).st_mtime > P.STAGE_KEEP:  # noqa: SLF001
                shutil.rmtree(path, ignore_errors=True)


def _stages(service: WorkspaceService) -> list[dict[str, Any]]:
    out = []
    try:
        names = sorted(os.listdir(_root(service)))
    except OSError:
        return out
    for name in names:
        if _STAGE_ID.fullmatch(name):
            with contextlib.suppress(HostError):
                out.append(_load_stage(service, name))
    return out


def _stage_read(service: WorkspaceService, staged_id: str, entry: dict[str, Any]) -> bytes:
    """One staged file, opened without following a link and held to its recorded size and hash: the bytes returned are
    the bytes that were hashed, so what is uploaded is what was shown."""
    changed = "the staged release changed: prepare it again"
    if pub.file_path_problem(entry.get("path")) or "/" in entry["path"]:
        raise HostError("publish.changed", changed)
    try:
        fd = os.open(
            _stage_dir(service, staged_id) / "files" / entry["path"],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        )
    except OSError:
        raise HostError("publish.changed", changed) from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != service.host_uid
            or info.st_size != entry["size"]
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise HostError("publish.changed", changed)
        data = handle.read(entry["size"] + 1)
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise HostError("publish.changed", changed)
    return data


def _verify_stage(service: WorkspaceService, stage: dict[str, Any]) -> list[bytes]:
    """Every staged file read now and its sha256 recomputed from the bytes (``_stage_read``), and the meta bound to the
    stage's own ticket, before anything trusts ``meta_hash`` or the words: the hashes in the signed text then cover the
    bytes that go, and a stage whose meta names another ticket is never sent."""
    if stage["app"].get("ticket") != stage["ticket"]["uid"]:
        raise HostError("publish.changed", "the staged release is not valid: prepare it again")
    if stage.get("op", "commit") != "commit":  # a rollback or a start sends no file: the server holds the version
        return []
    return [_stage_read(service, stage["staged_id"], f) for f in stage["app"]["files"]]


def _text(service: WorkspaceService, stage: dict[str, Any], cfg: dict[str, str]) -> str:
    try:
        return approval.app_release_text(
            staged_id=stage["staged_id"],
            ticket_key=stage["ticket"]["key"],
            origin=stage["origin"],
            op=stage.get("op", "commit"),
            app_id=stage["app_id"],
            version=stage["version"],
            meta=stage["app"],
            previous=stage.get("previous"),
            server=cfg["server"],
            share=cfg["share"],
        )
    except (ValueError, KeyError, TypeError):
        raise HostError("publish.changed", "the staged release is not valid: prepare it again") from None


# -- the local records (what this computer uploaded; never a secret) ---------------------------------------------------


def _records(service: WorkspaceService) -> list[dict[str, Any]]:
    doc = P._read_json(service, P._base(service) / "apps.json")  # noqa: SLF001
    rows = doc.get("apps") if isinstance(doc, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _save_records(service: WorkspaceService, rows: list[dict[str, Any]]) -> None:
    P._private(service)  # noqa: SLF001
    P._put_json(P._base(service) / "apps.json", {"v": 1, "apps": rows[-MAX_RECORDS:]})  # noqa: SLF001


# -- prepare -----------------------------------------------------------------------------------------------------------


def _files_of(call: Call, view: Any, names: list[str]) -> tuple[list[dict[str, Any]], list[bytes]]:
    files: list[dict[str, Any]] = []
    blobs: list[bytes] = []
    total = 0
    for name in sorted(names):
        room = min(STAGE_BYTES - total, approval.LIMITS["share_bytes"] - total)
        data, digest = P._artifact_bytes(call, view, name, room)  # noqa: SLF001
        total += len(data)
        files.append({"path": name, "size": len(data), "sha256": digest})
        blobs.append(data)
    return files, blobs


def _forbidden(cfg: dict[str, str], route: dict[str, Any] | None) -> tuple[str, ...]:
    """The origins an app may not reach: the publish server, the share origin and the hub (self-reach is refused before
    anybody signs, as orch-publish refuses it at the upload)."""
    hub = (route or {}).get("addrs") or [(route or {}).get("addr")]  # every address the host dials for this hub
    return tuple(x for x in (cfg["server"], cfg["share"], *hub) if isinstance(x, str) and x)


def _fresh(service: WorkspaceService, stage: dict[str, Any]) -> None:
    """The version and the live version the stage was made against are still the record's (``publish.changed``
    otherwise): a second stage of the same app, sent after the first, is refused, and a record that moved on since the
    person signed is never overwritten. Checked again at the signature (``facts_for``) and at the send (``_plan``)."""
    rec = next((r for r in _records(service) if r.get("id") == stage["app_id"]), None)
    prev = (stage.get("previous") or {}).get("version")
    if stage.get("op", "commit") != "commit":  # the recorded version is the staged one and the live one has not moved
        ok = (
            rec is not None
            and rec["versions"].get(str(stage["version"])) == stage["app"]
            and prev is not None
            and rec.get("live") == prev
        )
        if not ok:
            raise HostError("publish.changed", "this app changed since the release was prepared: prepare it again")
        return
    if rec is None:
        ok = stage["version"] == 1 and prev is None
    else:
        ok = str(stage["version"]) not in rec["versions"] and rec.get("live") == prev
    if not ok:
        raise HostError("publish.changed", "this app changed since the release was prepared: prepare it again")


def _prepare(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    view = P._ticket(call, args["ref"])  # noqa: SLF001
    if not P._claim_ok(call, view):  # noqa: SLF001
        raise HostError("op.not_allowed", "an agent prepares apps only on the ticket its session claims")
    P._gate(call)  # noqa: SLF001
    cfg = P._config(service)  # noqa: SLF001
    route = P._hub_route(service)  # noqa: SLF001 - a hub that cannot carry the app is said now, before anything is copied
    op = args.get("op", "commit")
    if op != "commit":
        return _prepare_again(call, args, view, cfg, op)
    for key in ("name", "kind", "access", "artifacts"):
        if key not in args:
            raise HostError("invalid.input", f"app.prepare: {key} is needed")
    if "version" in args:
        raise HostError("invalid.input", "app.prepare: a new version gets its number from the host")
    access = args["access"]
    audience = "company" if access == "company" else args.get("audience")  # a company app has one audience
    if (bad := pub.audience_problem(access, audience)) is not None:
        raise HostError("invalid.input", f"app.prepare: {'a group needs its audience' if bad == 'audience' else bad}")
    rec = None
    if args.get("app_id"):
        rec = next((r for r in _records(service) if r.get("id") == args["app_id"]), None)
        if rec is None:
            raise HostError("not_found", "no such app on this computer")
        if rec.get("ticket") != view.uid:
            raise HostError("invalid.input", "an app is updated from the ticket it was made on")
        slug = rec["slug"]
    elif "slug" in args:
        slug = args["slug"]
    else:
        raise HostError("invalid.input", "app.prepare: a new app needs a slug (its address)")
    if rec is None and any(r.get("slug") == slug for r in _records(service)):
        raise HostError("invalid.input", "an app with this address exists: pass its app_id to update it")
    files, blobs = _files_of(call, view, list(args["artifacts"]))
    meta = {
        "slug": slug, "name": args["name"], "kind": args["kind"], "access": access, "audience": audience,
        "pw": None, "reach": sorted(set(args.get("reach", []))), "files": files,
        "label": P._display(view.title, 80) or view.key, "ticket": view.uid,
    }  # fmt: skip
    if (bad := pub.meta_problem(meta, _forbidden(cfg, route))) is not None:
        raise HostError("invalid.input", f"app.prepare: the app is not valid ({bad})")
    version = 1
    previous = None
    if rec is not None:
        live = rec.get("live")
        version = max((int(v) for v in rec["versions"]), default=0) + 1
        if live is not None and str(live) in rec["versions"]:
            previous = {"version": int(live), "meta": rec["versions"][str(live)]}
    _sweep(service)
    stages = _stages(service)
    origin = "person" if call.column.name == "P" else "agent"
    if origin == "agent" and sum(1 for m in stages if m.get("origin") == "agent") >= AGENT_STAGES:
        raise HostError("quota.host", f"an agent may have {AGENT_STAGES} staged releases at most: ask a person")
    if len(stages) >= MAX_STAGED:
        raise HostError("quota.host", f"at most {MAX_STAGED} staged releases wait at once: discard some")
    P._rate(call, "app.prepare")  # noqa: SLF001
    staged_id = "pa_" + secrets.token_hex(16)
    stage = {
        "v": 1, "staged_id": staged_id, "ws": service.workspace_id, "ticket": {"uid": view.uid, "key": view.key},
        "origin": origin, "created": service.clock(), "app_id": (rec or {}).get("id") or secrets.token_hex(16),
        "version": version, "app": meta, "previous": previous, "by": str(call.viewer),
    }  # fmt: skip
    P._private(service, "appstage")  # noqa: SLF001
    tmp = _root(service) / f".new-{secrets.token_hex(8)}"
    try:
        os.mkdir(tmp, 0o700)
        os.mkdir(tmp / "files", 0o700)
        for entry, data in zip(files, blobs, strict=True):
            P._write(tmp / "files" / entry["path"], data)  # noqa: SLF001
        _text(service, stage, cfg)  # the words exist and are valid before anything is kept
        P._put_json(tmp / "meta.json", stage)  # noqa: SLF001
        os.rename(tmp, _stage_dir(service, staged_id))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    P._audit(call, "app.prepare", ticket=view.key, staged=staged_id, files=len(files), origin=origin)  # noqa: SLF001
    return {
        "staged_id": staged_id, "ticket": view.key, "app_id": stage["app_id"], "version": version, "origin": origin,
        "files": files, "access": access, "reach": meta["reach"], "needs": "a person's signature",
    }  # fmt: skip


def _prepare_again(call: Call, args: dict[str, Any], view: Any, cfg: dict[str, str], op: str) -> dict[str, Any]:
    """``app.prepare {app_id, op: rollback|start, version}``: a stage with no files for a version the server already
    holds. Its meta is the recorded one; its ``previous`` is the live version; its words name the action. A person
    signs it like any release; ``app.send`` then uploads nothing and goes straight to the commit request."""
    service = call.service
    if set(args) - {"ref", "app_id", "op", "version"} or "app_id" not in args or "version" not in args:
        raise HostError("invalid.input", f"app.prepare: {op} takes the app_id and the version only")
    rec = next((r for r in _records(service) if r.get("id") == args["app_id"]), None)
    if rec is None:
        raise HostError("not_found", "no such app on this computer")
    if rec.get("ticket") != view.uid:
        raise HostError("invalid.input", "an app is changed from the ticket it was made on")
    version, live = args["version"], rec.get("live")
    if live is None or str(live) not in rec["versions"] or str(version) not in rec["versions"]:
        raise HostError("invalid.input", "app.prepare: that version, or a live version, is not known here")
    if (op == "rollback" and version >= live) or (op == "start" and version != live):
        raise HostError("invalid.input", f"app.prepare: {op} does not fit version {version} (live is {live})")
    _sweep(service)
    stages = _stages(service)
    origin = "person" if call.column.name == "P" else "agent"
    if origin == "agent" and sum(1 for m in stages if m.get("origin") == "agent") >= AGENT_STAGES:
        raise HostError("quota.host", f"an agent may have {AGENT_STAGES} staged releases at most: ask a person")
    if len(stages) >= MAX_STAGED:
        raise HostError("quota.host", f"at most {MAX_STAGED} staged releases wait at once: discard some")
    P._rate(call, "app.prepare")  # noqa: SLF001
    staged_id = "pa_" + secrets.token_hex(16)
    stage = {
        "v": 1, "staged_id": staged_id, "ws": service.workspace_id, "ticket": {"uid": view.uid, "key": view.key},
        "origin": origin, "created": service.clock(), "app_id": rec["id"], "version": version, "op": op,
        "app": rec["versions"][str(version)], "previous": {"version": int(live), "meta": rec["versions"][str(live)]},
        "by": str(call.viewer),
    }  # fmt: skip
    P._private(service, "appstage")  # noqa: SLF001
    tmp = _root(service) / f".new-{secrets.token_hex(8)}"
    try:
        os.mkdir(tmp, 0o700)
        os.mkdir(tmp / "files", 0o700)
        _text(service, stage, cfg)  # the words exist and are valid before anything is kept
        P._put_json(tmp / "meta.json", stage)  # noqa: SLF001
        os.rename(tmp, _stage_dir(service, staged_id))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    P._audit(call, "app.prepare", ticket=view.key, staged=staged_id, op=op, origin=origin)  # noqa: SLF001
    return {
        "staged_id": staged_id, "ticket": view.key, "app_id": rec["id"], "version": version, "op": op,
        "origin": origin, "files": [], "access": stage["app"]["access"], "reach": stage["app"]["reach"],
        "needs": "a person's signature",
    }  # fmt: skip


# -- the signature -----------------------------------------------------------------------------------------------------


def facts_for(service: WorkspaceService, args: dict[str, Any], call: Call) -> approval.AppReleaseFacts:
    """``human.prepare {op: app.release}``: the staged release's own words, built here from the stage and the signed
    server setting (never from the request, which names only the staged id). The stage is read and hashed now."""
    _validate("app.release", args)
    if call.column.name != "P":
        raise HostError("op.not_allowed", "only a person lets an app go live")
    P._gate(call)  # noqa: SLF001
    cfg = P._config(service)  # noqa: SLF001
    stage = _load_stage(service, args["staged_id"])
    _verify_stage(service, stage)
    _fresh(service, stage)
    view = P._ticket(call, stage["ticket"]["uid"])  # noqa: SLF001
    P._may_share(call, view)  # noqa: SLF001
    P._hub_route(service)  # noqa: SLF001 - refuse BEFORE the person signs when the hub path is not usable
    return approval.AppReleaseFacts(stage["staged_id"], view.key, _text(service, stage, cfg))


def signed_release(call: Call, stage: dict[str, Any], text: str) -> bool:
    """Is there a ``log.added`` note on the ticket, signed by this person (actor kind person), with exactly ``text``,
    not older than an hour? The same check a share uses: the words are the staged release's, so a changed file, access,
    reach or version has other words and the old signature covers nothing."""
    if not text.startswith(approval.APP_MARK + "\n"):  # an app approval is a note of the app.release op alone
        return False
    return P._signed_approval(call, {"ticket": stage["ticket"]}, text)  # noqa: SLF001


# -- the worker half ---------------------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    _validate(op, args)
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    if (call.frame.get("opts") or {}).get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    if op in WRITE_OPS and service.state == "degraded":
        raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    service.store.refresh()
    if op == "app.prepare":
        return ok_result(_prepare(call, args))
    if op == "app.list":
        return _list(call, args)
    if op == "app.discard":
        return ok_result(_discard(call, args))
    if op == "app.commit_request":
        return ok_result(_commit_request(call, args))
    if op == "app.commit":
        return {"plan": _commit_plan(call, args)}
    return {"plan": _plan(call, args)}


def _plan(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    person = P._person(call)  # noqa: SLF001 - column P only: an agent never sends
    P._gate(call)  # noqa: SLF001
    cfg = P._config(service)  # noqa: SLF001
    stage = _load_stage(service, args["staged_id"])
    view = P._ticket(call, stage["ticket"]["uid"])  # noqa: SLF001
    P._may_share(call, view)  # noqa: SLF001
    _verify_stage(service, stage)  # the bytes and the ticket first: only then are the words worth comparing
    _fresh(service, stage)
    text = _text(service, stage, cfg)
    if not signed_release(call, stage, text):
        raise HostError(
            "publish.not_approved",
            "a person must sign this release first (orch app release " + stage["staged_id"] + ") and the signature "
            "must match this staged release and this server exactly",
        )
    P._rate(call, "app.send")  # noqa: SLF001
    plan: dict[str, Any] = {"op": "app.send", "cfg": cfg, "person": person, "stage": stage}
    plan["hub"] = P._hub_route(service)  # noqa: SLF001
    registered = P._registered(service, cfg) is not None  # noqa: SLF001
    try:
        plan["reg"] = P._delegation(call)  # noqa: SLF001
        P._plan_card(call, plan)  # noqa: SLF001
    except HostError:
        if not registered:
            raise
        plan.pop("reg", None)
    plan["registered"] = registered
    plan["pending"] = _owner_facts(call)  # the facts a commit request needs, or None when the caller is not the owner
    _reserve(service, stage)
    return plan


def _slot(service: WorkspaceService, stage: dict[str, Any]) -> tuple[str, str, int, str]:
    return (service.workspace_id, stage["app_id"], stage["version"], stage.get("op", "commit"))


def _reserve(service: WorkspaceService, stage: dict[str, Any]) -> None:
    now = service.clock()
    with P._MU:  # noqa: SLF001
        if now - _INFLIGHT.get(_slot(service, stage), -INFLIGHT_MAX) < INFLIGHT_MAX:
            raise HostError("lock.busy", "this release is already being sent", retry_after_ms=1000)
        _INFLIGHT[_slot(service, stage)] = now


def _release(service: WorkspaceService, stage: dict[str, Any]) -> None:
    with P._MU:  # noqa: SLF001
        _INFLIGHT.pop(_slot(service, stage), None)


def _row(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: rec.get(k) for k in ("id", "slug", "name", "kind", "access", "ticket", "live", "uploaded")}


def _list(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    from .visibility import Viewer
    from .workspace import ok_result

    service = call.service
    P._gate(call)  # noqa: SLF001
    viewer = Viewer.of(call)
    wanted = P._ticket(call, args["ref"]).key if args.get("ref") else None  # noqa: SLF001
    store = service.store
    apps, staged = [], []
    for rec in _records(service):
        view = store.ticket(rec.get("ticket", ""))
        if view is None or not viewer.sees(view) or (wanted is not None and view.key != wanted):
            continue
        if call.column.name == "G" and not P._claim_ok(call, view):  # noqa: SLF001
            continue
        apps.append(_row({**rec, "ticket": view.key}))
    for stage in _stages(service):
        view = store.ticket(stage["ticket"]["uid"])
        if view is None or not viewer.sees(view) or (wanted is not None and view.key != wanted):
            continue
        if call.column.name == "G" and not P._claim_ok(call, view):  # noqa: SLF001
            continue
        staged.append(
            {
                "staged_id": stage["staged_id"], "app_id": stage["app_id"], "version": stage["version"],
                "name": stage["app"]["name"], "kind": stage["app"]["kind"], "access": stage["app"]["access"],
                "origin": stage["origin"], "ticket": view.key, "files": len(stage["app"]["files"]),
            }
        )  # fmt: skip
    return ok_result({"apps": apps, "staged": staged})


def _discard(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    stage = _load_stage(service, args["staged_id"])
    view = P._ticket(call, stage["ticket"]["uid"])  # noqa: SLF001
    if call.column.name != "P":
        if stage["origin"] != "agent" or not P._claim_ok(call, view):  # noqa: SLF001
            raise HostError("op.not_allowed", "an agent discards only the releases it prepared, on its own ticket")
    elif stage.get("by") != str(call.viewer):  # a person: the one who prepared it, or one who may release it
        P._may_share(call, view)  # noqa: SLF001
    P._gate(call)  # noqa: SLF001
    shutil.rmtree(_stage_dir(service, stage["staged_id"]), ignore_errors=True)
    P._audit(call, "app.discard", ticket=view.key, staged=stage["staged_id"])  # noqa: SLF001
    return {"discarded": True}


# -- going live (#1455): the second signature in the same click session ---------------------------------------------
# The upload is not the release: orch-publish serves a version only after a commit (or a rollback or a start) that
# carries the owner's approval of that one request (hub contract 11.3). The host draws the request (its ts and nonce)
# and keeps it for PENDING_TTL seconds, once; the person's client checks it against the note it just signed
# (``publishclient.golive``) and signs it with its device key; ``app.commit`` then sends it with the approval.


def _released_facts(stage: dict[str, Any]) -> dict[str, Any]:
    """What is needed to rebuild the release words after the stage is gone."""
    return {
        "staged_id": stage["staged_id"], "ticket": stage["ticket"], "origin": stage["origin"],
        "previous": stage.get("previous"), "op": stage.get("op", "commit"), "by": stage.get("by"),
    }  # fmt: skip


def _keep_released(service: WorkspaceService, stage: dict[str, Any]) -> None:
    rows = [r for r in _records(service) if r.get("id") != stage["app_id"]]
    rec = next(r for r in _records(service) if r.get("id") == stage["app_id"])
    rec.setdefault("released", {})[f"{stage.get('op', 'commit')}:{stage['version']}"] = _released_facts(stage)
    _save_records(service, [*rows, rec])


def _owner_person(service: WorkspaceService) -> str | None:
    """The workspace card's owner (the person whose PK is in the pinned genesis): orch-publish accepts only this
    person's approval (``other_person`` otherwise)."""
    from orch.crypto.encoding import unb64u
    from orch.crypto.suite import person_id

    genesis = service.store.genesis_event()
    try:
        return "p_" + person_id(unb64u(genesis["owner"]["pk_pub"], 65)).hex()
    except Exception:  # noqa: BLE001 - fail closed
        return None


def _owner_facts(call: Call) -> dict[str, Any] | None:
    """For the card owner only: their public key and the certificates of their live devices (the client picks its own
    and checks it against its key). Worker thread. ``None`` for anyone else: their release uploads but is not live."""
    service = call.service
    if call.viewer is None or call.viewer != _owner_person(service):
        return None
    core = service.store.state._core  # noqa: SLF001
    member = core.ws.members.get(call.viewer)
    if member is None:
        return None
    certs = [
        dict(d.cert) for d in core.ws.devices.values() if d.person == call.viewer and not d.removed and not d.revoked
    ]
    return {"pk_pub": member.pk_pub, "device_certs": certs}


def _facts_of(cfg: dict[str, str], key: str, stage_like: dict[str, Any]) -> dict[str, Any]:
    """The facts the client rebuilds the words and the body from: all from the host's own records."""
    return {
        "op": stage_like["op"], "app_id": stage_like["app_id"], "version": stage_like["version"],
        "meta": stage_like["app"], "previous": stage_like.get("previous"), "staged_id": stage_like["staged_id"],
        "ticket_key": key, "origin": stage_like["origin"], "server": cfg["server"], "share": cfg["share"],
    }  # fmt: skip


def _pending_answer(call: Call, plan: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
    """The commit request for the owner, else the plain words of why nothing is live."""
    owner = plan.get("pending")
    if owner is None:
        return {"needs": "the workspace owner makes it live: this version is uploaded and waits for their approval"}
    like = {**stage, "op": stage.get("op", "commit")}
    return {"commit": _draw(call.service, plan["cfg"], plan["hub"], like, owner, str(call.viewer))}


def _draw(
    service: WorkspaceService,
    cfg: dict[str, str],
    hub: dict[str, Any],
    like: dict[str, Any],
    owner: dict[str, Any],
    person: str,
) -> dict[str, Any]:
    """Draw the commit request (``ts``, ``nonce``, body, target) and keep it for the client's second signature."""
    from orch.publishclient import golive
    from orch.wsproto.pubsign import PublishContext

    ws = service.workspace_id
    now = service.clock()
    body = golive.commit_body(like["app"], like["op"], like["app_id"], like["version"])
    nonce = os.urandom(16)
    request_id = "cr_" + secrets.token_hex(16)
    req = {
        "request_id": request_id, "op": like["op"], "app_id": like["app_id"], "version": like["version"],
        "method": "POST", "host": PublishContext(ws, cfg["server"]).host,
        "target": golive.commit_target(ws, like["app_id"], like["op"]), "body_hex": body.hex(),
        "ts_ms": int(now * 1000), "nonce_hex": nonce.hex(), "hub_id": hub["hub_id"],
        "facts": _facts_of(cfg, like["ticket"]["key"], like), "pk_pub": owner["pk_pub"],
        "device_certs": owner["device_certs"],
    }  # fmt: skip
    with P._MU:  # noqa: SLF001
        for key in [k for k, v in _PENDING.items() if now - v["at"] > PENDING_TTL]:
            del _PENDING[key]
        _PENDING[(ws, request_id)] = {"at": now, "person": person, "req": req, "ticket": like["ticket"]}
    return req


def _released_stage(service: WorkspaceService, rec: dict[str, Any], op: str, version: int) -> dict[str, Any]:
    """A stage-shaped dict rebuilt from the record: the words of a release whose stage is gone."""
    got = (rec.get("released") or {}).get(f"{op}:{version}")
    meta = (rec.get("versions") or {}).get(str(version))
    if got is None or meta is None:
        raise HostError("not_found", "no such release on this computer: prepare it again")
    return {
        "staged_id": got["staged_id"], "ticket": got["ticket"], "origin": got["origin"], "previous": got["previous"],
        "op": op, "app_id": rec["id"], "version": version, "app": meta, "by": got.get("by"),
    }  # fmt: skip


def _commit_request(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    """``app.commit_request {app_id, op, version}``: a fresh request for a release already sent (the first one timed out
    or was lost). The same signed note still binds it, so it is checked again here (an hour at most)."""
    service = call.service
    person = P._person(call)  # noqa: SLF001
    P._gate(call)  # noqa: SLF001
    cfg = P._config(service)  # noqa: SLF001
    owner = _owner_facts(call)
    if owner is None:
        raise HostError("role.denied", "only the workspace owner makes an app live")
    rec = next((r for r in _records(service) if r.get("id") == args["app_id"]), None)
    if rec is None:
        raise HostError("not_found", "no such app on this computer")
    like = _released_stage(service, rec, args["op"], args["version"])
    view = P._ticket(call, like["ticket"]["uid"])  # noqa: SLF001
    P._may_share(call, view)  # noqa: SLF001
    _check_live_state(rec, like)
    if not signed_release(call, like, _text(service, like, cfg)):
        raise HostError("publish.not_approved", "a person must sign this release again: it is more than an hour old")
    P._rate(call, "app.commit")  # noqa: SLF001
    return _draw(service, cfg, P._hub_route(service), like, owner, person)  # noqa: SLF001


def _check_live_state(rec: dict[str, Any], like: dict[str, Any]) -> None:
    """The live version is the one the release was made against (``publish.changed`` otherwise)."""
    prev = (like.get("previous") or {}).get("version")
    if rec.get("live") != prev and not (like["op"] == "commit" and rec.get("live") == like["version"]):
        raise HostError("publish.changed", "this app changed since the release was signed: prepare it again")
    if like["op"] == "commit" and rec.get("live") == like["version"]:
        raise HostError("publish.changed", "this version is already live")


def _commit_plan(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    """``app.commit {request_id, approval}``: take the pending request (once), check the caller and the note again, and
    plan the send with the pinned ``ts`` and ``nonce`` and the approval the person's client signed."""
    service = call.service
    person = P._person(call)  # noqa: SLF001
    P._gate(call)  # noqa: SLF001
    cfg = P._config(service)  # noqa: SLF001
    ws, now = service.workspace_id, service.clock()
    with P._MU:  # noqa: SLF001
        pending = _PENDING.pop((ws, args["request_id"]), None)  # one use, whatever follows
    if pending is None or now - pending["at"] > PENDING_TTL or pending["person"] != person:
        raise HostError("publish.changed", "that request is gone or too old: try again")
    if _owner_facts(call) is None:
        raise HostError("role.denied", "only the workspace owner makes an app live")
    req = pending["req"]
    rec = next((r for r in _records(service) if r.get("id") == req["app_id"]), None)
    if rec is None:
        raise HostError("not_found", "no such app on this computer")
    like = _released_stage(service, rec, req["op"], req["version"])
    view = P._ticket(call, like["ticket"]["uid"])  # noqa: SLF001
    P._may_share(call, view)  # noqa: SLF001
    _check_live_state(rec, like)
    if not signed_release(call, like, _text(service, like, cfg)):  # the note is still there, locally signed, fresh
        raise HostError("publish.not_approved", "the signature of this release is gone or more than an hour old")
    P._rate(call, "app.commit")  # noqa: SLF001
    plan: dict[str, Any] = {"op": "app.commit", "cfg": cfg, "person": person, "stage": like, "req": req}
    plan["approval"] = args["approval"]
    plan["hub"] = P._hub_route(service)  # noqa: SLF001
    _reserve(service, like)
    return plan


def _commit(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    """Send the approved request exactly as drawn. The signer refuses it unless the approval names it."""
    from orch.hubclient.publish import HubPublishSigner, HubPublishTransport
    from orch.publishclient import PublishClient, flow
    from orch.wsproto.pubsign import PublishContext

    service, req, cfg = call.service, plan["req"], plan["cfg"]
    hub_link, _transport, wsk_pub, sign = P._open_link(service, plan["hub"])  # noqa: SLF001
    signer = HubPublishSigner(
        public_key=wsk_pub, sign=sign, ctx=PublishContext(service.workspace_id, cfg["server"]),
        hub_id=plan["hub"]["hub_id"],
    )  # fmt: skip
    pinned = signer.pin(req["ts_ms"], bytes.fromhex(req["nonce_hex"]), plan["approval"])
    client = PublishClient(pinned, transport=HubPublishTransport(hub_link))
    flow.app_release(client, service.workspace_id, req["app_id"], req["op"], bytes.fromhex(req["body_hex"]))
    with P._MU:  # noqa: SLF001
        rows = [r for r in _records(service) if r.get("id") != req["app_id"]]
        rec = next(r for r in _records(service) if r.get("id") == req["app_id"])
        rec["live"] = req["version"]
        _save_records(service, [*rows, rec])
    P._audit(call, "app.live", app=req["app_id"], version=req["version"], op=req["op"])  # noqa: SLF001
    return {"app_id": req["app_id"], "version": req["version"], "op": req["op"], "live": True}


# -- the event-loop half -----------------------------------------------------------------------------------------------


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    """Run a plan's network half in a thread, off the workspace worker; the answer is the op's result."""
    import asyncio

    if "plan" not in planned:
        return planned
    return await asyncio.to_thread(_run, call, planned["plan"])


def _run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from orch.hubclient import HubRefused
    from orch.publishclient import PublishError

    from .workspace import ok_result

    try:
        return ok_result(_commit(call, plan) if plan["op"] == "app.commit" else _send(call, plan))
    except PublishError as exc:
        err = P._refused(exc)  # noqa: SLF001
        P._audit(call, "app.refused", op=plan["op"], code=err.code, status=getattr(exc, "status", 0))  # noqa: SLF001
        raise err from None
    except HubRefused as exc:
        err = P._hub_error(exc)  # noqa: SLF001
        P._audit(call, "app.refused", op=plan["op"], code=err.code, reason=exc.code, via="hub")  # noqa: SLF001
        raise err from None
    finally:
        P._close_hub_links()  # noqa: SLF001
        _release(call.service, plan["stage"])


def _send(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    """Upload the version meta and every file; the version is NOT committed (see the module doc)."""
    from orch.canon.jcs import dumps as cj
    from orch.publishclient import flow

    service = call.service
    cfg, stage = plan["cfg"], plan["stage"]
    meta = stage["app"]
    op = stage.get("op", "commit")
    with P._MU:  # noqa: SLF001 - right before the upload: the bytes and the record are still what was signed
        blobs = _verify_stage(service, stage)  # the bytes returned are the bytes sent
        _fresh(service, stage)
    ws, app_id, version = service.workspace_id, stage["app_id"], str(stage["version"])
    if op != "commit":  # a rollback or a start sends no file: only the commit request is left
        with P._MU:  # noqa: SLF001
            _verify_stage(service, stage)
            _fresh(service, stage)
            _keep_released(service, stage)
        shutil.rmtree(_stage_dir(service, stage["staged_id"]), ignore_errors=True)
        P._audit(call, "app.send", ticket=stage["ticket"]["key"], app=app_id, version=version, op=op)  # noqa: SLF001
        return {"app_id": app_id, "version": stage["version"], "op": op, "uploaded": False, "live": False,
                **_pending_answer(call, plan, stage)}  # fmt: skip
    client = P._client(service, cfg["server"], plan["hub"])  # noqa: SLF001
    if "reg" in plan and not plan.get("registered"):
        P._register(call, {**plan, "op": "publish.register"})  # noqa: SLF001
    flow.put_app_meta(client, ws, app_id, version, cj(meta))
    for entry, data in zip(meta["files"], blobs, strict=True):
        flow.put_app_file(client, ws, app_id, version, entry["path"], data)
    with P._MU:  # noqa: SLF001
        _verify_stage(service, stage)  # the staged copy is still the signed one when the version is recorded
        _fresh(service, stage)
        rows = [r for r in _records(service) if r.get("id") != app_id]
        old = next((r for r in _records(service) if r.get("id") == app_id), None)
        rec = old or {
            "id": app_id, "slug": meta["slug"], "name": meta["name"], "kind": meta["kind"], "access": meta["access"],
            "ticket": stage["ticket"]["uid"], "versions": {}, "live": None,
        }  # fmt: skip
        rec["versions"][version] = meta
        rec["name"], rec["access"] = meta["name"], meta["access"]
        rec["uploaded"] = stage["version"]
        rec.setdefault("released", {})[f"commit:{version}"] = _released_facts(stage)
        _save_records(service, [*rows, rec])
    shutil.rmtree(_stage_dir(service, stage["staged_id"]), ignore_errors=True)
    P._audit(call, "app.send", ticket=stage["ticket"]["key"], app=app_id, version=version, files=len(blobs))  # noqa: SLF001
    return {"app_id": app_id, "version": stage["version"], "uploaded": True, "live": False,
            **_pending_answer(call, plan, stage)}  # fmt: skip

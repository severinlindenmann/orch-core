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

APP_OPS: dict[str, int] = {"app.prepare": 1, "app.send": 1, "app.list": 1, "app.discard": 1}
NETWORK_OPS = frozenset({"app.send"})
WRITE_OPS = frozenset({"app.prepare", "app.send", "app.discard"})
MAX_STAGED = 10
AGENT_STAGES = 2
MAX_RECORDS = 200
STAGE_BYTES = 200 * 1024 * 1024
_STAGE_ID = re.compile(r"pa_[0-9a-f]{32}")
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
            "reach": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": pub.REACH_MAX},
            "artifacts": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "minItems": 1,
                "maxItems": 100,
                "uniqueItems": True,
            },
        },
        "required": ["ref", "name", "kind", "access", "artifacts"],
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
    return [_stage_read(service, stage["staged_id"], f) for f in stage["app"]["files"]]


def _text(service: WorkspaceService, stage: dict[str, Any], cfg: dict[str, str]) -> str:
    try:
        return approval.app_release_text(
            staged_id=stage["staged_id"],
            ticket_key=stage["ticket"]["key"],
            origin=stage["origin"],
            op="commit",
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


def _prepare(call: Call, args: dict[str, Any]) -> dict[str, Any]:
    service = call.service
    view = P._ticket(call, args["ref"])  # noqa: SLF001
    if not P._claim_ok(call, view):  # noqa: SLF001
        raise HostError("op.not_allowed", "an agent prepares apps only on the ticket its session claims")
    P._gate(call)  # noqa: SLF001
    cfg = P._config(service)  # noqa: SLF001
    P._hub_route(service)  # noqa: SLF001 - a hub that cannot carry the app is said now, before anything is copied
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
    if (bad := pub.meta_problem(meta)) is not None:
        raise HostError("invalid.input", f"app.prepare: the app is not valid ({bad})")
    version = 1
    previous = None
    if rec is not None:
        live = rec.get("live")
        version = max((int(v) for v in rec["versions"]), default=0) + 1
        if live is not None and str(live) in rec["versions"]:
            previous = {"version": int(live), "meta": rec["versions"][str(live)]}
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
    return plan


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
    if call.column.name != "P" and (stage["origin"] != "agent" or not P._claim_ok(call, view)):  # noqa: SLF001
        raise HostError("op.not_allowed", "an agent discards only the releases it prepared, on its own ticket")
    P._gate(call)  # noqa: SLF001
    shutil.rmtree(_stage_dir(service, stage["staged_id"]), ignore_errors=True)
    P._audit(call, "app.discard", ticket=view.key, staged=stage["staged_id"])  # noqa: SLF001
    return {"discarded": True}


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
        return ok_result(_send(call, plan))
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


def _send(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    """Upload the version meta and every file; the version is NOT committed (see the module doc)."""
    from orch.canon.jcs import dumps as cj
    from orch.publishclient import flow

    service = call.service
    cfg, stage = plan["cfg"], plan["stage"]
    # the staged files are read and hashed right before they go; the bytes returned are the bytes sent
    blobs = _verify_stage(service, stage)
    meta = stage["app"]
    client = P._client(service, cfg["server"], plan["hub"])  # noqa: SLF001
    if "reg" in plan and not plan.get("registered"):
        P._register(call, {**plan, "op": "publish.register"})  # noqa: SLF001
    ws, app_id, version = service.workspace_id, stage["app_id"], str(stage["version"])
    flow.put_app_meta(client, ws, app_id, version, cj(meta))
    for entry, data in zip(meta["files"], blobs, strict=True):
        flow.put_app_file(client, ws, app_id, version, entry["path"], data)
    with P._MU:  # noqa: SLF001
        rows = [r for r in _records(service) if r.get("id") != app_id]
        old = next((r for r in _records(service) if r.get("id") == app_id), None)
        rec = old or {
            "id": app_id, "slug": meta["slug"], "name": meta["name"], "kind": meta["kind"], "access": meta["access"],
            "ticket": stage["ticket"]["uid"], "versions": {}, "live": None,
        }  # fmt: skip
        rec["versions"][version] = meta
        rec["name"], rec["access"] = meta["name"], meta["access"]
        rec["uploaded"] = stage["version"]
        _save_records(service, [*rows, rec])
    shutil.rmtree(_stage_dir(service, stage["staged_id"]), ignore_errors=True)
    P._audit(call, "app.send", ticket=stage["ticket"]["key"], app=app_id, version=version, files=len(blobs))  # noqa: SLF001
    return {
        "app_id": app_id, "version": stage["version"], "uploaded": True, "live": False,
        "needs": "the server's approval of the commit (hub.publish_approval); until then this version is not served",
    }  # fmt: skip

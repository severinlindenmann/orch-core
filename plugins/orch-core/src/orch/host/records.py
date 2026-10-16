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
  policy. A records push counts as adding a reader (D-P2-34): while convenience signing is on for the workspace, its
  durable ``convenience_seen`` mark is set, its history holds ``auth: "convenience"`` events, or the tool's scan of the
  pushed history found one (``history_convenience``, #441), the push is refused (``sign.convenience_readers``) unless
  that exact URL carries the owner's private-remote mark. Every answer is recorded (the export destination audit).
* ``records.remote_private {url, on}`` sets or clears the workspace's one private-remote mark (owner decision 12 Oct
  2026): "only I read this remote". orch trusts the owner's word; it is not proof of the remote's ACLs. The mark is
  one exact URL: a changed ``pushurl``, ``insteadOf`` rewrite or remote URL resolves to another string and is not
  marked. The mark binds that URL string; the person's own settings (ssh config, credential helpers, DNS) decide how
  it is reached. It relaxes an owner rule, so only an **owner** sets or clears it, and it is a **write** (Opus #430
  finding 4): ``opts.idem`` is required, the answer is an outcome record (a retry replays it), and every change is
  audited (``marks``: action, redacted URL, person, time). A URL with credentials, a query string or a fragment is
  refused.
* ``records.note`` (a write, owners and maintainers) and ``records.status`` (a read): the tool's last commit, push,
  repo state, resolved URL and auto setting, and the pending counts since that commit (H11e part 2). ``configured``
  means the workspace's records repo exists (the last repo state reported for it); with ``current_url`` the status
  also says whether the push URL now is marked private and equals the last pushed one.

All five are column P only (``person_read``, and ``person_write`` for the mark and the note: G and N get
``op.not_allowed``); all but the mark are for an owner or maintainer now (a push exports every ticket, restricted
ones included). This is a policy orch enforces on its own export paths, not information-flow control: manual git
can still push.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch import records as rec
from orch.ops.readers import convenience_seen, ensure_convenience_mark, has_convenience_events

from . import convenience
from .custody import ensure_private_dir, read_json, write_private
from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = ["CAPS", "RECORDS_OPS", "TRUST_TEXT", "Ctx", "run_op", "serve"]

CAPS = ["records/1"]
RECORDS_OPS: dict[str, int] = {
    "records.snapshot": 1,
    "records.export_check": 1,
    "records.remote_private": 1,
    "records.status": 1,
    "records.note": 1,
}
#: the writes among them (an idem, an outcome record): the mark (owners only, audited) and the note (a host record)
WRITE_OPS = frozenset({"records.remote_private", "records.note"})
#: the writes only an owner makes (the mark relaxes an owner rule)
OWNER_OPS = frozenset({"records.remote_private"})
#: persons whose last note is kept
MAX_PERSONS = 64
#: visible tickets listed in ``records.status`` ``pending.tickets``
STATUS_TICKETS = 200
#: mark changes kept in the audit list
MAX_MARKS = 50
#: manifests kept per workspace (the oldest goes first)
MAX_MANIFESTS = 4
#: export answers kept in the audit list
MAX_EXPORTS = 50
TRUST_TEXT = (
    "orch trusts your word that only you read this remote; if someone else can read it, they get this workspace's "
    "convenience-signed history"
)

_URL = {"type": "string", "minLength": 1, "maxLength": 2048, "pattern": r"^[^\x00-\x1f\x7f]+$"}
_SHA = {"type": "string", "pattern": r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"}
REPO_STATES = ("ok", "missing", "not_dedicated", "unsafe", "index_differs")
_SCHEMAS = {
    "records.snapshot": {
        "type": "object",
        "properties": {"page": {"type": "string", "minLength": 1, "maxLength": 200}},
        "additionalProperties": False,
    },
    "records.export_check": {
        "type": "object",
        "properties": {
            "sha": _SHA,
            "url": _URL,
            "ref": {"type": "string", "pattern": r"^refs/heads/[A-Za-z0-9._/-]{1,200}$"},
            "history_convenience": {"type": "boolean"},
        },
        "required": ["sha", "url", "ref", "history_convenience"],
        "additionalProperties": False,
    },
    "records.remote_private": {
        "type": "object",
        "properties": {"url": _URL, "on": {"type": "boolean"}},
        "required": ["on"],
        "additionalProperties": False,
    },
    "records.status": {
        "type": "object",
        "properties": {"current_url": _URL},
        "additionalProperties": False,
    },
    "records.note": {
        "type": "object",
        "properties": {
            "manifest": {"type": "string", "pattern": r"^m_[0-9a-f]{64}$"},
            "commit": _SHA,
            "pushed": {"type": "boolean"},
            "url": _URL,
            "repo": {
                "type": "object",
                "properties": {
                    "state": {"enum": list(REPO_STATES)},
                    "problem": {"type": "string", "minLength": 1, "maxLength": 300},
                },
                "required": ["state"],
                "additionalProperties": False,
            },
            "auto": {
                "oneOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "properties": {
                            "on": {"type": "boolean"},
                            "every_min": {"type": "integer", "minimum": 5, "maximum": 1440},
                            "push": {"type": "boolean"},
                        },
                        "required": ["on", "every_min", "push"],
                        "additionalProperties": False,
                    },
                ]
            },
        },
        "required": ["pushed"],
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


def _validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")
    # a schema ``$`` also matches before a final newline
    for name in ("url", "current_url", "ref", "sha", "page", "commit", "manifest"):
        value = args.get(name)
        if isinstance(value, str) and any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise HostError("invalid.input", f"{op}: {name} is not valid")
    for name in ("url", "current_url"):
        url = args.get(name)
        if isinstance(url, str):
            from orch.records.git import url_problem

            problem = url_problem(url)
            if problem is not None:  # never stored, never echoed: the mark and the audit hold no secret
                raise HostError("invalid.input", f"{op}: {name} is refused: {problem}")
    if op == "records.remote_private" and args["on"] and "url" not in args:
        raise HostError("invalid.input", "records.remote_private: url is required to set the mark")
    if op == "records.note" and "manifest" in args and "commit" not in args:
        raise HostError("invalid.input", "records.note: a manifest needs its commit")
    if op == "records.note" and args.get("pushed") and "commit" not in args:
        raise HostError("invalid.input", "records.note: a push needs its commit")


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


# --- the context: the host's request, or the direct-mode tool (D-P2-38) -------------------------------------------


@dataclass
class Ctx:
    """What a records op needs, from the host's request or from the direct-mode tool (same rules, same files)."""

    store: Any
    state_dir: Path
    workspace_id: str
    host_uid: int
    clock: Callable[[], float]
    principal: str
    person: str | None
    #: a ticket view the caller may see (``records.status`` lists visible tickets only)
    sees: Callable[[Any], bool]
    #: where the manifests and the hash cache live (the workspace service; a plain object in direct mode)
    holder: Any = field(default=None)
    #: the caller is a workspace owner (direct mode: the person who owns these files); a URL is shown in full to an
    #: owner only, to anyone else as its host
    owner: bool = field(default=True)

    @property
    def actor(self) -> str:
        """Who acted, for the audit and the per-person note: the person, else the principal (direct mode)."""
        return self.person or self.principal


def ctx_of(call: Call, viewer: Viewer) -> Ctx:
    service = call.service
    return Ctx(
        store=service.store,
        state_dir=Path(service.state_dir),
        workspace_id=service.workspace_id,
        host_uid=service.host_uid,
        clock=service.clock,
        principal=call.principal,
        person=viewer.person,
        sees=viewer.sees,
        holder=service,
        owner=viewer.role == "owner",
    )


# --- serving ------------------------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    """One records op on the worker thread (after the column): args, files, the workspace state, the role, the op."""
    from .outcomes import request_idem
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    _validate(op, args)
    idem = request_idem(op, call.frame.get("opts"))  # reads: dry_run is invalid.input, idem ignored; writes: required
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    viewer = Viewer.of(call)
    if not viewer.manager:
        raise HostError("op.not_allowed", f"{op} is for a workspace owner or maintainer at the terminal or dashboard")
    if op in OWNER_OPS and viewer.role != "owner":
        raise HostError("op.not_allowed", f"{op} relaxes an owner rule: only a workspace owner sets or clears the mark")
    ctx = ctx_of(call, viewer)
    try:
        if op not in WRITE_OPS:
            return ok_result(run_op(ctx, op, args))
        return _write(call, ctx, op, args, str(idem))
    except rec.RecordsError as exc:
        raise _host_error(exc) from None


def run_op(ctx: Ctx, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """The op itself (the host after its checks, or the direct-mode tool)."""
    if op == "records.snapshot":
        return _snapshot(ctx, args)
    if op == "records.export_check":
        return _export_check(ctx, args)
    if op == "records.remote_private":
        return _remote_private(ctx, args)
    if op == "records.note":
        return _note(ctx, args)
    if op == "records.status":
        return _status(ctx, args.get("current_url"))
    raise HostError("unknown_command", f"{op} is not served")


def _write(call: Call, ctx: Ctx, op: str, args: dict[str, Any], idem: str) -> dict[str, Any]:
    """A records write: an outcome record under the caller's idem (a retry replays the stored answer)."""
    from .auth import Redactor
    from .workspace import ok_result

    def apply(_intent: Any) -> dict[str, Any]:
        return ok_result(run_op(ctx, op, args))

    return call.service.outcomes.execute(
        call.principal,
        idem,
        op,
        dict(args),
        apply,
        planned=None,
        session=(call.frame.get("auth") or {}).get("session"),
        redactor=Redactor((call.frame.get("auth") or {}).get("grant")),
    )


def _state(holder: Any) -> dict[str, Any]:
    st = getattr(holder, "records_state", None)
    if st is None:
        st = {"manifests": OrderedDict(), "hashes": {}}
        holder.records_state = st
    return st


def _take(ctx: Ctx) -> dict[str, Any]:
    """A new manifest of the record files, under the store lock."""
    from orch.model import WORKSPACE

    store = ctx.store
    st = _state(ctx.holder)
    with store.locked():
        store.load_all()  # the chain heads of every ticket log, verified
        root_fd = os.dup(store.fs.fd)  # the store's pinned root (#516), never the root re-opened by path
        try:
            paths = rec.list_records(root_fd)
            entries = rec.hash_records(root_fd, paths, st["hashes"])
        finally:
            os.close(root_fd)
        ws_seq = int(store.head_seq(WORKSPACE))
        heads: dict[str, str | None] = {"workspace": store.log_head(WORKSPACE)}
        seqs: dict[str, int] = {"workspace": ws_seq}
        for p in paths:
            if p.startswith("tickets/") and p.endswith("/events.jsonl"):
                uid = p.split("/")[1]
                heads[uid] = store.log_head(uid)
                seqs[uid] = int(store.head_seq(uid))
    live = {(e.path, e.size) for e in entries}
    st["hashes"] = {k: v for k, v in st["hashes"].items() if (k[0], k[1]) in live}  # keep the cache bounded
    mid = rec.manifest_id(ctx.workspace_id, ws_seq, heads, entries)
    return {"id": mid, "ws_seq": ws_seq, "heads": heads, "seqs": seqs, "files": [e.as_dict() for e in entries]}


def _manifests(ctx: Ctx) -> OrderedDict[str, dict[str, Any]]:
    now = float(ctx.clock())
    manifests: OrderedDict[str, dict[str, Any]] = _state(ctx.holder)["manifests"]
    for mid in [m for m, v in manifests.items() if v["expires"] <= now]:
        manifests.pop(mid, None)
    return manifests


def _held(ctx: Ctx, mid: str) -> dict[str, Any]:
    held = _manifests(ctx).get(mid)
    if held is None:
        raise HostError("page.expired", "the records manifest expired or is unknown: take a new snapshot")
    if held["principal"] != ctx.principal:
        raise HostError("page.expired", "the records manifest belongs to another caller")
    return held


def _snapshot(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    now = float(ctx.clock())
    manifests = _manifests(ctx)
    if "page" in args:
        mid, _, pos = args["page"].partition(".")
        if not pos.isdigit():
            raise HostError("page.expired", "the records manifest expired or is unknown: take a new snapshot")
        held = _held(ctx, mid)
        offset = int(pos)
        manifest = held["manifest"]
        taken_at = held["taken_at"]
    else:
        manifest = _take(ctx)
        taken_at = _stamp(now)
        manifests[manifest["id"]] = {
            "principal": ctx.principal,
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
        "workspace": ctx.workspace_id,
        "manifest": manifest["id"],
        "ws_seq": manifest["ws_seq"],
        "heads": manifest["heads"],
        "taken_at": taken_at,
        "count": len(files),
        "bytes": sum(f["size"] for f in files),
        "files": files[offset:end],
        "next_page": nxt,
    }


def convenience_on(ctx: Ctx) -> bool:
    """The convenience-readers condition (§0.8.6): the flag is on, the durable ``convenience_seen`` mark is set
    (#441), or the merged history holds convenience events (which sets the mark: an existing workspace counts as
    seen)."""
    if convenience.enabled(ctx.state_dir, ctx.workspace_id, ctx.host_uid):
        return True
    if convenience_seen(ctx.state_dir, ctx.workspace_id):
        return True
    store = ctx.store
    with store.locked():
        found = has_convenience_events(store)
    if found:
        _mark_seen(ctx)
    return found


def _mark_seen(ctx: Ctx) -> None:
    """A scan found convenience events: set the mark (best effort; the answer is "seen" either way)."""
    with contextlib.suppress(Exception):
        ensure_convenience_mark(ctx.state_dir, ctx.workspace_id, ctx.host_uid, clock=ctx.clock)


def _export_check(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    from types import SimpleNamespace

    from .devmode import refuse_push

    # §11A.17.3 F5, F2: no push from a dev host, none of a dev-pair sandbox (SBX genesis or marker), before any write
    holder = ctx.holder
    refuse_push(
        SimpleNamespace(dev=getattr(holder, "dev", None), store=ctx.store, entry=getattr(holder, "entry", None))
    )
    now = float(ctx.clock())
    doc = read_record(ctx.state_dir, ctx.workspace_id, ctx.host_uid)
    # the tool's scan of the event logs reachable from sha (#441): it refuses this push only and never writes the
    # durable mark, which records only what the host itself observed (an append, a cut tail, its merged history)
    pushed = args["history_convenience"]
    conv = pushed or convenience_on(ctx)
    mark = doc.get("private_remote")
    marked = isinstance(mark, dict) and mark.get("url") == args["url"]
    policy = "none"
    if conv:
        if not marked:
            entry = {**args, "policy": "refused", "at": _stamp(now), "by": ctx.actor}
            doc["exports"] = [*doc["exports"], entry][-MAX_EXPORTS:]
            _write_record(ctx.state_dir, ctx.workspace_id, ctx.host_uid, doc)
            why = (
                "the private-remote mark names another URL; the push destination changed since it was marked"
                if isinstance(mark, dict)
                else "no private-remote mark for this URL"
            )
            what = "a history with convenience-signed events" if pushed else "a workspace with convenience signing"
            raise HostError(
                "sign.convenience_readers",
                f"a records push adds a reader to {what} ({why})",
                hint="mark this exact push URL private (orch records remote-private), or keep the records local",
            )
        policy = "private_remote"
    body = {"workspace": ctx.workspace_id, **args, "policy": policy, "convenience": conv}
    check = "x_" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    doc["exports"] = [*doc["exports"], {**args, "policy": policy, "at": _stamp(now), "by": ctx.actor}][-MAX_EXPORTS:]
    _write_record(ctx.state_dir, ctx.workspace_id, ctx.host_uid, doc)
    return {**body, "check": check}


def _remote_private(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    from orch.records.git import redact

    now = float(ctx.clock())
    doc = read_record(ctx.state_dir, ctx.workspace_id, ctx.host_uid)
    if args["on"]:
        doc["private_remote"] = {"url": args["url"], "at": _stamp(now), "by": ctx.actor}
    else:
        doc["private_remote"] = None
    entry = {"action": "set" if args["on"] else "clear", "at": _stamp(now), "by": ctx.actor}
    if args.get("url"):
        entry["url"] = redact(args["url"])
    doc["marks"] = [*(doc.get("marks") or []), entry][-MAX_MARKS:]
    _write_record(ctx.state_dir, ctx.workspace_id, ctx.host_uid, doc)
    return {"private_remote": doc["private_remote"], "text": TRUST_TEXT}


# --- the note and the status (a display, never a decision) ----------------------------------------------------------


def _note(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    """What the tool committed or pushed, its repo state, the URL git resolved and the auto setting: an unsigned host
    record in ``records.json``, **kept per person** (``lasts[<person>]``; Opus #433 finding 1): one manager's note
    never changes what another person's ``records.status`` reports for them, and the tool's auto mode never reads it
    (it keeps its own clock in the person's state file). ``last_noted`` says who noted last, workspace-wide. A
    manifest must be one this caller took and the host still holds (its per-log sequence numbers make ``pending``
    exact). A manual git commit makes it stale: status is a display, never a decision."""
    now = float(ctx.clock())
    doc = read_record(ctx.state_dir, ctx.workspace_id, ctx.host_uid)
    lasts = dict(doc.get("lasts") or {})
    last = dict(lasts.get(ctx.actor) or {})
    if "manifest" in args:
        manifest = _held(ctx, args["manifest"])["manifest"]
        last.update(
            commit=args["commit"],
            at=_stamp(now),
            ws_seq=manifest["ws_seq"],
            seqs=dict(manifest["seqs"]),
        )
    if args.get("pushed"):
        last.update(pushed_at=_stamp(now), pushed_commit=args["commit"])
    for name in ("url", "repo"):
        if name in args:
            last[name] = args[name]
    if "repo" in args:  # the records repo is the workspace's (one record root): its last report, whoever made it
        doc["repo"] = {"state": args["repo"]["state"], "at": _stamp(now), "by": ctx.actor}
    if "auto" in args:
        last["auto"] = args["auto"]
    lasts[ctx.actor] = last
    doc["lasts"] = dict(sorted(lasts.items())[-MAX_PERSONS:]) if len(lasts) > MAX_PERSONS else lasts
    doc["last_noted"] = {"by": ctx.actor, "at": _stamp(now)}
    _write_record(ctx.state_dir, ctx.workspace_id, ctx.host_uid, doc)
    return {}


#: repo states that mean the workspace has its own records repo (``missing``: none; ``not_dedicated``: the record root
#: sits in another repository)
_CONFIGURED = frozenset({"ok", "index_differs", "unsafe"})


def _configured(doc: dict[str, Any]) -> bool:
    """The records repo exists, per workspace: the last repo state any tool reported (``repo`` in ``records.json``),
    else (a record from before it) any person's last report. The host runs no git and reads no repository file
    (D-P2-19), so this is the tools' word; the tool's own ``status`` replaces it with what it sees now."""
    repo = doc.get("repo")
    if isinstance(repo, dict):
        return repo.get("state") in _CONFIGURED
    lasts = doc.get("lasts") or {}
    return any(isinstance(v, dict) and (v.get("repo") or {}).get("state") in _CONFIGURED for v in lasts.values())


_SCHEME_URL = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(?:[^/@]*@)?([^/]*)(.*)$", re.S)
_SCP_URL = re.compile(r"^(?:[^@/:]+@)?([^:/]+):(.*)$", re.S)


def display_url(url: str, owner: bool) -> str:
    """A push URL for a display: without its userinfo, and for anyone but an owner only its host (``local`` for a
    path or ``file://``)."""
    m = _SCHEME_URL.match(url)
    if m:
        scheme, host, rest = m.groups()
        if owner:
            return f"{scheme}://{host}{rest}"
        return host or "local"
    m = _SCP_URL.match(url)
    if m:
        host, path = m.groups()
        return f"{host}:{path}" if owner else host
    return url if owner else "local"


def _status(ctx: Ctx, current_url: str | None = None) -> dict[str, Any]:
    """The caller's own last note (commit, push, repo state, URL, auto) and the pending counts since that commit, plus
    who noted last workspace-wide. ``remote.url`` is **display only**: it is what the caller's tool reported, and a
    client must never pass it to ``records.remote_private`` (the tool resolves the URL to mark from git itself).

    ``current_url`` is the effective push URL the records tool resolved just now (its ``status`` passes it): the
    answer adds ``remote.current_url`` (for a display, :func:`display_url`), ``remote.current_private`` (that exact
    string carries the private-remote mark) and ``remote.matches_last`` (it equals the last pushed destination), for
    the dashboard's push confirmation. Display only: the push resolves and checks its URL again."""
    from orch.model import WORKSPACE

    doc = read_record(ctx.state_dir, ctx.workspace_id, ctx.host_uid)
    last = (doc.get("lasts") or {}).get(ctx.actor) or {}  # the caller's own notes only (Opus #433 finding 1)
    seqs = last.get("seqs") or {}
    store = ctx.store
    tickets: list[dict[str, Any]] = []
    events = 0
    more = False
    with store.locked():
        store.load_all()
        ws_events = max(0, int(store.head_seq(WORKSPACE)) - int(seqs.get("workspace", 0)))
        for uid, view in sorted(store.state.tickets.items()):
            n = max(0, int(store.head_seq(uid)) - int(seqs.get(uid, 0)))
            if not n:
                continue
            events += n  # an aggregate: invisible tickets count, but are never named
            if ctx.sees(view):
                if len(tickets) < STATUS_TICKETS:
                    tickets.append({"key": view.key, "events": n})
                else:
                    more = True
    remote = None
    url = last.get("url")
    mark = doc.get("private_remote")
    marked_url = mark.get("url") if isinstance(mark, dict) else None
    if isinstance(url, str) or current_url is not None:
        shown = url if isinstance(url, str) else current_url
        private = marked_url is not None and marked_url == shown
        blocked = convenience_on(ctx) and not private
        remote = {
            "url": url if isinstance(url, str) else None,
            "private": private,
            "export": "blocked_convenience" if blocked else "ok",
            "current_url": None if current_url is None else display_url(current_url, ctx.owner),
            "current_private": None if current_url is None else marked_url == current_url,
            "matches_last": None if current_url is None else (isinstance(url, str) and url == current_url),
        }
    out_last = None
    if last.get("commit"):
        out_last = {
            "commit": last["commit"],
            "at": last.get("at"),
            "by": ctx.actor,
            "ws_seq": last.get("ws_seq"),
            "pushed_at": last.get("pushed_at") if last.get("pushed_commit") == last["commit"] else None,
        }
    return {
        "configured": _configured(doc),
        "repo": last.get("repo"),
        "pending": {"events": events + ws_events, "tickets": tickets, "more": more, "workspace_events": ws_events},
        "last": out_last,
        "auto": last.get("auto"),
        "remote": remote,
        "last_noted": doc.get("last_noted"),
    }

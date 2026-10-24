"""The add-on marketplace, host side (V3 G2, #920; docs/architecture/orch-v2-marketplace.md). The CLI is
``orch market ...``; design note ``state/design-g2.md`` with the Opus edits E1 to E9.

Ops:

* ``market.add {url, commit?}``: a person (column P, workspace owner) adds a marketplace: gate, fetch, verify, record.
  ``commit`` pins the index at that full object id (``pinned``). Anything but the official URL is refused for now.
* ``market.list {entries?}``: P and G, no network: the added marketplaces, each from its cached bytes **re-parsed and
  re-verified now** (E6); with ``entries`` the add-ons of each.
* ``market.refresh {url?, commit?}``: P and G. Fetches again an already added marketplace (it takes no new URL, so an
  agent cannot make the host connect anywhere new). A pinned market is only reported on unless a person gives
  ``commit`` (E4). An agent's refresh is skipped when the last fetch was under five minutes ago (E5).

Order of every call that can reach the network (E1): the actor, the canonical URL, the class and hub gate, the records,
and only then DNS and git. The network work runs off the workspace worker with no lock held; the store is locked only
for the compare-and-write (E3). Unverified data is never recorded as a floor (E2). Nothing in a refusal echoes index
content (E9: ``name``, ``title`` and ``summary`` are shown under ``from_marketplace`` and are not covered by the
signature).
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch.market import ADDONS, MarketError, fetch, index
from orch.market.constants import AGENT_MIN_INTERVAL
from orch.ops.errors import ERRORS

from . import marketinstall
from .errors import HostError
from .marketstate import MarketFiles

if TYPE_CHECKING:
    from .workspace import Call, WorkspaceService

__all__ = ["MARKET_OPS", "MARKET_READS", "NETWORK_OPS", "OP_CODES", "execute", "hub_joined", "serve"]

MARKET_READS = frozenset({"market.list"}) | marketinstall.READS
MARKET_OPS: dict[str, int] = {"market.add": 1, "market.list": 1, "market.refresh": 1, **marketinstall.MARKET_OPS}
#: these reach the network: off the workspace worker (add and refresh, and the package fetch of stage)
NETWORK_OPS = frozenset({"market.add", "market.refresh"}) | marketinstall.NETWORK_OPS

_COMMON = ("market.actor_refused", "market.state_invalid", "market.malformed")
_DEV = ("market.community_unisolated",)
#: the codes each op can raise (the per-op declaration; a test checks every raise site against it)
OP_CODES: dict[str, tuple[str, ...]] = {
    "market.add": (
        *_COMMON,
        "market.community_unisolated",
        "market.not_allowed",
        "market.already_added",
        "market.address_refused",
        "market.fetch_failed",
        "market.commit_mismatch",
        "market.index_unsigned",
        "market.source_mismatch",
        "market.rollback",
        "market.unpinned",
        "market.bad_digest_format",
        "market.duplicate_id",
        "market.refresh_running",
    ),  # fmt: skip
    "market.list": _COMMON,
    "market.refresh": (
        *_COMMON,
        "market.community_unisolated",
        "market.not_allowed",
        "market.address_refused",
        "market.fetch_failed",
        "market.commit_mismatch",
        "market.index_unsigned",
        "market.source_mismatch",
        "market.rollback",
        "market.unpinned",
        "market.bad_digest_format",
        "market.duplicate_id",
        "market.refresh_running",
        "market.publisher_key_changed",
    ),  # fmt: skip
    **marketinstall.OP_CODES,
}
_COMMIT = {"type": "string", "pattern": "^(?:[0-9a-f]{40}|[0-9a-f]{64})$"}
_URL = {"type": "string", "minLength": 1, "maxLength": 200}
_SCHEMAS: dict[str, dict[str, Any]] = {
    "market.add": {
        "type": "object",
        "properties": {"url": _URL, "commit": _COMMIT},
        "required": ["url"],
        "additionalProperties": False,
    },
    "market.list": {"type": "object", "properties": {"entries": {"type": "boolean"}}, "additionalProperties": False},
    "market.refresh": {
        "type": "object",
        "properties": {"url": _URL, "commit": _COMMIT},
        "additionalProperties": False,
    },
}
_SCHEMAS.update(marketinstall.SCHEMAS)
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


def _refuse(op: str, code: str) -> HostError:
    """The one place that turns a code into a refusal: declared for the op, fixed catalog text, nothing else."""
    if code not in OP_CODES[op] and code not in ("not_found", "role.denied", "invalid.input", "workspace.degraded"):
        raise AssertionError(f"{op} raised {code}, which it does not declare")
    return HostError(code, ERRORS[code].message if code in ERRORS else code)


def _files(service: WorkspaceService) -> MarketFiles:
    return MarketFiles(Path(service.state_dir), service.workspace_id, service.host_uid)


def hub_joined(service: WorkspaceService) -> bool:
    """Is a hub joined (spec 9: read from the signed log, fail closed)? The workspace model folds ``hub.joined`` and
    ``hub.left``; a hub record that says anything but ``unbound`` or cannot be read also counts as joined (a join in
    progress is joined). The hub's policy is G7: until then a joined workspace allows no marketplace."""
    model = dict(service.store.state.workspace.hub) if service.store is not None else {}
    if model.get("status") == "joined":
        return True
    from . import hubops

    try:
        state = hubops._load_state(service)  # noqa: SLF001
    except Exception:  # noqa: BLE001 - unreadable hub record: not standalone
        return True
    return state is not None and getattr(state, "status", None) != "unbound"


def _check(call: Call) -> tuple[str, dict[str, Any]]:
    op = call.frame["op"]
    args = dict(call.frame["args"])
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        raise HostError("invalid.input", f"{op}: {'.'.join(map(str, errors[0].absolute_path)) or 'args'} is not valid")
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    if (call.frame.get("opts") or {}).get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    person = call.column.name == "P" and call.viewer is not None
    if op in ("market.add", *marketinstall.HUMAN_ONLY) or (op == "market.refresh" and "commit" in args):
        # human-only: before anything else (E1); a person's column P, never an agent's grant or a mandate
        if not person:
            raise _refuse(op, "market.actor_refused")
        member = service.store.state.workspace.members.get(call.viewer)
        if member is None or member.role != "owner":
            raise HostError("role.denied", "marketplaces are for workspace owners")
    elif call.column.name not in ("P", "G"):
        raise HostError("op.not_allowed", f"{op} is for a person or an agent with a grant")
    service.store.refresh()
    return op, args


def serve(call: Call) -> dict[str, Any]:
    """The worker half: shape, who, the gates and the records (no network); the fetch runs in :func:`execute`."""
    from .workspace import ok_result

    op, args = _check(call)
    service = call.service
    now = service.clock()
    if op in marketinstall.MARKET_OPS:
        return marketinstall.serve(call, op, args)
    try:
        if op == "market.list":
            return ok_result(_list(service, bool(args.get("entries")), now))
        files = _files(service)
        records = files.load()
        if op == "market.add":
            classes = {args["url"]: marketinstall.market_class(service, args["url"])}
            if args["url"] in records:
                raise MarketError("market.already_added")
            urls = [args["url"]]
        elif "url" in args:
            classes = {args["url"]: marketinstall.market_class(service, args["url"])}
            if args["url"] not in records:
                raise HostError("not_found", "that marketplace was not added: orch market list")
            urls = [args["url"]]
        else:
            urls = sorted(records)
            classes = {u: marketinstall.market_class(service, u) for u in urls}
    except MarketError as exc:
        raise _refuse(op, exc.code) from None
    return {
        "plan": {
            "op": op, "urls": urls, "commit": args.get("commit"), "agent": call.column.name != "P",
            "all": op == "market.refresh" and "url" not in args, "classes": classes,
        }
    }  # fmt: skip


async def execute(call: Call, planned: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    if "plan" not in planned:
        return planned
    if planned["plan"]["op"] in marketinstall.MARKET_OPS:
        return await marketinstall.execute(call, planned)
    return await asyncio.to_thread(_run, call, planned["plan"])


def _audit(call: Call, event: str, **more: Any) -> None:
    from . import hubops

    hubops._audit(call, event, **more)  # noqa: SLF001


def _run(call: Call, plan: dict[str, Any]) -> dict[str, Any]:
    from .workspace import ok_result

    op, service = plan["op"], call.service
    files = _files(service)
    rows: list[dict[str, Any]] = []
    for url in plan["urls"]:
        try:
            cls, keys = plan["classes"][url]
            if op == "market.add":
                row = _add(files, url, plan["commit"], service.clock(), cls, tuple(keys))
            else:
                row = _refresh(files, url, plan["commit"], plan["agent"], service.clock, cls, tuple(keys))
        except MarketError as exc:
            if not plan["all"]:
                _audit(call, "market.refused", op=op, code=exc.code)
                raise _refuse(op, exc.code) from None
            row = {"url": url, "error": exc.code}
        rows.append(row)
        _audit(call, op, url=url, moved=bool(row.get("moved", op == "market.add")), error=row.get("error"))
    if op == "market.add":
        return ok_result(rows[0])
    return ok_result({"markets": rows})


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _record(
    rec: dict[str, Any] | None, got: str, raw: bytes, v: index.Verdict, cls: str, **more: Any
) -> dict[str, Any]:
    """The new record. A floor (``last_version``, ``payload_sha256``) comes only from a verified index (E2)."""
    old = rec or {"last_version": None, "payload_sha256": None, "sig_key": None, "pinned": False}
    new = {
        "class": cls,
        "added_at": old.get("added_at", 0),
        "index_commit": got,
        "index_sha256": _sha(raw),
        "pinned": old["pinned"],
        "last_version": old["last_version"],
        "payload_sha256": old["payload_sha256"],
        "sig_key": old["sig_key"],
    }
    if v.verified:
        new["last_version"], new["payload_sha256"] = v.doc["version"], v.payload_sha256
        if cls == "other" and rec is None:  # the index key of a non-official market is remembered at a human add only
            new["sig_key"] = v.doc["signature"]["key"]
    new.update(more)
    return new


def _add(
    files: MarketFiles, url: str, commit: str | None, now: float, cls: str, keys: tuple[str, ...]
) -> dict[str, Any]:
    with files.refresh_lock(url):
        files.stamp(url, now)
        got, raw = fetch.fetch_index(url, commit)
        v = marketinstall.verdict(raw, url, cls, keys, now)  # only the official URL and a sandbox's test URL pass
        with files.commit() as write:
            records = files.load()
            if url in records:
                raise MarketError("market.already_added")
            rec = _record(None, got, raw, v, cls, added_at=int(now), pinned=commit is not None)
            write({**records, url: rec}, {url: raw})
    return _summary(url, rec, v)


def _summary(url: str, rec: dict[str, Any], v: index.Verdict) -> dict[str, Any]:
    return {
        "url": url, "class": rec["class"], "version": v.doc["version"], "index_commit": rec["index_commit"],
        "pinned": rec["pinned"], "stale": v.stale, "verified": v.verified, "entries": len(v.doc[ADDONS]),
        "moved": True,
    }  # fmt: skip


def _refresh(
    files: MarketFiles, url: str, commit: str | None, agent: bool, clock: Any, cls: str, keys: tuple[str, ...]
) -> dict[str, Any]:
    now = clock()
    rec = files.load().get(url)
    if rec is None:
        raise MarketError("market.state_invalid")  # gone since the plan was made
    last = files.last_fetch(url)
    if agent and last is not None and 0 <= now - last < AGENT_MIN_INTERVAL:  # E5: no network at all
        return {"url": url, "skipped": "fresh", "moved": False}
    with files.refresh_lock(url):
        files.stamp(url, now)
        got, raw = fetch.fetch_index(url, commit)
        v = marketinstall.verdict(raw, url, cls, keys, now)
        _check_index_key(rec, v)
        index.check_floor(rec["last_version"], rec["payload_sha256"], v)
        if rec["pinned"] and commit is None:  # E4: reported, never moved by a plain refresh
            return {
                "url": url, "moved": False, "pinned": True, "head": got, "newer": got != rec["index_commit"],
                "version": v.doc["version"], "stale": v.stale,
            }  # fmt: skip
        with files.commit() as write:
            records = files.load()
            cur = records.get(url)
            if cur is None:
                raise MarketError("market.malformed")
            index.check_floor(cur["last_version"], cur["payload_sha256"], v)  # again, against the record as it is now
            new = _record(cur, got, raw, v, cls, pinned=cur["pinned"] or commit is not None)
            write({**records, url: new}, {url: raw})
    return _summary(url, new, v)


def _check_index_key(rec: dict[str, Any], v: index.Verdict) -> None:
    """A non-official index's signature key is remembered at the human ``add``; a refresh never writes it and refuses
    a changed or dropped key (spec 4.4). Unreachable before #287 (only the official URL passes the gate)."""
    if rec["class"] != "other" or rec["sig_key"] is None:
        return
    sig = v.doc.get("signature")
    if sig is None or sig["key"] != rec["sig_key"]:
        raise MarketError("market.publisher_key_changed")


def fingerprint(key_b64: str) -> str:
    """``SHA256:`` of the 65 key bytes, as ssh and ``orch.land.key.fingerprint`` show a key."""
    raw = base64.urlsafe_b64decode(key_b64 + "=" * (-len(key_b64) % 4))
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _installed_row(service: WorkspaceService, name: str, rec: dict[str, Any] | None, url: str) -> dict[str, Any] | None:
    """``{version, state}`` of the add-on when it came from this marketplace, else ``None``."""
    if rec is None or rec["origin"] != url:
        return None
    return {"version": rec["version"], "state": marketinstall.state_of(service, name)}


def _update_available(rec: dict[str, Any] | None, url: str, entry: dict[str, Any]) -> bool:
    if rec is None or rec["origin"] != url:
        return False
    return marketinstall.mi.semver(entry["version"]) > marketinstall.mi.semver(rec["version"])


def _list(service: WorkspaceService, entries: bool, now: float) -> dict[str, Any]:
    files = _files(service)
    try:
        records = files.load()
    except MarketError as exc:
        raise _refuse("market.list", exc.code) from None
    rows = []
    for url in sorted(records):
        rec = records[url]
        row: dict[str, Any] = {
            "url": url, "class": rec["class"], "pinned": rec["pinned"], "index_commit": rec["index_commit"],
            "last_fetch": None if (t := files.last_fetch(url)) is None else int(t),
        }  # fmt: skip
        raw = files.read_cache(url)
        try:
            cls, keys = marketinstall.market_class(service, url)  # the class is decided now, never read from the record
            if raw is None or _sha(raw) != rec["index_sha256"] or cls != rec["class"]:
                raise MarketError("market.malformed")
            v = marketinstall.verdict(raw, url, cls, keys, now)
        except MarketError:
            row["cache"] = "invalid"  # never trusted: run orch market refresh
            rows.append(row)
            continue
        doc = v.doc
        revoked = set(doc.get("revoked", []))
        row |= {
            "cache": "ok", "version": doc["version"], "verified": v.verified, "stale": v.stale,
            "expires_at": doc.get("expires_at"), "entries": len(doc[ADDONS]), "revoked": len(revoked),
            "from_marketplace": {"name": doc["name"], "title": doc.get("title")},
        }  # fmt: skip
        if entries:
            have = files.installs()["addons"]
            row[ADDONS] = [
                {
                    "id": e["id"],
                    "installed": _installed_row(service, e["id"], have.get(e["id"]), url),
                    "update": _update_available(have.get(e["id"]), url, e),
                    "version": e["version"],
                    "capabilities": e["capabilities"],
                    "revoked": f"{e['id']}@{e['version']}" in revoked,
                    "publisher": fingerprint(e["publisher"]["key"]) if "publisher" in e else None,
                    "from_marketplace": {"summary": e.get("summary")},
                }  # fmt: skip
                for e in doc[ADDONS]
            ]
        rows.append(row)
    return {"markets": rows}

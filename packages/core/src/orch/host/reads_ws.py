"""H6b reads of the workspace itself (§0.8, §0.8.5): ``workspace.show``, ``member.list``, ``session.list``,
``grant.list`` (with the ``read_ext/2`` fields) and ``workspace.settings``.

They read the replayed workspace (:attr:`ReadCtx.workspace`) and, for what the replayed view does not keep (a grant's
``issued_at``, who revoked it, when a setting last changed, the ``auth`` a device signed with), the **verified**
workspace log through :func:`_ws_events`. ``session.list`` and the ``session_ids`` of ``grant.list`` read the host
session records (:mod:`orch.host.sessions`) in one snapshot. Every entry is filtered for the viewer before it is
counted or returned (§0.5).

Decisions the contract leaves open:

* ``workspace.show`` ``name`` is ``workspace.name`` of ``config.json`` (the workspace's display name, in no event), else
  the prefix; ``wsk_fingerprint`` is ``sha256:`` and the first 32 hex digits of SHA-256 over the raw ``wsk_pub`` of the
  genesis event; ``epoch`` is the **committed** membership epoch of §11. P2 publishes no membership list yet, so
  nothing is committed: :func:`committed_epoch` answers ``0`` until the P3 bridge records one.
* ``member.list`` ``devices[].auth`` is the ``auth`` of the latest workspace-log event the device signed (the custody
  label F1 records), ``null`` when it has signed none there. The ``read_ext/3`` device fields (v1.9):
  ``cert_expires_at`` is the F1 certificate's ``expires_ms`` as ``Time``; ``label``, ``paired_at`` and ``relay`` come
  from R6's relay record and ``reader`` from the H12d reader record, neither of which is built yet
  (:func:`relay_devices`, :func:`reader_levels`): until then ``null``, ``null``, ``false`` and ``null``, never a guess.
  ``label`` is for persons only (P, and R, which reads as P): N and G get ``null``.
* ``session.list`` ``claims`` are the tickets whose **live** claim is this session under this grant.
* ``workspace.settings`` binds the first page's ``limit`` into the token: a later page that names another ``limit`` is
  ``invalid.input`` (the only argument that op has); one that names none keeps the first page's.
"""

from __future__ import annotations

import hashlib
from typing import Any

from orch import repopaths
from orch.addons.settings_value import KEY as ADDONS_KEY
from orch.canon import HashError, canonical_policy, check_repo_identity
from orch.model.types import WORKSPACE

from . import sessions
from .errors import HostError
from .reads import ReadCtx, handler, iso

GATES = ("requirements", "plan", "verify", "code")
SETTINGS = ("grant_hours", "claim_ttl_min", "lease_ttl_min")
SESSION_IDS = 100


# --- the verified workspace log --------------------------------------------------------------------------------------


def _ws_events(ctx: ReadCtx, first: int = 1, last: int | None = None) -> list[dict[str, Any]]:
    """Events ``first``..``last`` of the replayed, chain-checked workspace log (all of it by default)."""
    store = ctx.store
    with store.locked():
        info = store._logs.get(WORKSPACE)
        if info is None:
            return []
        end = info.seq if last is None else min(last, info.seq)
        return store._read_events(WORKSPACE, first, end) if end >= first else []


def committed_epoch(ctx: ReadCtx) -> int:
    """The committed membership epoch (§11). P2 has no relay publication, so none is committed yet: ``0``."""
    return 0


def _name(ctx: ReadCtx, prefix: str) -> str:
    from orch import schema
    from orch.store import StoreError, fsio

    try:  # the store's own rules: below its pinned root (#516), no symlink, size limit, bounded nesting, schema-valid
        raw = ctx.store.fs.read_or_none("config.json")
        if raw is None:
            return prefix
        doc = fsio.loads(raw)
        schema.validate("workspace", doc)
        name = doc["workspace"]["name"]  # type: ignore[index]
    except (OSError, ValueError, KeyError, TypeError, RecursionError, schema.SchemaError, StoreError):
        return prefix
    return name if isinstance(name, str) and name else prefix


def _fingerprint(wsk_pub: str) -> str:
    from orch import crypto

    try:
        raw = crypto.unb64u(wsk_pub, crypto.PUB_LEN)
    except Exception:  # noqa: BLE001 - a key that does not decode is fingerprinted as the text it is
        raw = wsk_pub.encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:32]


# --- workspace.show --------------------------------------------------------------------------------------------------


@handler("workspace.show")
def workspace_show(ctx: ReadCtx) -> dict[str, Any]:
    ws = ctx.workspace
    genesis = _ws_events(ctx, 1, 1)
    wsk = genesis[0].get("wsk_pub", "") if genesis else ""
    return {
        "id": ws.workspace_id,
        "prefix": ws.prefix,
        "name": _name(ctx, ws.prefix),
        "genesis": ws.genesis,
        "epoch": committed_epoch(ctx),
        "wsk_fingerprint": _fingerprint(wsk),
        "members_v": ws.roster_v,
        "state": "degraded" if ctx.service.state == "degraded" else "ok",
        "next_key": _next_key(ctx, ws),
        "required_reader": _required(ctx),  # H12d-2 (§0.8.6 item 9.11)
    }


def _required(ctx: ReadCtx) -> int:
    from .readerops import read_required

    return read_required(ctx.service)


def may_create(column: str, role: str | None, relay: Any, ws: Any, now: float) -> bool:
    """May this principal create a ticket (v1.7, §11A.14 14.6)? A person who is a member and not a viewer: column P;
    column R also needs the requesting device valid with ``decide`` on the replayed roster (R2's L2 checks the
    member-list scopes again). Column G and no-person get ``null`` (H12e judgement call: ``next_key`` is the person
    paths' advisory field; an agent's ``new`` allocates its key itself)."""
    from .human import _device_refusal

    if role is None or role == "viewer":
        return False
    if column == "P":
        return True
    if column != "R" or relay is None:
        return False
    dev = ws.devices.get(getattr(relay, "device_id", None))
    person = getattr(relay, "person_id", None)
    if dev is None or (person is not None and dev.person != person):  # the bridge already pins the pair
        return False
    return _device_refusal(dev, now, False) is None


def _next_key(ctx: ReadCtx, ws: Any) -> str | None:
    """``workspace.show.next_key``: the store's exact next key (``_next_key()`` after the directory scan, under the
    lock), advisory and never a reservation; ``null`` for a principal that may not create (:func:`may_create`)."""
    call = ctx.call
    if not may_create(call.column.name, ctx.viewer.role, call.relay, ws, ctx.now):
        return None
    return ctx.store.peek_key()


# --- member.list -----------------------------------------------------------------------------------------------------


def _device_auth(events: list[dict[str, Any]]) -> dict[str, str]:
    """device -> the ``auth`` of the latest workspace-log event it signed."""
    out: dict[str, str] = {}
    for e in events:
        actor = e.get("actor") or {}
        if actor.get("kind") == "person" and isinstance(actor.get("device"), str) and isinstance(e.get("auth"), str):
            out[actor["device"]] = e["auth"]
    return out


def relay_devices(ctx: ReadCtx) -> dict[str, dict[str, Any]]:
    """device -> its R6 relay record (``membership/devices/<id>.json``, 11A.15.3: ``{device_id, person_id, label,
    paired_ms, ...}``), for the devices paired through the relay.

    **R6 is not built**: core has no writer and no reader of that record yet, so no device is relay-paired and this is
    empty. R6 replaces this body with its own read; :func:`member_list` already maps a record to ``label``,
    ``paired_at`` and ``relay``."""
    return {}


def reader_levels(ctx: ReadCtx) -> dict[str, int]:
    """device -> the H12d reader level of its reader record (``readers/<device_id>.json``, §0.8.6 item 9.10), for the
    current devices whose record verifies now (H12d-2, :func:`orch.host.readerops.device_levels`). A device without
    one is absent, so ``member.list`` answers ``reader: null`` (no record), never 0: the gate's "no record counts as 0"
    is the gate's rule, not a level the device declared."""
    from .readerops import device_levels

    return device_levels(ctx.service)


def _label(record: dict[str, Any] | None) -> str | None:
    """The record's label cleaned per protocol §2.5 (:func:`orch.identity.pairing.clean_label`), ``None`` when there
    is no record, no label, or nothing is left after cleaning."""
    from orch.identity.errors import Refused
    from orch.identity.pairing import clean_label

    text = record.get("label") if record else None
    if not isinstance(text, str):
        return None
    try:
        return clean_label(text) or None
    except Refused:
        return None


#: the first epoch ms past ``9999-12-31T23:59:59Z``: a later value has no RFC 3339 ``Time``
_TIME_END_MS = 253402300800000


def _ms_time(value: Any) -> str | None:
    """An epoch-ms value as ``Time`` (whole seconds, like every host read); ``None`` for anything else, and for a value
    outside the RFC 3339 range (before 1970 or after year 9999, which a verified cert may carry): one odd value never
    breaks the read."""
    return iso(value // 1000) if type(value) is int and 0 <= value < _TIME_END_MS else None


@handler("member.list")
def member_list(ctx: ReadCtx) -> dict[str, Any]:
    ws = ctx.workspace
    auth = _device_auth(_ws_events(ctx))
    relay = relay_devices(ctx)
    readers = reader_levels(ctx)
    labels = ctx.viewer.column in {"P", "R"}  # N and G get label null (orch-52, #505); R reads exactly as P (§11A.4)
    now_ms = ctx.now * 1000
    members = []
    for person, member in ws.members.items():
        devices = []
        for dev in ws.devices.values():
            if dev.person != person:
                continue
            valid = not dev.removed and not dev.revoked and (dev.expires_ms is None or dev.expires_ms > now_ms)
            record = relay.get(dev.id)
            if record is not None and record.get("person_id") != dev.person:
                record = None  # a record for another person is not this device's pairing
            devices.append(
                {
                    "id": dev.id,
                    "auth": auth.get(dev.id),
                    "scopes": list(dev.scopes),
                    "valid": valid,
                    # v1.9, read_ext/3 (§0.8.5 "member.list device fields")
                    "label": _label(record) if labels else None,
                    "paired_at": _ms_time(record.get("paired_ms")) if record else None,
                    "relay": record is not None,
                    "cert_expires_at": _ms_time(dev.expires_ms),
                    "reader": readers.get(dev.id),
                }
            )
        devices.sort(key=lambda d: d["id"])
        members.append({"person": person, "name": member.name, "role": member.role, "devices": devices})
    members.sort(key=lambda m: m["person"])
    return {"members": members, "required_reader": _required(ctx)}


# --- reader.list (H12d-2) --------------------------------------------------------------------------------------------


@handler("reader.list")
def reader_list(ctx: ReadCtx) -> dict[str, Any]:
    """§0.8.6 item 9.11: columns P and R only (G and N: ``op.not_allowed``)."""
    from .readerops import listing

    if ctx.viewer.column not in ("P", "R"):
        raise HostError("op.not_allowed", "reader.list is for a person at the terminal, the dashboard or a phone")
    return listing(ctx.service, phone=ctx.viewer.column == "R")  # a phone: no decl, no dk_hash (#513 review)


# --- session.list ----------------------------------------------------------------------------------------------------


def _person_reader(ctx: ReadCtx) -> None:
    if ctx.viewer.column not in ("P", "G"):
        raise HostError("op.not_allowed", f"{ctx.call.frame['op']} is not allowed here")


def _holdings(ctx: ReadCtx) -> tuple[dict[tuple[str, str], list[str]], dict[tuple[str, str], list[str]]]:
    """Live claims and task leases (``KEY/T<n>``) by (grant, session), over the tickets this viewer may see: a
    ticket the viewer cannot see is never part of this."""
    claims: dict[tuple[str, str], list[str]] = {}
    leases: dict[tuple[str, str], list[str]] = {}
    for view in ctx.visible_tickets():
        claim = view.claim
        if claim is not None and claim.live:
            claims.setdefault((claim.grant, claim.session), []).append(view.key)
        for task in view.tasks:
            # a lease records only its session; task events need the claim (model/tasks.py), so the lease belongs to
            # the grant of that claim, and a lease with no such claim to nobody (never to another grant's label)
            if task.leased_by and claim is not None and claim.session == task.leased_by:
                leases.setdefault((claim.grant, task.leased_by), []).append(f"{view.key}/{task.id}")
    return claims, leases


@handler("session.list")
def session_list(ctx: ReadCtx) -> dict[str, Any]:
    _person_reader(ctx)
    active = ctx.args.get("active", True)
    ws = ctx.workspace
    recs = [r for r in sessions.records(ctx.service).snapshot(ctx.now) if ctx.viewer.session_visible(r.person)]
    claims, leases = _holdings(ctx)
    ttl = int(ws.settings["claim_ttl_min"]) * 60
    rows = []
    for r in recs:
        keys = ctx.viewer.claims(ctx.store, claims.get((r.grant_id, r.session), []))
        held = sorted(set(leases.get((r.grant_id, r.session), [])))
        grant = ws.grants.get(r.grant_id)
        if active and not (grant is not None and grant.active and (keys or held or r.last_at > ctx.now - ttl)):
            continue
        item = {
            "session": r.session,
            "agent": r.agent,
            "person": r.person,
            "grant_id": r.grant_id,
            "claims": sorted(keys),
            "leases": held,
            "last_at": iso(r.last_at),
        }
        rows.append((-r.last_at, r.session, r.grant_id, item))
    rows.sort(key=lambda row: row[:3])
    return {"sessions": [row[3] for row in rows]}


# --- grant.list ------------------------------------------------------------------------------------------------------


def _grant_history(
    events: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """``issued_at`` by grant, ``grant.revoked`` by grant (``by``, ``at``, ``seq``), and the events that end grants
    without a ``grant.revoked`` (a member removal)."""
    issued: dict[str, str] = {}
    revoked: dict[str, dict[str, Any]] = {}
    enders: list[dict[str, Any]] = []
    for e in events:
        typ = e.get("type")
        if typ == "grant.issued":
            issued[e["grant"]] = e["issued_at"]
        elif typ == "grant.revoked" and e["grant"] not in revoked:
            revoked[e["grant"]] = {"by": e["actor"]["id"], "at": e["at"], "seq": e["seq"]}
        elif typ == "member.removed":
            enders.append(e)
    return issued, revoked, enders


def _revoked_at(
    gid: str, grant: Any, issued: dict[str, str], revoked: dict[str, dict[str, Any]], enders: list[dict[str, Any]]
) -> str | None:
    """When the grant was revoked: its ``grant.revoked``, else the member removal that ended it, else ``None`` (it is
    not revoked, or the cause is not a workspace-log event naming it)."""
    if gid in revoked:
        return revoked[gid]["at"]
    if not grant.revoked:
        return None
    since = issued.get(gid, "")  # the first removal of the person at or after the grant was issued (never before it)
    return next((e["at"] for e in enders if e.get("person") == grant.person and e["at"] >= since), None)


@handler("grant.list")
def grant_list(ctx: ReadCtx) -> dict[str, Any]:
    _person_reader(ctx)
    active = ctx.args.get("active", True)
    ws = ctx.workspace
    issued, revoked, enders = _grant_history(_ws_events(ctx))
    by_grant: dict[str, list[Any]] = {}
    for r in sessions.records(ctx.service).snapshot(ctx.now):  # one snapshot: `sessions` and `session_ids` agree
        by_grant.setdefault(r.grant_id, []).append(r)
    out = []
    for gid, grant in ws.grants.items():
        if not ctx.viewer.session_visible(grant.person) or (active and not grant.active):
            continue
        recs = sorted(by_grant.get(gid, []), key=lambda r: (-r.last_at, r.session))
        entry: dict[str, Any] = {
            "id": gid,
            "person": grant.person,
            "scope": grant.scope,
            "verbs": grant.verbs if isinstance(grant.verbs, str) else list(grant.verbs),
            "label": grant.label,
            "issued_at": issued.get(gid),
            "expires_at": grant.expires_at,
            "revoked_at": _revoked_at(gid, grant, issued, revoked, enders),
            "sessions": len(recs),
            "session_ids": [r.session for r in recs[:SESSION_IDS]],
        }
        if gid in revoked:
            entry["revoked"] = revoked[gid]
        if len(recs) > SESSION_IDS:
            entry["sessions_more"] = True
        out.append(entry)
    out.sort(key=lambda g: (g["issued_at"] or "", g["id"]))
    return {"grants": out}


# --- workspace.settings ----------------------------------------------------------------------------------------------


def _settings_at(events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, set[str]]:
    """The latest ``settings.changed`` and ``policy.changed`` as ``{seq, at, by}``, and the setting keys any
    ``settings.changed`` has named."""
    latest: dict[str, dict[str, Any]] = {}
    named: set[str] = set()
    for e in events:
        typ = e.get("type")
        if typ in ("settings.changed", "policy.changed"):
            latest[typ] = {"seq": e["seq"], "at": e["at"], "by": e["actor"]["id"]}
        if typ == "settings.changed":
            named |= set(e.get("set") or {})
    return latest.get("settings.changed"), latest.get("policy.changed"), named


def _linked_repos(ctx: ReadCtx) -> set[str]:
    """Repo names in ``links.repos`` of a ticket the viewer may see now."""
    out: set[str] = set()
    for view in ctx.visible_tickets():
        out |= set(((view.fields.get("links") or {}).get("repos")) or [])
    return out


LINKED_KEYS = 12


def _linking_tickets(ctx: ReadCtx) -> dict[str, list[str]]:
    """Repo name to the keys of the tickets (the viewer may see) that link it, at most ``LINKED_KEYS`` each, sorted.
    A display aid for the person about to remove a repo; it decides nothing."""
    out: dict[str, list[str]] = {}
    for view in ctx.visible_tickets():
        for name in ((view.fields.get("links") or {}).get("repos")) or []:
            out.setdefault(name, []).append(view.key)
    return {name: sorted(keys)[:LINKED_KEYS] for name, keys in out.items()}


def _remote(value: Any) -> tuple[bool, str | None]:
    """``(serve, remote)``: a declared remote that is not an F1 canonical identity is not served (never repaired)."""
    if value is None:
        return True, None
    try:
        return True, check_repo_identity(value)
    except HashError:
        return False, None


def _repo_entry(raw: dict[str, Any], manager: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {"default_branch": raw.get("default_branch")}
    if manager:
        entry["path"] = raw.get("path")
        serve, remote = _remote(raw.get("remote"))
        if serve:
            entry["remote"] = remote
    return entry


def sign_methods(service: Any, ws: Any) -> list[str] | None:
    """B6: the workspace's signed ``settings.sign_methods``, or ``None`` when no ``settings.changed`` ever set it.
    ``None`` means "not set": the model then judges no way, so every way is allowed; a reader must not invent a
    narrower default."""
    methods = ws.settings.get("sign_methods")
    return None if methods is None else list(methods)


def _snapshot(ctx: ReadCtx, limit: int) -> dict[str, Any]:
    from orch.store.render import thaw

    cursor = ctx.sub_cursor()  # right before the state is read (H6a)
    ws = ctx.workspace
    settings_at, policy_at, named = _settings_at(_ws_events(ctx))
    return {
        "settings": {k: int(ws.settings[k]) for k in SETTINGS},
        "gates": {g: canonical_policy(thaw(ws.policies[g])) for g in GATES},
        "defaults": [k for k in SETTINGS if k not in named],
        "sign_methods": sign_methods(ctx.service, ws),
        "icon": thaw(ws.settings["icon"]) if "icon" in ws.settings else None,
        "publish": thaw(ws.settings["publish"]) if "publish" in ws.settings else None,
        ADDONS_KEY: thaw(ws.settings[ADDONS_KEY]) if ADDONS_KEY in ws.settings else {},
        "settings_at": settings_at,
        "policy_at": policy_at,
        "sub_cursor": cursor,
        "repos": {name: thaw(raw) for name, raw in sorted(ws.repos.items())},
        "limit": limit,
    }


@handler("workspace.settings")
def workspace_settings(ctx: ReadCtx) -> dict[str, Any]:
    viewer = ctx.viewer
    token = ctx.args.get("page")
    if token is None:
        snap, start = None, 0
        data = _snapshot(ctx, int(ctx.args.get("limit", 32)))
    else:
        snap, start = ctx.pages.resume("workspace.settings", token, ctx.principal, ctx.epoch, viewer.person, ctx.args)
        if "limit" in ctx.args and ctx.args["limit"] != snap.data["limit"]:
            raise HostError("invalid.input", "the page token belongs to a request with other arguments")
        if not isinstance(start, int) or not 0 <= start <= len(snap.data["repos"]):
            raise HostError("page.expired", "the page token is not valid any more: restart from the first page")
        data = snap.data
    limit = int(data["limit"])
    names = list(data["repos"])
    linked = _linked_repos(ctx) if viewer.column == "N" else None  # each page re-applies the current visibility
    page: dict[str, Any] = {}
    nxt: int | None = None
    keyboard = (
        linked is None and viewer.manager and viewer.column == "P"
    )  # the person at the keyboard (R is not served)
    keys_of = _linking_tickets(ctx) if keyboard else {}
    for i in range(start, len(names)):
        name = names[i]
        if linked is not None and name not in linked:
            continue
        if len(page) == limit:
            nxt = i
            break
        raw = data["repos"][name]
        page[name] = (
            {"default_branch": raw.get("default_branch")} if linked is not None else _repo_entry(raw, viewer.manager)
        )
        if keyboard and isinstance(raw.get("path"), str):
            # the person at the keyboard (an owner or maintainer): is the folder there, and which branch (read-only)
            page[name]["check"] = repopaths.observe(ctx.store.layout, raw["path"])
        if keys_of.get(name):
            page[name]["linked_by"] = keys_of[name]
    if nxt is not None and snap is None:  # a snapshot only when a later page can follow
        snap = ctx.pages.open("workspace.settings", ctx.principal, ctx.epoch, viewer.person, data)
    out: dict[str, Any] = {
        "settings": {**data["settings"], "repos": page},
        "next_page": ctx.pages.token(snap, nxt, ctx.args) if nxt is not None else None,
        "gates": data["gates"],
        "defaults": data["defaults"],
        "sign_methods": data["sign_methods"],
        "icon": data["icon"],  # always present (null: none signed): the key means "this host knows the icon setting"
        "sub_cursor": data["sub_cursor"],
    }
    if viewer.column in ("P", "R"):  # the Publish server addresses: for a person (or their phone), not an agent (G, N)
        out["publish"] = data["publish"]
    if viewer.column != "N":
        out[ADDONS_KEY] = data[ADDONS_KEY]  # G6 ruling 5: not secret, see the add-on settings review note
        out["settings_at"] = data["settings_at"]
        out["policy_at"] = data["policy_at"]
    return out

"""Workspace-log events (ticket-format §5.3, §5.4.2, §5.11, D60): genesis, members and roles, devices, grants,
policies, settings, addons, restore.

Events that change what tickets mean (members leaving, role changes, compromised devices, policies, addon binds,
restore) are applied to every ticket in the same step, with one ``settle`` each, so generations follow §5.7.
"""

from __future__ import annotations

import copy
import posixpath
from collections.abc import Callable
from typing import Any

from orch.canon import HashError, event_head

from . import authz, generations, policies
from .codes import Code, Refusal
from .types import GATES, WORKSPACE, Addon, Core, Device, Grant, LogCore, Member, TCore, WsCore, ts
from .verifier import SigContext, Verifier


def _head(e: dict[str, Any]) -> str | None:
    try:
        return event_head(e)
    except (HashError, KeyError):
        return None


def cross(
    core: Core, change: Callable[[], None], per_ticket: Callable[[TCore], None]
) -> list[tuple[str, tuple[str, ...]]]:
    """Apply a workspace change and its effect on every ticket, settling each ticket once."""
    ws = core.ws
    snaps = {uid: generations.snapshot(ws, t) for uid, t in core.tickets.items()}
    change()
    for uid, t in core.tickets.items():
        per_ticket(t)
        generations.settle(ws, t, snaps[uid])
    return []


def _role(ws: WsCore, e: dict[str, Any]) -> str:
    return ws.members[e["actor"]["id"]].role


def _owner_only(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    if _role(ws, e) != "owner":
        return Refusal(Code.ROLE_DENIED, f"{e['type']} is owner only")
    return None


def genesis(core: Core, e: dict[str, Any], v: Verifier, expected: str | None) -> Refusal | None:
    """§5.11 genesis. The cross-field checks (1, 3, 5 partly) are the schema's; the signatures are the verifier's."""
    ws, a = core.ws, e["actor"]
    if e["roster_v"] != 0:
        return Refusal(Code.GENESIS_INVALID, "the genesis has roster_v 0")
    head = _head(e)
    if expected is not None and head is not None and head != expected:
        return Refusal(Code.TRUST_GENESIS_MISMATCH, "the genesis differs from the pinned one")
    cert = e["device_cert"]
    if (r := authz.device_valid(None, cert, ts(e["at"]), False)) is not None:
        return r
    if not v.verify_embedded(e, pk_pub=e["owner"]["pk_pub"]):
        return Refusal(Code.GENESIS_INVALID, "delegation or device certificate does not verify")
    if "sig" not in e or not v.verify_person(e, SigContext(e["workspace_id"], WORKSPACE, cert)):
        return Refusal(Code.SIG_INVALID, "genesis signature does not verify")
    o = e["owner"]
    ws.created = True
    ws.workspace_id, ws.prefix, ws.host_id, ws.wsk_pub, ws.genesis = (
        e["workspace_id"],
        e["prefix"],
        e["host_id"],
        e["wsk_pub"],
        head,
    )
    ws.members[o["person"]] = Member(o["person"], o["name"], "owner", o["pk_pub"])
    ws.roster_v = 1
    ws.devices[a["device"]] = Device(a["device"], o["person"], copy.deepcopy(cert))
    return None


def member_added(core: Core, e: dict[str, Any], v: Verifier) -> Refusal | None:
    ws, p = core.ws, e["person"]
    role = _role(ws, e)
    if role != "owner" and not (role == "maintainer" and e["role"] in ("member", "viewer")):
        return Refusal(Code.ROLE_DENIED, "owners add anyone; maintainers add members and viewers")
    if p in ws.members:
        return Refusal(Code.MEMBER_EXISTS, p)
    cert = e["device_cert"]
    dev = "d_" + cert["o"]["device_id"]
    if dev in ws.devices:
        return Refusal(Code.DEVICE_EXISTS, dev)
    if (r := authz.device_valid(None, cert, ts(e["at"]), False)) is not None:
        return r
    if not v.verify_embedded(e, pk_pub=e["pk_pub"]):
        return Refusal(Code.DEVICE_CERT, "the device certificate is not signed by the person key")
    ws.former.pop(p, None)
    ws.members[p] = Member(p, e["name"], e["role"], e["pk_pub"])
    ws.devices[dev] = Device(dev, p, copy.deepcopy(cert))
    ws.roster_v += 1
    return None


def _last_owner(ws: WsCore, person: str) -> bool:
    return ws.members[person].role == "owner" and sum(m.role == "owner" for m in ws.members.values()) == 1


def member_removed(core: Core, e: dict[str, Any]) -> Refusal | None:
    ws, p = core.ws, e["person"]
    if p not in ws.members:
        return Refusal(Code.MEMBER_UNKNOWN, p)
    role = _role(ws, e)
    if role != "owner" and not (role == "maintainer" and ws.members[p].role in ("member", "viewer")):
        return Refusal(Code.ROLE_DENIED, "owners remove anyone; maintainers remove members and viewers")
    if _last_owner(ws, p):
        return Refusal(Code.MEMBERS_LAST_OWNER, "the last owner can't be removed")

    def change() -> None:
        ws.former[p] = ws.members.pop(p)
        ws.roster_v += 1
        for g in ws.grants.values():  # a removal ends the person's grants and devices; a re-add starts clean (§5.7)
            g.revoked = g.revoked or g.person == p
        for d in ws.devices.values():
            d.removed = d.removed or d.person == p

    cross(core, change, lambda t: generations.void_person(ws, t, p))
    return None


def role_changed(core: Core, e: dict[str, Any]) -> Refusal | None:
    ws, p = core.ws, e["person"]
    if (r := _owner_only(ws, e)) is not None:
        return r
    if p not in ws.members:
        return Refusal(Code.MEMBER_UNKNOWN, p)
    if _last_owner(ws, p) and e["role"] != "owner":
        return Refusal(Code.MEMBERS_LAST_OWNER, "the last owner can't be demoted")

    def change() -> None:
        ws.members[p].role = e["role"]
        ws.roster_v += 1

    cross(core, change, lambda t: generations.void_person(ws, t, p))
    return None


def device_added(core: Core, e: dict[str, Any], v: Verifier) -> Refusal | None:
    ws, a, dev = core.ws, e["actor"], e["device"]
    if dev in ws.devices:
        return Refusal(Code.DEVICE_EXISTS, dev)
    if (r := authz.device_valid(None, e["cert"], ts(e["at"]), False)) is not None:
        return r
    if not v.verify_embedded(e, pk_pub=ws.members[a["id"]].pk_pub):
        return Refusal(Code.DEVICE_CERT, "the device certificate is not signed by the person key")
    ws.devices[dev] = Device(dev, a["id"], copy.deepcopy(e["cert"]))
    return None


def device_removed(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    dev = ws.devices.get(e["device"])
    if dev is None:
        return Refusal(Code.DEVICE_UNKNOWN, e["device"])
    if e["actor"]["id"] != dev.person and _role(ws, e) != "owner":
        return Refusal(Code.ROLE_DENIED, "a device is removed by its person or an owner")
    dev.removed = True
    return None


def device_revoked(core: Core, e: dict[str, Any], v: Verifier) -> Refusal | None:
    ws = core.ws
    dev = ws.devices.get(e["device"])
    if dev is None:
        return Refusal(Code.DEVICE_UNKNOWN, e["device"])
    holder = ws.members.get(dev.person) or ws.former.get(dev.person)
    if holder is None or "p_" + e["revocation"]["o"]["person_id"] != dev.person:
        return Refusal(Code.DEVICE_CERT, "the revocation is not for a device of this person")
    if not v.verify_embedded(e, pk_pub=holder.pk_pub):
        return Refusal(Code.DEVICE_CERT, "the revocation is not signed by the person key")

    def change() -> None:
        dev.revoked = e["reason"]
        if e["reason"] == "compromised":  # also ends every grant that device signed (§5.7)
            for g in ws.grants.values():
                g.revoked = g.revoked or g.device == dev.id

    if e["reason"] == "compromised":
        cross(core, change, lambda t: generations.void_device(ws, t, dev.id))
    else:
        change()
    return None


def policy_changed(core: Core, e: dict[str, Any]) -> Refusal | None:
    ws = core.ws
    if (r := _owner_only(ws, e)) is not None:
        return r
    for g, p in e["gates"].items():
        if (r := policies.check_change(g, p)) is not None:
            return r
    cross(core, lambda: ws.policies.update(copy.deepcopy(e["gates"])), lambda t: generations.mark(t, *e["gates"]))
    return None


def settings_changed(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    if (r := _owner_only(ws, e)) is not None:
        return r
    s = e["set"]
    repos = dict(ws.repos)
    for name, v in s.get("repos", {}).items():
        if v is None:
            repos.pop(name, None)
        else:
            repos[name] = v["path"]
    paths = [posixpath.normpath(p) for p in repos.values()]
    if len(set(paths)) != len(paths):
        return Refusal(Code.SETTINGS_INVALID, "two repos resolve to the same path")
    ws.repos = repos
    for k in ("grant_hours", "claim_ttl_min", "lease_ttl_min"):
        if k in s:
            ws.settings[k] = s[k]
    return None


def grant_issued(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """D60 terms: the role decides scope and length; always for the signer; times are checked on every read."""
    role, hours = _role(ws, e), e["hours"]
    if role == "viewer":
        return Refusal(Code.GRANT_TERMS, "a viewer holds no grant")
    if role == "member" and (e["scope"] != "workable" or hours > ws.settings["grant_hours"]):
        return Refusal(Code.GRANT_TERMS, f"a member's grant is workable and at most {ws.settings['grant_hours']} h")
    issued, expires = ts(e["issued_at"]), ts(e["expires_at"])
    if expires != issued + 3600 * hours or abs(ts(e["at"]) - issued) > 300:
        return Refusal(Code.GRANT_TERMS, "issued_at/expires_at do not match at and hours")
    if e["grant"] in ws.grants:
        return Refusal(Code.GRANT_EXISTS, e["grant"])
    ws.grants[e["grant"]] = Grant(
        e["grant"],
        e["actor"]["id"],
        e["scope"],
        copy.deepcopy(e["verbs"]),
        e["issued_at"],
        e["expires_at"],
        e.get("label"),
        device=e["actor"]["device"],
    )
    return None


def grant_revoked(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    g = ws.grants.get(e["grant"])
    if g is None:
        return Refusal(Code.GRANT_UNKNOWN, e["grant"])
    if _role(ws, e) != "owner" and g.person != e["actor"]["id"]:
        return Refusal(Code.ROLE_DENIED, "owners revoke any grant; others their own")
    if g.revoked:
        return Refusal(Code.GRANT_INVALID, "already revoked")
    g.revoked = True
    return None


def addon_event(core: Core, e: dict[str, Any]) -> Refusal | None:
    ws, typ, name = core.ws, e["type"], e["name"]
    if (r := _owner_only(ws, e)) is not None:
        return r
    old = ws.addons.get(name)
    if typ != "addon.granted" and old is None:
        return Refusal(Code.ADDON_UNKNOWN, name)
    named = generations.addon_binds_gates(old.binds if old else None)
    if typ == "addon.granted":
        binds = copy.deepcopy(e["binds"])
        named |= generations.addon_binds_gates(binds)

        def change() -> None:
            ws.addons[name] = Addon(name, e["version"], e["package_sha256"], list(e["capabilities"]), binds)
    else:

        def change() -> None:
            if typ == "addon.disabled":
                old.enabled = False
            else:
                old.purged = True

    cross(core, change, lambda t: generations.mark(t, *named))
    return None


def restore(core: Core, lc: LogCore, e: dict[str, Any]) -> Refusal | None:
    ws = core.ws
    if (r := _owner_only(ws, e)) is not None:
        return r
    if e["head"] != lc.head or e["from_seq"] != lc.seq:
        return Refusal(Code.RESTORE_BAD_HEAD, "from_seq/head must name the last event on disk")
    cross(core, lambda: None, lambda t: generations.mark(t, *GATES))
    return None

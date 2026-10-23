"""Workspace-log events (ticket-format §5.3, §5.4.2, §5.11, D60): genesis, members and roles, devices, grants,
policies, settings, addons, restore.

Events that change what tickets mean (members leaving, role changes, compromised devices, policies, addon binds,
restore) are applied to every ticket in the same step, raising each gate at most once (§5.7).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from orch.canon import HashError, check_repo_identity, event_head, same_repo_identity

from . import addon_rules, authz, generations, hubledger, identity, policies
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


def _role(ws: WsCore, e: dict[str, Any]) -> str | None:
    """The effective role of the signer of ``e`` through ``identity.signer`` (``None``: none, fail closed)."""
    sg = identity.signer(ws, e)
    return None if isinstance(sg, Refusal) else sg.role


def _owner_only(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    if _role(ws, e) != "owner":
        return Refusal(Code.ROLE_DENIED, f"{e['type']} is owner only")
    return None


def genesis(core: Core, e: dict[str, Any], v: Verifier, expected: str | None) -> Refusal | None:
    """§5.11 genesis. The cross-field checks (1, 3, 5 partly) are the schema's; the signatures are the verifier's."""
    ws, a = core.ws, e["actor"]
    if e["seq"] != 1:  # the schema says so too, but the trust root must not rely on the schema alone
        return Refusal(Code.GENESIS_INVALID, "the genesis is seq 1 of the workspace log")
    if e["roster_v"] != 0:
        return Refusal(Code.GENESIS_INVALID, "the genesis has roster_v 0")
    head = _head(e)
    if expected is not None and head is not None and head != expected:
        return Refusal(Code.TRUST_GENESIS_MISMATCH, "the genesis differs from the pinned one")
    cert = e["device_cert"]
    # check 5 (§5.11): the certificate is valid at this `at` with `operate` (the genesis installs members, policies and
    # settings), whatever device_valid says about it; every failure of checks 1, 2, 3 and 5 is genesis.invalid
    if authz.device_valid(None, cert, ts(e["at"]), True) is not None or a["device"] != "d_" + cert["o"]["device_id"]:
        return Refusal(
            Code.GENESIS_INVALID, "the genesis device certificate is expired, lacks `operate` or has a drop: scope"
        )
    if not v.verify_embedded(e, pk_pub=e["owner"]["pk_pub"]):
        # the verifier reports the first failing check: checks 1, 2, 3 and 5 are genesis.invalid, check 6 sig.invalid
        why = v.genesis_failure(e)
        if why == "genesis.bad_sig":
            return Refusal(Code.SIG_INVALID, "genesis signature does not verify")
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
    if dev in ws.devices or dev in ws.revocations:
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
    if dev in ws.devices or dev in ws.revocations:
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
    device = e["device"]
    dev = ws.devices.get(device)
    person = "p_" + e["revocation"]["o"]["person_id"]
    known = ws.revocations.get(device, {}).get(person)
    if dev is None and device not in ws.revocations:
        return Refusal(Code.DEVICE_UNKNOWN, device)
    if dev is None and known is None:
        # A tombstone holds evidence of other persons only: this person's revocation has no evidence to verify against.
        return Refusal(Code.DEVICE_CERT, "the revocation is not for a device of this person")
    if dev is not None:
        holder = ws.members.get(dev.person) or ws.former.get(dev.person)
        if holder is None:
            return Refusal(Code.DEVICE_CERT, "the device has no person key")
        cert, pk_pub, previous = dev.cert, holder.pk_pub, dev.revoked
        if person != dev.person:
            return Refusal(Code.DEVICE_CERT, "the revocation is not for a device of this person")
    else:
        # A restore's tombstone supplies verification evidence, never membership or actor authority.
        cert, pk_pub = known["device_cert"], known["pk_pub"]
        previous = known["revocation"]["o"]["reason"]
    if not v.verify_embedded(e, pk_pub=pk_pub, device_cert=cert):
        return Refusal(Code.DEVICE_CERT, "the revocation is not signed by the person key")
    if previous and not (previous != "compromised" and e["reason"] == "compromised"):
        return Refusal(Code.DEVICE_EXISTS, device)

    def change() -> None:
        if dev is not None:
            dev.revoked = e["reason"]
        ws.revocations.setdefault(device, {})[person] = {
            "revocation": copy.deepcopy(e["revocation"]),
            "device_cert": copy.deepcopy(cert),
            "pk_pub": pk_pub,
        }
        if e["reason"] == "compromised":
            for grant in ws.grants.values():
                grant.revoked = grant.revoked or grant.device == device

    if e["reason"] == "compromised":
        cross(core, change, lambda ticket: generations.void_device(ws, ticket, device))
    else:
        change()
    return None


def hub_pinned(core: Core, e: dict[str, Any], v: Verifier, *, dev: bool) -> Refusal | None:
    """``hub.pinned`` (15.2) and its one effect on tickets (C3b-2): hub contract hv 0 carries no reason, so every
    channel revocation is treated like a compromised device. At the pin that FIRST adds channel X to the ledger's
    ``revoked_channels``, the counting decisions signed by ``hub:<hub_id>/X`` are voided (``void_signer``), on every
    unsettled ticket, exactly as ``device.revoked`` voids a device's. An op-cert revocation, a new card and a snapshot
    that drops a user or lowers a role void nothing (like ``role.changed`` for the decisions already taken)."""
    ws, hub_id = core.ws, e["hub_id"]
    led = hubledger.ledger_of(ws, hub_id)
    before = set(led["revoked_channels"]) if led else set()
    if (r := hubledger.apply_pin(ws, e, v, dev=dev)) is not None:
        return r
    led = hubledger.ledger_of(ws, hub_id)
    keys = [f"hub:{hub_id}/{c}" for c in sorted(set(led["revoked_channels"]) - before)] if led else []
    if keys:
        cross(core, lambda: None, lambda t: [generations.void_signer(ws, t, k) for k in keys])
    return None


def policy_changed(core: Core, e: dict[str, Any]) -> Refusal | None:
    ws = core.ws
    if (r := _owner_only(ws, e)) is not None:
        return r
    for g, p in e["gates"].items():
        if (r := policies.check_change(g, p)) is not None:
            return r
    gates = policies.channel_gates(e, e["gates"], ws.policies)
    cross(core, lambda: ws.policies.update(gates), lambda t: generations.mark(t, *e["gates"]))
    return None


#: the ways to sign, in the canonical order a stored ``sign_methods`` has (B6); ``touchid`` is reserved
SIGN_METHODS = ("terminal", "click", "phone", "hub")


def settings_changed(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    if (r := _owner_only(ws, e)) is not None:
        return r
    s = e["set"]
    repos = dict(ws.repos)
    for name, v in s.get("repos", {}).items():
        if v is None:
            repos.pop(name, None)
        else:
            if "remote" in v:
                try:
                    check_repo_identity(v["remote"])
                except HashError as exc:
                    return Refusal(Code.SETTINGS_INVALID, str(exc))
                if v["remote"].startswith("local:") and v["remote"] != f"local:{name}":
                    return Refusal(Code.SETTINGS_INVALID, "local remote must name its own repo")
            repos[name] = copy.deepcopy(v)
    remotes = [p["remote"] for p in repos.values() if "remote" in p]
    if any(same_repo_identity(a, b) for i, a in enumerate(remotes) for b in remotes[i + 1 :]):
        return Refusal(Code.SETTINGS_INVALID, "two repos declare the same identity")
    ws.repos = repos
    for k in ("grant_hours", "claim_ttl_min", "lease_ttl_min"):
        if k in s:
            ws.settings[k] = s[k]
    if "sign_methods" in s:  # B6: terminal always; touchid is reserved (the schema refuses it); canonical order
        methods = set(s["sign_methods"])
        if "terminal" not in methods:
            return Refusal(Code.SETTINGS_INVALID, "sign_methods always contains terminal")
        ws.settings["sign_methods"] = [m for m in SIGN_METHODS if m in methods]
    if "publish" in s:  # the Publish add-on's server: one signed place for shared files to go; null removes it
        server = s["publish"]
        if server is None:
            ws.settings.pop("publish", None)
        else:
            if server["server"] == server["share"]:
                return Refusal(Code.SETTINGS_INVALID, "publish: the share address must differ from the server address")
            ws.settings["publish"] = dict(server)
    if "models" in s:  # Model routing: harness -> mode -> model; null clears; each value is a name of the closed list
        table = s["models"]
        if table is None:
            ws.settings.pop("models", None)
        else:
            from orch.modelchoices import MODEL_CHOICES

            for harness, per in table.items():
                if harness not in MODEL_CHOICES or any(m not in MODEL_CHOICES[harness] for m in per.values()):
                    return Refusal(Code.SETTINGS_INVALID, "models: a model that orch does not offer for its harness")
            ws.settings["models"] = {h: dict(sorted(per.items())) for h, per in sorted(table.items()) if per}
    if (
        "icon" in s
    ):  # the workspace icon: a pair of ids of the closed lists; null clears it (the default comes from the id)
        choice = s["icon"]
        if choice is None:
            ws.settings.pop("icon", None)
        else:
            from orch.wsicons import COLOUR_LABELS, ICON_LABELS

            if (
                not isinstance(choice, dict)
                or set(choice) != {"icon", "colour"}
                or choice["icon"] not in ICON_LABELS
                or choice["colour"] not in COLOUR_LABELS
            ):
                return Refusal(Code.SETTINGS_INVALID, "icon: an icon and a colour that orch offers")
            ws.settings["icon"] = {"colour": choice["colour"], "icon": choice["icon"]}
    return None


def _grant_terms(ws: WsCore, role: str | None, scope: Any, hours: int) -> Refusal | None:
    """D60 terms for a grant by a signer of ``role`` (the person event and the C10 request share them)."""
    if role is None:
        return Refusal(Code.MEMBER_UNKNOWN, "the signer has no role")
    if role == "viewer":
        return Refusal(Code.GRANT_TERMS, "a viewer holds no grant")
    if role == "member" and (scope != "workable" or hours > ws.settings["grant_hours"]):
        return Refusal(Code.GRANT_TERMS, f"a member's grant is workable and at most {ws.settings['grant_hours']} h")
    return None


def grant_requested(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """``grant.requested`` (hub protocol 8.8, C10): a hub-signed ask for an agent grant. It changes no grant by itself;
    it records the terms, the canonical signer and the role the host will use for the host-actor ``grant.issued`` that
    answers it. The grant is for the signer, so the signer must be a member (an aliased hub user): a hub user with no
    member behind the alias could never run an agent (``agent.for`` is a member), so the request is refused."""
    sg = identity.signer(ws, e)
    if isinstance(sg, Refusal):
        return sg
    if (r := _grant_terms(ws, sg.role, e["scope"], e["hours"])) is not None:
        return r
    if sg.person not in ws.members:
        return Refusal(Code.MEMBER_UNKNOWN, "a grant is for a member: alias this hub user to a person first")
    if e["id"] in ws.requests:
        return Refusal(Code.GRANT_EXISTS, e["id"])
    ws.requests[e["id"]] = {
        "person": sg.person,
        "role": sg.role,
        "scope": e["scope"],
        "verbs": copy.deepcopy(e["verbs"]),
        "hours": e["hours"],
        "label": e.get("label"),
        "at": e["at"],
        "hub_id": e["hub_id"],
        "used": False,
    }
    return None


def _host_grant(ws: WsCore, e: dict[str, Any]) -> tuple[dict[str, Any] | None, Refusal | None]:
    """The host-actor ``grant.issued`` (C10): valid only when it names an admitted ``grant.requested`` of the same
    terms, not answered yet, ``0 <= at - request.at <= 300 s``, ``issued_at = min(request.at, at)``.
    Returns the request."""
    req = ws.requests.get(e.get("request"))
    if req is None:
        return None, Refusal(Code.GRANT_TERMS, "a host-issued grant names an admitted grant.requested")
    if req["used"]:
        return None, Refusal(Code.GRANT_TERMS, "this request was answered already")
    same = (
        e["scope"] == req["scope"]
        and e["verbs"] == req["verbs"]
        and e["hours"] == req["hours"]
        and e.get("label") == req["label"]
    )
    if not same:
        return None, Refusal(Code.GRANT_TERMS, "the grant is not what was requested")
    lag = ts(e["at"]) - ts(req["at"])
    if not 0 <= lag <= 300 or ts(e["issued_at"]) != min(ts(req["at"]), ts(e["at"])):
        return None, Refusal(Code.GRANT_TERMS, "the request is too old (300 s) or issued_at is not min(request, now)")
    return req, None


def grant_issued(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """D60 terms: the role decides scope and length; always for the signer; times are checked on every read.

    C10 (hub protocol 8.8, P2-c): a *host* actor appends it too, only answering a ``grant.requested``. Then the
    person is the request's canonical signer, the role is that signer's at the request, there is no device (the
    runner binds the secret instead), and the grant belongs to the request's hub (``Grant.hub_id``) so that leaving it
    ends the grant."""
    req = None
    if e["actor"]["kind"] == "host":
        req, r = _host_grant(ws, e)
        if r is not None:
            return r
        role, person, device, hub_id = req["role"], req["person"], "", req["hub_id"]
    else:
        if "request" in e:
            return Refusal(Code.GRANT_TERMS, "only the host answers a request")
        role, person, device, hub_id = _role(ws, e), e["actor"]["id"], e["actor"]["device"], None
    hours = e["hours"]
    if (r := _grant_terms(ws, role, e["scope"], hours)) is not None:
        return r
    issued, expires = ts(e["issued_at"]), ts(e["expires_at"])
    if expires != issued + 3600 * hours or abs(ts(e["at"]) - issued) > 300:
        return Refusal(Code.GRANT_TERMS, "issued_at/expires_at do not match at and hours")
    if e["grant"] in ws.grants:
        return Refusal(Code.GRANT_EXISTS, e["grant"])
    if req is not None:
        req["used"] = True
    ws.grants[e["grant"]] = Grant(
        e["grant"],
        person,
        e["scope"],
        copy.deepcopy(e["verbs"]),
        e["issued_at"],
        e["expires_at"],
        e.get("label"),
        device=device,
        hub_id=hub_id,
    )
    return None


def grant_revoked(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """Owners revoke any grant; others their own. "Own" is the grant's person against the **canonical** person of the
    signer (C10: a hub user aliased to the person revokes that person's grants)."""
    g = ws.grants.get(e["grant"])
    if g is None:
        return Refusal(Code.GRANT_UNKNOWN, e["grant"])
    sg = identity.signer(ws, e)
    if isinstance(sg, Refusal):
        return sg
    if sg.role != "owner" and g.person != sg.person:
        return Refusal(Code.ROLE_DENIED, "owners revoke any grant; others their own")
    if g.revoked:
        return Refusal(Code.GRANT_INVALID, "already revoked")
    g.revoked = True
    return None


def device_excluded(core: Core, e: dict[str, Any]) -> Refusal | None:
    """``device.excluded`` (hub protocol 8.8, C10): a workspace-local act of an ``admin`` of the hub. The device signs
    nothing more in this workspace; ``compromised`` also ends its grants and voids its open decisions as a compromised
    device does (ticket-format 5.7). **Refused for a device of a workspace owner** (L2-P5-2): owner devices go only
    through the person's own PK-signed ``device.revoked``, so the hub cannot block the owner's break-glass."""
    ws = core.ws
    sg = identity.signer(ws, e)
    if isinstance(sg, Refusal):
        return sg
    if sg.role != "owner":
        return Refusal(Code.ROLE_DENIED, "device.excluded is for an admin of the hub")
    dev = ws.devices.get(e["device"])
    if dev is None:
        return Refusal(Code.DEVICE_UNKNOWN, e["device"])
    holder = ws.members.get(dev.person)
    if holder is None or holder.role == "owner":
        return Refusal(Code.ROLE_DENIED, "an owner's device is revoked by its person key, not excluded by a channel")
    if dev.removed or dev.revoked:
        return Refusal(Code.DEVICE_EXISTS, e["device"])

    def change() -> None:
        dev.removed = True
        if e["reason"] == "compromised":
            for grant in ws.grants.values():
                grant.revoked = grant.revoked or grant.device == e["device"]

    if e["reason"] == "compromised":
        cross(core, change, lambda ticket: generations.void_device(ws, ticket, e["device"]))
    else:
        change()
    return None


def addon_event(core: Core, e: dict[str, Any]) -> Refusal | None:
    ws, typ, name = core.ws, e["type"], e["name"]
    if (r := _owner_only(ws, e)) is not None:
        return r
    old = ws.addons.get(name)
    if typ != "addon.granted" and old is None:
        return Refusal(Code.ADDON_UNKNOWN, name)
    named = generations.addon_binds_gates(old.binds if old else None)  # the gates it named before
    if typ == "addon.granted":
        if (r := addon_rules.check_headings(name, e["sections"], ws.addons)) is not None:
            return r
        new = Addon(
            name,
            e["version"],
            e["package_sha256"],
            list(e["capabilities"]),
            copy.deepcopy(e["fields"]),
            copy.deepcopy(e["sections"]),
            list(e["artifact_kinds"]),
        )
        named |= generations.addon_binds_gates(new.binds)

        def change() -> None:
            ws.addons[name] = new
    else:

        def change() -> None:
            if typ == "addon.disabled":
                old.enabled = False
            else:
                old.purged = True

    def per_ticket(t: TCore) -> None:
        if typ == "addon.purged":  # §8.1: purge removes the addon's data from the derived state of every ticket
            t.fields["addons"].pop(name, None)
            for sid in [s for s in t.sections if s.startswith(name + ".")]:
                del t.sections[sid]
            for aname in [n for n, a in t.artifacts.items() if a.addon == name]:
                del t.artifacts[aname]
        elif typ == "addon.granted":  # §8.1: stored values the new grant does not allow are dropped (events stay)
            data = t.fields["addons"].get(name, {})
            for fname in [f for f, v in data.items() if not addon_rules.holds_value(new.fields.get(f), v)]:
                del data[fname]
            if not data:
                t.fields["addons"].pop(name, None)
            allowed = {s["id"]: set(s["types"]) for s in new.sections}
            for sid in [s for s in t.sections if s.startswith(name + ".")]:
                if t.ticket_type not in allowed.get(sid, ()):
                    del t.sections[sid]
        generations.mark(t, *named)

    cross(core, change, per_ticket)
    return None


def restore(core: Core, lc: LogCore, e: dict[str, Any], v: Verifier) -> Refusal | None:
    ws = core.ws
    if (r := _owner_only(ws, e)) is not None:
        return r
    if e["head"] != lc.head or e["from_seq"] != lc.seq:
        return Refusal(Code.RESTORE_BAD_HEAD, "from_seq/head must name the last event on disk")
    changes = []
    # Entries are unique per (device, person) (§5.10). An entry for a known device must be of its person and key; an
    # entry for a pair a tombstone already holds must be under the same key. A device without a live association
    # (unknown, or tombstone only) may carry several persons' evidence: every pair is kept, none chosen over another.
    for entry in e["revocations"]:
        rev = entry["revocation"]
        dev_id, person = "d_" + rev["o"]["device_id"], "p_" + rev["o"]["person_id"]
        if not v.verify_embedded({"type": "restore.revocation", **entry}, pk_pub=entry["pk_pub"]):
            return Refusal(Code.DEVICE_CERT, "restore revocation evidence does not verify")
        dev = ws.devices.get(dev_id)
        if dev is not None:
            holder = ws.members.get(dev.person) or ws.former.get(dev.person)
            known_person, pk_pub = dev.person, holder.pk_pub if holder else None
        elif person in ws.revocations.get(dev_id, {}):
            known_person, pk_pub = person, ws.revocations[dev_id][person]["pk_pub"]
        else:
            continue  # a new pair: an unknown device, or a tombstone holding only other persons' evidence
        if person != known_person or entry["pk_pub"] != pk_pub:
            return Refusal(Code.DEVICE_CERT, "the revocation is not for the device's known person key")
    for entry in e["revocations"]:
        rev = entry["revocation"]
        dev_id, person = "d_" + rev["o"]["device_id"], "p_" + rev["o"]["person_id"]
        old = ws.revocations.get(dev_id, {}).get(person)
        if old is not None:
            previous = old["revocation"]
            if previous == rev:
                continue
            if previous["o"]["reason"] == "compromised" or rev["o"]["reason"] != "compromised":
                return Refusal(Code.DEVICE_EXISTS, dev_id)
        changes.append((dev_id, person, entry))
    cross(core, lambda: None, lambda t: generations.mark(t, *GATES))
    for dev_id, person, entry in changes:
        ws.revocations.setdefault(dev_id, {})[person] = copy.deepcopy(entry)
        reason = entry["revocation"]["o"]["reason"]
        dev = ws.devices.get(dev_id)
        if dev is not None:
            dev.revoked = reason
        if reason == "compromised":
            for grant in ws.grants.values():
                grant.revoked = grant.revoked or grant.device == dev_id
            for ticket in core.tickets.values():
                before = generations.snapshot(ws, ticket)
                generations.void_device(ws, ticket, dev_id)
                generations.settle(ws, ticket, before, raise_gates=False)
    return None


# --- the hub (hub contract H0 section 9.1; C2a selective, C2b full) -------------------------------------------------

#: modes the model accepts. Full mode (D74) makes the hub a member of kind ``hub``: ``ws.hub["member"]`` is set by a
#: full ``hub.joined`` and gone with ``hub.left``; selective mode never has one (D80).
HUB_MODES = ("selective", "full")


def hub_joined(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """``hub.joined`` (owner only, owner-signed): a new binding of the one hub. A selective one adds no member (D80);
    a full one makes the hub a member of kind ``hub`` (D74). ``binding_seq`` is strictly higher than any earlier one
    (across joins, mode switches and rejoins, 9.2); an earlier join must have ended with ``hub.left``, except the one
    switch the contract allows while joined: selective to full, naming the same hub (9.1; full to selective is
    ``hub.left`` and a new selective join, so the hub loses ``WK_e`` from the new epoch on). Aliases are never
    re-pointed, in either direction (9.1)."""
    if (r := _owner_only(ws, e)) is not None:
        return r
    h = ws.hub
    if e["mode"] not in HUB_MODES:
        return Refusal(Code.HUB_INVALID, "unknown hub mode")
    if h and h["status"] == "joined":
        switch = h["mode"] == "selective" and e["mode"] == "full" and e["hub_id"] == h["hub_id"]
        if not switch or e["hk_root_pub"] != h["hk_root_pub"]:
            return Refusal(Code.HUB_INVALID, "this workspace is already joined to a hub; leave it first")
    if e["binding_seq"] <= h.get("binding_seq", 0):
        return Refusal(Code.HUB_INVALID, "binding_seq must be higher than every earlier binding of this workspace")
    # aliases belong to their hub (L2P4-C1): a rejoin restores this hub's own, another hub starts empty
    book = {k: dict(v) for k, v in h.get("alias_book", {}).items()}
    aliases = dict(book.get(e["hub_id"], {}))
    users = {u: p for p, u in aliases.items()}
    for a in e["aliases"]:
        pid, uid = a["person_id"], a["user_id"]
        if aliases.get(pid, uid) != uid or users.get(uid, pid) != pid:
            return Refusal(Code.HUB_INVALID, "an alias is never re-pointed")
        aliases[pid], users[uid] = uid, pid
    ledger = {k: dict(v) for k, v in h.get("ledger", {}).items()}
    if e["hub_id"] in ledger and ledger[e["hub_id"]]["root"] != e["hk_root_pub"]:
        return Refusal(Code.HUB_INVALID, "root_mismatch")  # the hub id is derived from the root: it never changes
    ledger.setdefault(e["hub_id"], hubledger.new_ledger(e["hk_root_pub"]))
    # the union over all hubs must stay a function: a user id another hub aliased to someone else is refused (L2P4-C1b)
    other = {u: p for hid, al in book.items() if hid != e["hub_id"] for p, u in al.items()}
    for a in e["aliases"]:
        if other.get(a["user_id"], a["person_id"]) != a["person_id"]:
            return Refusal(Code.HUB_INVALID, "this user id is already aliased to another person under another hub")
    # C3b-5 (§15.6): a u_ id belongs to the hub it first appeared under. An alias on this hub may not take an id that
    # first appeared under another hub, unless that hub's own book maps the id to the same person.
    user_hub = dict(h.get("user_hub", {}))
    for a in e["aliases"]:
        first = user_hub.get(a["user_id"])
        if first is not None and first != e["hub_id"] and book.get(first, {}).get(a["person_id"]) != a["user_id"]:
            return Refusal(Code.HUB_INVALID, "foreign_user")
    for a in e["aliases"]:
        user_hub.setdefault(a["user_id"], e["hub_id"])
    if any("p_" + pid not in ws.members for pid in aliases):
        return Refusal(Code.HUB_INVALID, "an alias names a person who is not a member")
    ws.hub = {
        "hub_id": e["hub_id"],
        "hk_root_pub": e["hk_root_pub"],
        "mode": e["mode"],
        "binding_seq": e["binding_seq"],
        "status": "joined",
        "aliases": aliases,
        "alias_book": {**book, e["hub_id"]: dict(aliases)},
        "ledger": ledger,
        "user_hub": user_hub,
        **({"joined_at": e["at"]} if "at" in e else {}),  # C8 P2-b: nothing made before this binding lands under it
        **({"member": {"kind": "hub", "hub_id": e["hub_id"]}} if e["mode"] == "full" else {}),
    }
    return None


def hub_left(ws: WsCore, e: dict[str, Any], core: Core | None = None) -> Refusal | None:
    """``hub.left`` (owner only): ends the binding it names. It must name the hub and binding that are joined;
    ``binding_seq`` stays as the high-water mark, so no earlier binding can come back.

    C8 (design-c8 M2.2): from this line the hub's power is gone. Hub contract hv 0 has no reason field, so every leave
    is treated as a compromise: the counting decisions signed by ``hub:<hub_id>/*`` in a generation that is still open
    are voided on every unsettled ticket (``void_hub``; consumed generations stay, as for a device), and every grant
    whose request was a line of this hub ends (``Grant.hub_id``; state, not a new line, L2-P5-1)."""
    if (r := _owner_only(ws, e)) is not None:
        return r
    h = ws.hub
    if not h or h["status"] != "joined":
        return Refusal(Code.HUB_INVALID, "this workspace is not joined to a hub")
    if e["hub_id"] != h["hub_id"] or e["binding_seq"] != h["binding_seq"]:
        return Refusal(Code.HUB_INVALID, "hub.left must name the hub and binding that are joined")
    hub_id = e["hub_id"]

    def change() -> None:
        ws.hub = {
            **{k: v for k, v in h.items() if k != "member"},
            "status": "left",
        }  # the hub member goes with the leave
        for grant in ws.grants.values():
            if grant.hub_id == hub_id:
                grant.revoked = True

    if core is None:
        change()
    else:
        cross(core, change, lambda t: generations.void_hub(ws, t, hub_id))
    return None

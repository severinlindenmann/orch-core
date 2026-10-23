"""Authorization replay (ticket-format §5.2, §5.3, §5.11): who may append what, checked identically at append
(``admit``) and on every read (``replay``).

Checks in order: the actor kind may append the type; person events: the signing device's certificate is valid at the
event's position and has the scope, ``roster_v`` is current, the signature verifies; agent events: the grant was
valid, in scope and covered the verb; unattended events: the three types, workspace-visible tickets, quotas.
Role rules that belong to one type live with that type's handler.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from . import breakglass, hubledger, identity, policies, visibility
from .codes import Code, Refusal
from .emits import HUMAN_ONLY
from .types import WORKSPACE, Core, Device, TCore, WsCore, ts
from .verifier import SigContext, Verifier

# P person (signed), A agent with a grant or a person, U also an unattended agent, G agent with a grant only,
# H host. Both logs where a type exists in both (§5.4).
ACTOR_RULES: dict[str, str] = {
    "ticket.created": "PA",
    "ticket.updated": "PA",
    "status.changed": "PA",
    "ticket.submitted": "PA",
    "ticket.closed": "P",
    "ticket.reopened": "P",
    "ticket.moved": "P",
    "visibility.changed": "P",
    "people.changed": "P",
    "policy.changed": "P",
    "claim.taken": "G",
    "claim.released": "PAH",
    "task.started": "G",
    "task.done": "PA",
    "task.skipped": "PA",
    "task.blocked": "PA",
    "task.reopened": "PA",
    "handoff.written": "PA",
    "log.added": "PAU",
    "question.asked": "PAU",
    "question.answered": "P",
    "gate.approved": "P",
    "gate.changes_requested": "P",
    "verdict.given": "P",
    "gate.invalidated": "H",
    "branch.pushed": "H",
    "artifact.added": "PAU",
    "artifact.replaced": "PA",
    "edit.external": "H",
    "projection.repaired": "H",
    "restore": "P",
    "invalid.acknowledged": "P",
    "workspace.created": "P",
    "member.added": "P",
    "member.removed": "P",
    "role.changed": "P",
    "device.added": "P",
    "device.removed": "P",
    "device.revoked": "PH",
    "settings.changed": "P",
    "grant.issued": "PH",  # C10: a host actor only with `request` (hub protocol 8.8), checked by the type rule
    "grant.requested": "P",  # C10: hub-signed only (HUB_ONLY)
    "grant.revoked": "P",
    "device.excluded": "P",  # C10: hub-signed only (HUB_ONLY)
    "addon.granted": "P",
    "addon.disabled": "P",
    "addon.purged": "P",
    "hub.joined": "P",
    "hub.left": "P",
    "relay.reopened": "P",  # C10 M4: the owner reopens the direct path, after leaving the hub
    "hub.pinned": "H",
}
OPERATE = frozenset(
    {
        "member.added",
        "member.removed",
        "role.changed",
        "policy.changed",
        "settings.changed",
        "grant.issued",
        "grant.revoked",
        "addon.granted",
        "addon.disabled",
        "addon.purged",
        "hub.joined",
        "hub.left",
        "relay.reopened",
        "restore",
        "invalid.acknowledged",
    }
)
#: the event types a hub actor may append (hub contract 8.5, ticket-format 15.4): the type rules of exactly these go
#: through ``identity.signer`` / ``role_of``. Anything else is refused here even when the injected verifier allows it.
HUB_TYPES = frozenset(
    {
        "ticket.created", "ticket.updated", "status.changed", "ticket.submitted", "ticket.closed", "ticket.reopened",
        "claim.released", "log.added", "handoff.written", "question.asked", "question.answered", "gate.approved",
        "gate.changes_requested", "verdict.given", "ticket.moved", "people.changed", "visibility.changed",
        "addon.disabled", "addon.purged",
        # phase 5 (hub protocol 8.8, V3-D29)
        "grant.requested", "grant.revoked", "device.excluded", "policy.changed", "settings.changed",
    }
)  # fmt: skip
#: C10: types that exist only as hub-signed requests; a device-signed one (no operation writes it) is refused
HUB_ONLY = frozenset({"grant.requested", "device.excluded"})
DECISIONS = frozenset({"gate.approved", "gate.changes_requested", "verdict.given", "question.answered"})
VIEWER_MAY = frozenset({"question.answered", "device.added", "device.removed", "device.revoked"})
UNATTENDED_PER_SESSION = 30
UNATTENDED_PER_WORKSPACE = 120
BYTES_PER_SESSION = 20 * 1024 * 1024
BYTES_PER_WORKSPACE = 100 * 1024 * 1024
WINDOW = 3600


def _actor_letter(a: dict[str, Any]) -> str:
    if a["kind"] == "person":
        return "P"
    if a["kind"] == "host":
        return "H"
    return "U" if a.get("unattended") is True else "G"


def check_actor_kind(e: dict[str, Any]) -> Refusal | None:
    rule = ACTOR_RULES.get(e["type"])
    if rule is None:
        return Refusal(Code.EVENT_UNKNOWN_TYPE, e["type"])
    letter = _actor_letter(e["actor"])
    allowed = letter in rule or (letter == "G" and "A" in rule)
    if not allowed:
        return Refusal(Code.EVENT_BAD_ACTOR, f"a {e['actor']['kind']} actor may not append {e['type']}")
    return None


def device_valid(dev: Device | None, cert: dict[str, Any], at: int, operate: bool) -> Refusal | None:
    o = cert["o"]
    if dev is not None and (dev.removed or dev.revoked):
        return Refusal(Code.DEVICE_INVALID, "the device was removed or revoked")
    if o["expires_ms"] is not None and o["expires_ms"] <= at * 1000:
        return Refusal(Code.DEVICE_INVALID, "the device certificate has expired")
    scopes = o["scopes_max"]
    if (
        any(str(s).startswith("drop:") for s in scopes)
        or "decide" not in scopes
        or (operate and "operate" not in scopes)
    ):
        return Refusal(Code.DEVICE_SCOPE, "the certificate lacks the scope this event needs")
    return None


#: B6: the ``auth`` labels the workspace setting ``sign_methods`` governs, and the method each one is: a convenience
#: signature is "one click on this computer", the iPhone app's Secure Enclave signature is "the phone". The other labels
#: (passphrase is the terminal, always allowed; the Mac and cross-platform ones are reserved) are not judged here.
AUTH_METHOD = {"convenience": "click", "secure-enclave-unlocked": "phone", "hub": "hub"}


def method_refusal(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """A person event made another way than the workspace's signed ``sign_methods`` allows. Judged by the workspace
    state at this event's own position (append and replay alike): an event from before the setting, or from before a
    change of it, stays valid. No setting: every way is allowed, as before."""
    methods = ws.settings.get("sign_methods")
    need = AUTH_METHOD.get(str(e.get("auth")))
    if methods is None or need is None or need in methods:
        return None
    return Refusal(Code.SIGN_METHOD, f"auth {e['auth']} ({need}) is not an allowed way to sign in this workspace")


def person_has_valid_device(ws: WsCore, person: str, at: int) -> bool:
    return any(d.person == person and device_valid(d, d.cert, at, False) is None for d in ws.devices.values())


def authorize_person(core: Core, log: str, e: dict[str, Any], v: Verifier, *, hub_only: bool = False) -> Refusal | None:
    ws, a, typ, at = core.ws, e["actor"], e["type"], ts(e["at"])
    m = ws.members.get(a["id"])
    if m is None:
        return Refusal(Code.MEMBER_UNKNOWN, f"{a['id']} is not a member")
    dev = ws.devices.get(a["device"])
    recovery = breakglass.is_recovery(ws, e)
    if recovery:
        cert = e["cert"]
    elif dev is None or dev.person != a["id"]:
        return Refusal(Code.DEVICE_UNKNOWN, f"{a['device']} is not a device of {a['id']}")
    else:
        cert = dev.cert
    if (r := device_valid(dev, cert, at, typ in OPERATE)) is not None:
        return r
    if (r := method_refusal(ws, e)) is not None:
        return r
    if (
        hub_only and (r := breakglass.local_refusal(ws, e)) is not None
    ):  # C8/D78: a joined full-mode position signs through the hub
        return r
    if m.role == "viewer" and typ not in VIEWER_MAY:
        return Refusal(Code.ROLE_DENIED, "a viewer cannot write")
    if e["roster_v"] != ws.roster_v:
        return Refusal(Code.MEMBERS_STALE, f"roster_v {e['roster_v']} is not current ({ws.roster_v})")
    if "sig" not in e:
        return Refusal(Code.SIG_INVALID, "person event without sig")
    if not v.verify_person(e, SigContext(ws.workspace_id, log, cert)):
        return Refusal(Code.SIG_INVALID, "person signature does not verify")
    return None


#: the hub contract codes by where they arise in section 15.3 (the verifier returns the contract code only)
_HUB_INVALID_CODES = frozenset({"malformed", "unavailable", "other_hub", "other_workspace", "not_joined", "wrong_mode"})
_HUB_UNKNOWN_SNAPSHOT = "unknown_snapshot"
_HUB_DENIED_CODES = frozenset(
    {"channel_revoked", "forbidden_role", "not_granted", "not_custodial", "factor_too_weak"}
)  # step 5; everything else the verifier refuses is the certificate chain (step 2)
_HUB_FACTORS = ("password+totp", "se-unlocked", "passkey")
_FACTOR_RANK = {f: i for i, f in enumerate(_HUB_FACTORS)}  # D66, weakest first
_HUB_FIELDS = ("channel", "channel_factor", "hub_id", "snapshot_version", "channel_cert", "uk_cert", "channel_proof")


def authorize_hub(core: Core, log: str, e: dict[str, Any], v: Verifier, *, dev: bool = False) -> Refusal | None:
    """Step 5 "Signer" for a hub actor (ticket-format 15.3), each check against the ledger and workspace state AT THE
    LINE'S POSITION and with ``at`` as the time: 1 ``hub.invalid`` (joined, shape, ``item_hash`` by mode), 2
    ``hub.cert_invalid``, 3 ``hub.unknown_snapshot``, 4 ``hub.foreign_user``, 5 ``hub.denied``; then the shared person
    rules on the effective role (a viewer, ``roster_v``) and the two signatures. ``dev`` is the reader's own dev
    marking (never a field of the event)."""
    ws, a, typ = core.ws, e["actor"], e["type"]
    h = ws.hub
    hub_id = e.get("hub_id")
    if not h or h.get("status") != "joined" or h.get("hub_id") != hub_id:
        return Refusal(Code.HUB_INVALID, "not_joined")
    led = hubledger.ledger_of(ws, hub_id)
    if led is None:
        return Refusal(Code.HUB_INVALID, "not_joined")
    joined_at = h.get("joined_at")
    if joined_at is not None and ts(e["at"]) < ts(joined_at):
        # C8 P2-b: a line the hub signed before this binding began (for example one held back across a leave and a
        # rejoin of the same hub id) is not a line of this binding. ``at`` is inside both hub signatures.
        return Refusal(Code.HUB_INVALID, "before_binding")
    mode = h.get("mode")
    if (
        any(k not in e for k in _HUB_FIELDS)
        or a.get("via") != e["channel"]
        or e["channel_factor"] not in _HUB_FACTORS
        or ("item_hash" in e) != (mode == "selective")
    ):
        return Refusal(Code.HUB_INVALID, "malformed")
    user = a["id"][2:]
    member_role = None
    if mode == "selective":  # 15.7: the grant is the alias in the owner-signed hub.joined
        person = next((p for p, u in h.get("alias_book", {}).get(hub_id, {}).items() if u == user), None)
        member_role = identity.role_of(ws, "p_" + person) if person is not None else None
    check = getattr(v, "verify_hub_actor", None)
    code = (
        "unavailable"
        if check is None
        else check(
            e, workspace_id=ws.workspace_id, log=log, root=led["root"], card=led["card"],
            revoked=frozenset(led["revoked_op_certs"]),
            snapshot=led["snapshot"], revoked_channels=frozenset(led["revoked_channels"]), at_ms=ts(e["at"]) * 1000,
            dev=dev, mode=mode, member_role=member_role,
        )
    )  # fmt: skip
    if code is not None and code != _HUB_UNKNOWN_SNAPSHOT and code not in _HUB_DENIED_CODES:
        return Refusal(Code.HUB_INVALID if code in _HUB_INVALID_CODES else Code.HUB_CERT_INVALID, code)
    if code == _HUB_UNKNOWN_SNAPSHOT:
        return Refusal(Code.HUB_UNKNOWN_SNAPSHOT, code)
    # step 4 runs after the certificate chain and the snapshot, so an unauthenticated event never learns the id map
    who = identity.hub_actor(ws, hub_id, user)
    if isinstance(who, Refusal):
        return who
    if code is not None:
        return Refusal(Code.HUB_DENIED, code)
    if typ not in HUB_TYPES:  # defence in depth beside the verifier: only the types whose rules read the signer
        return Refusal(Code.HUB_DENIED, "not_custodial")
    # D66: the factor floor of the ONE model function, at this position (the host passes the same function's result to
    # the contract verifier at admission, never a literal)
    t = None if log == WORKSPACE else core.tickets.get(log)
    floor = policies.factor_floor(ws, t, typ, e.get("gate"))
    if _FACTOR_RANK[e["channel_factor"]] < _FACTOR_RANK[floor]:
        return Refusal(Code.HUB_DENIED, "factor_too_weak")
    sg = identity.signer(ws, e)
    if isinstance(sg, Refusal):
        return sg
    if (r := method_refusal(ws, e)) is not None:  # C3b-6: sign_methods without `hub` refuses every hub event
        return r
    if sg.role is None:  # aliased to a person who is no member now, or no role in the snapshot
        return Refusal(Code.HUB_DENIED, "forbidden_role")
    if sg.role == "viewer" and typ not in VIEWER_MAY:
        return Refusal(Code.ROLE_DENIED, "a viewer cannot write")
    if e["roster_v"] != ws.roster_v:
        return Refusal(Code.MEMBERS_STALE, f"roster_v {e['roster_v']} is not current ({ws.roster_v})")
    sigs = getattr(v, "verify_custodial_sigs", None)
    if "sig" not in e or sigs is None or not sigs(e, workspace_id=ws.workspace_id, log=log):
        return Refusal(Code.SIG_INVALID, "the hub signatures do not verify")
    return None


def verb_covers(typ: str, verbs: Any, verb_events: Mapping[str, Collection[str]] | None = None) -> bool:
    """``"agent"`` covers every agent operation. A list names **operations**, matched exactly (§10.1 A3, no prefix or
    group matching); an event is covered when some granted operation emits its type (``verb_events``: operation name ->
    the event types it emits, built from the registry and passed in, so the model stays pure). Without the table (a
    model test) the names are read as event types."""
    if verbs == "agent":
        return True
    verbs = [v for v in verbs if v not in HUMAN_ONLY]  # a person's own operation is never in a grant (F1 10.1)
    if verb_events is None:
        return typ in verbs
    return any(typ in verb_events.get(v, ()) for v in verbs)


def authorize_agent(
    core: Core, t: TCore | None, e: dict[str, Any], verb_events: Mapping[str, Collection[str]] | None = None
) -> Refusal | None:
    ws, a, at = core.ws, e["actor"], ts(e["at"])
    g = ws.grants.get(a["grant"])
    if g is None:
        return Refusal(Code.GRANT_INVALID, f"unknown grant {a['grant']}")
    if g.person != a["for"]:
        return Refusal(Code.GRANT_INVALID, "an agent's `for` must be the person who issued the grant")
    if g.revoked or not (ts(g.issued_at) <= at < ts(g.expires_at)):
        return Refusal(Code.GRANT_INVALID, "the grant was revoked or not valid at this position")
    m = ws.members.get(a["for"])
    if m is None or m.role == "viewer":
        return Refusal(Code.GRANT_INVALID, "the grant's person is not a member who may run agents")
    if not verb_covers(e["type"], g.verbs, verb_events):
        return Refusal(Code.GRANT_VERB, f"the grant does not cover {e['type']}")
    if g.scope == "all" and m.role == "member":
        return Refusal(Code.GRANT_SCOPE, "a member's grant is `workable` only (D60), whatever it says")
    # An agent's permissions are the intersection of its grant and its person: it never reaches a ticket that its
    # `for` person can't see, whatever the grant scope (§9, §10.1 A3).
    if t is not None and not visibility.can_see(ws, t, a["for"]):
        return Refusal(Code.GRANT_SCOPE, "the agent's person may not see this ticket")
    return None


def authorize_unattended(ws: WsCore, t: TCore | None, e: dict[str, Any]) -> Refusal | None:
    a, typ, at = e["actor"], e["type"], ts(e["at"])
    if t is None or visibility.is_restricted(t):
        return Refusal(Code.UNATTENDED_DENIED, "unattended writes only on tickets with visibility workspace")
    size = 0
    if typ == "artifact.added":
        if "ac" in e or "task" in e or e["kind"] == "feedback":
            return Refusal(Code.UNATTENDED_DENIED, "an unattended artifact has no ac, no task and is not feedback")
        size = e.get("bytes", 0)
    recent = [(s, sess, b) for (s, sess, b) in ws.unattended if at - WINDOW < s <= at]
    mine = [(s, sess, b) for (s, sess, b) in recent if sess == a["session"]]
    if (
        len(mine) + 1 > UNATTENDED_PER_SESSION
        or len(recent) + 1 > UNATTENDED_PER_WORKSPACE
        or sum(b for *_, b in mine) + size > BYTES_PER_SESSION
        or sum(b for *_, b in recent) + size > BYTES_PER_WORKSPACE
    ):
        return Refusal(Code.QUOTA_UNATTENDED, "unattended quota exceeded")
    return None


def record_unattended(ws: WsCore, e: dict[str, Any]) -> None:
    a = e["actor"]
    if a["kind"] == "agent" and a.get("unattended") is True:
        at = ts(e["at"])
        ws.unattended = [x for x in ws.unattended if at - WINDOW < x[0]]
        ws.unattended.append((at, a["session"], e.get("bytes", 0) if e["type"] == "artifact.added" else 0))


def freeze_check(core: Core, log: str, e: dict[str, Any]) -> Refusal | None:
    """New person decisions are refused while an invalid event is unacknowledged (§5.11)."""
    if e["type"] not in DECISIONS:
        return None
    if core.logs[WORKSPACE].frozen() or (log in core.logs and core.logs[log].frozen()):
        return Refusal(Code.FREEZE_ACTIVE, "an invalid event is not acknowledged yet (invalid.acknowledged)")
    return None


def visible_to_actor(core: Core, t: TCore | None, e: dict[str, Any]) -> Refusal | None:
    a = e["actor"]
    if t is None or a["kind"] != "person" or e["type"] in visibility.MANAGEMENT:
        return None
    sg = identity.signer(core.ws, e)
    if isinstance(sg, Refusal) or not visibility.can_see(core.ws, t, sg.person):
        return Refusal(Code.TICKET_NOT_VISIBLE, "restricted ticket")
    return None

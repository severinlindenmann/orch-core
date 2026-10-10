"""Authorization replay (ticket-format §5.2, §5.3, §5.11): who may append what, checked identically at append
(``admit``) and on every read (``replay``).

Checks in order: the actor kind may append the type; person events: the signing device's certificate is valid at the
event's position and has the scope, ``roster_v`` is current, the signature verifies; agent events: the grant was
valid, in scope and covered the verb; unattended events: the three types, workspace-visible tickets, quotas.
Role rules that belong to one type live with that type's handler.
"""

from __future__ import annotations

from typing import Any

from . import visibility
from .codes import Code, Refusal
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
    "grant.issued": "P",
    "grant.revoked": "P",
    "addon.granted": "P",
    "addon.disabled": "P",
    "addon.purged": "P",
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
        "restore",
        "invalid.acknowledged",
    }
)
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


def person_has_valid_device(ws: WsCore, person: str, at: int) -> bool:
    return any(d.person == person and device_valid(d, d.cert, at, False) is None for d in ws.devices.values())


def authorize_person(core: Core, log: str, e: dict[str, Any], v: Verifier) -> Refusal | None:
    ws, a, typ, at = core.ws, e["actor"], e["type"], ts(e["at"])
    m = ws.members.get(a["id"])
    if m is None:
        return Refusal(Code.MEMBER_UNKNOWN, f"{a['id']} is not a member")
    dev = ws.devices.get(a["device"])
    recovery = (
        typ == "device.added"
        and e["device"] == a["device"]
        and dev is None
        and not person_has_valid_device(ws, a["id"], at)
    )
    if recovery:
        cert = e["cert"]
    elif dev is None or dev.person != a["id"]:
        return Refusal(Code.DEVICE_UNKNOWN, f"{a['device']} is not a device of {a['id']}")
    else:
        cert = dev.cert
    if (r := device_valid(dev, cert, at, typ in OPERATE)) is not None:
        return r
    if m.role == "viewer" and typ not in VIEWER_MAY:
        return Refusal(Code.ROLE_DENIED, "a viewer cannot write")
    if e["roster_v"] != ws.roster_v:
        return Refusal(Code.MEMBERS_STALE, f"roster_v {e['roster_v']} is not current ({ws.roster_v})")
    if not v.verify_person(e, SigContext(ws.workspace_id, log, cert)):
        return Refusal(Code.SIG_INVALID, "person signature does not verify")
    return None


def verb_covers(typ: str, verbs: Any) -> bool:
    """``"agent"`` covers every agent operation; a list names operations and is matched exactly (§10.1 A3): the
    event type is the operation name, with no prefix or group matching."""
    return verbs == "agent" or typ in verbs


def authorize_agent(core: Core, t: TCore | None, e: dict[str, Any]) -> Refusal | None:
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
    if not verb_covers(e["type"], g.verbs):
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
    if not visibility.can_see(core.ws, t, a["id"]):
        return Refusal(Code.TICKET_NOT_VISIBLE, "restricted ticket")
    return None

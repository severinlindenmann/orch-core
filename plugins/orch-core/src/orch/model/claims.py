"""Claims (T7, A4; ticket-format §5.2, §5.9): one claim per ticket, backed by a grant, with task leases below it.

A claim lapses by inactivity (``claim_ttl_min``), when its grant ends, when its person leaves, or when the ticket is
done or closed. Replay has no clock: lapse is judged against the ``at`` of the event being applied (or ``now`` in a
view), and the host records it lazily with ``claim.released``.
"""

from __future__ import annotations

from typing import Any

from .codes import Code, Refusal
from .types import ClaimCore, TCore, WsCore, ts


def in_family(session: str, claim_session: str) -> bool:
    """``s_X`` and its subagents ``s_X.1``, ``s_X.1.2`` act under the claim of ``s_X`` (A4)."""
    return session == claim_session or session.startswith(claim_session + ".")


def grant_ended(ws: WsCore, grant_id: str, at: int) -> bool:
    g = ws.grants.get(grant_id)
    return g is None or g.revoked or ts(g.expires_at) <= at


def lapse_reason(ws: WsCore, t: TCore, at: int) -> str | None:
    """Why the claim is not live at ``at`` (a release reason of §11.4), or ``None`` when it is live."""
    c = t.claim
    if c is None:
        return "released"
    if c.ended is not None:
        return c.ended
    if c.for_person not in ws.members:
        return "member_removed"
    if grant_ended(ws, c.grant, at):
        return "grant_ended"
    if at - c.last_activity > ws.settings["claim_ttl_min"] * 60:
        return "expired"
    return None


def live(ws: WsCore, t: TCore, at: int) -> bool:
    return t.claim is not None and lapse_reason(ws, t, at) is None


def require_holder(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    """An agent working on a ticket acts under a live claim of its session family; persons are exempt."""
    actor = e["actor"]
    if actor["kind"] != "agent":
        return None
    at = ts(e["at"])
    if t.claim is None or not live(ws, t, at):
        return Refusal(Code.CLAIM_NOT_LIVE, "no live claim on this ticket")
    if not in_family(actor["session"], t.claim.session):
        return Refusal(Code.CLAIM_NOT_HOLDER, f"the claim is held by {t.claim.session}")
    return None


def touch(ws: WsCore, t: TCore, e: dict[str, Any]) -> None:
    """Any event of the claim's session family keeps the claim alive."""
    a = e["actor"]
    if a["kind"] == "agent" and t.claim is not None and in_family(a["session"], t.claim.session):
        t.claim.last_activity = max(t.claim.last_activity, ts(e["at"]))


def release_leases(t: TCore, session: str | None = None) -> None:
    for tid, lease in list(t.leases.items()):
        if session is None or in_family(lease.session, session):
            del t.leases[tid]
            if t.tasks.get(tid) and t.tasks[tid].state == "started":
                t.tasks[tid].state = "open"


def taken(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    a, at = e["actor"], ts(e["at"])
    if t.status in ("done", "closed"):
        return Refusal(Code.TICKET_FROZEN, f"claim.taken is refused on {t.status} tickets")
    takeover = e.get("takeover")
    if t.status in ("open", "backlog"):
        if takeover is not None:
            return Refusal(Code.CLAIM_NOT_LIVE, "nothing to take over")
    elif t.status == "in_progress":
        if takeover is None:
            return Refusal(Code.CLAIM_EXISTS, "the ticket is claimed; a takeover needs a reason")
        if t.claim is None or takeover["from_session"] != t.claim.session:
            return Refusal(Code.CLAIM_NOT_LIVE, "from_session is not the claim's session")
    else:
        return Refusal(Code.STATUS_TRANSITION, f"claim.taken from {t.status}")
    if takeover is not None:
        t.takeovers.append({"event": e["id"], "from_session": takeover["from_session"], "reason": takeover["reason"]})
    release_leases(t)
    t.claim = ClaimCore(a["session"], a["for"], a["grant"], at, at)
    t.status = "in_progress"
    return None


def released(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    a, at = e["actor"], ts(e["at"])
    c = t.claim
    if c is None or c.session != e["session"]:
        return Refusal(Code.CLAIM_NOT_LIVE, "no such claim")
    if a["kind"] == "agent":
        if not in_family(a["session"], c.session):
            return Refusal(Code.CLAIM_NOT_HOLDER, "an agent releases only its own claim")
    elif a["kind"] == "person":
        if lapse_reason(ws, t, at) is not None:
            return Refusal(Code.CLAIM_NOT_LIVE, "the claim is not live")
        if not may_manage(ws, t, a["id"]):
            return Refusal(Code.ROLE_DENIED, "only the ticket owner, an owner or a maintainer releases another's claim")
    else:  # host: the reason must be true at this position (§5.12)
        if lapse_reason(ws, t, at) != e["reason"]:
            return Refusal(Code.CLAIM_NOT_LIVE, f"the claim has not lapsed for {e['reason']}")
    t.claim = None
    release_leases(t)
    if t.status == "in_progress":
        t.status = "open"
    return None


def may_manage(ws: WsCore, t: TCore, person: str) -> bool:
    """The ticket owner, workspace owners and maintainers (§5.4.2 "who may sign what")."""
    m = ws.members.get(person)
    return m is not None and (m.role in ("owner", "maintainer") or (t.owner == person and m.role != "viewer"))

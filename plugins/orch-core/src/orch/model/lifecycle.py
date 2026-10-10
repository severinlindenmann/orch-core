"""Ticket lifecycle and status (ticket-format §5.9): the from-state table, people, ticket policy overrides, restore.

Events not in the §5.9 table don't change status; a table event whose from-state isn't listed is refused (for host
events: the status stays unchanged and the event, which the host should not have appended, is refused too).
"""

from __future__ import annotations

import copy
import re
from typing import Any

from orch.canon import HashError, section_hash

from . import claims, gates, generations, policies, source, tasks
from .codes import Code, Refusal
from .types import GATES, Core, LogCore, TCore, WsCore, new_fields, position

_KEY = re.compile(r"[A-Z][A-Z0-9]{0,15}-([1-9][0-9]{3,}|[1-9][0-9]{0,2}|0[0-9]{3})")


def created(core: Core, uid: str, e: dict[str, Any]) -> Refusal | None:
    ws = core.ws
    key = e["key"]
    if not key.startswith(ws.prefix + "-") or not _KEY.fullmatch(key):
        return Refusal(Code.TICKET_BAD_REFERENCE, f"{key} is not a key of workspace prefix {ws.prefix}")
    if key in core.keys:
        return Refusal(Code.TICKET_EXISTS, f"{key} is taken (keys are never reused)")
    m = ws.members.get(e["owner"])
    if m is None or m.role == "viewer":
        return Refusal(Code.MEMBER_UNKNOWN, "the owner must be a member who can own tickets")
    core.keys[key] = uid
    core.created_at[uid] = position(e, uid)
    t = TCore(uid=uid, key=key, owner=e["owner"], status="open", fields=new_fields(e["ticket_type"], e["title"]))
    core.tickets[uid] = t
    return None


def status_changed(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    if t.status not in ("open", "backlog") or e["from"] != t.status:
        return Refusal(Code.STATUS_TRANSITION, f"status.changed from {e['from']} while the ticket is {t.status}")
    t.status = e["to"]
    return None


def submitted(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    if t.status != "in_progress":
        return Refusal(Code.STATUS_TRANSITION, f"ticket.submitted from {t.status}")
    if (r := claims.require_holder(ws, t, e)) is not None:
        return r
    lack: list[str] = []
    for g in ("requirements", "plan"):
        if policies.applies(ws, t, g) and not generations.reached(ws, t, g):
            lack.append(f"{g} not approved")
    sec = {"feature": "verification", "bug": "verification", "spike": "findings"}.get(t.ticket_type)
    if sec and gates.section_hash_of(t, sec) == gates.EMPTY:
        lack.append(f"section {sec} is empty")
    for ac, ev in tasks.ac_evidence(ws, t).items():
        if not ev:
            lack.append(f"{ac} has no evidence")
    for r in source.linked_repos(t):
        if r not in t.fields["links"]["branches"]:
            lack.append(f"repo {r} has no branch in links.branches")
    lack += [f"repo {r}: no branch observed" for r in source.missing_repos(t)]
    if lack:
        return Refusal(Code.SUBMIT_INCOMPLETE, "; ".join(lack))
    t.status = "testing"
    return None


def known_before(core: Core, t: TCore, key: str, e: dict[str, Any]) -> bool:
    """The ticket ``key`` exists at an earlier merged position than the event ``e`` (so admit and replay agree)."""
    uid = core.keys.get(key)
    return uid is not None and uid != t.uid and core.created_at[uid] < position(e, t.uid)


def closed(core: Core, t: TCore, e: dict[str, Any]) -> Refusal | None:
    if not claims.may_manage(core.ws, t, e["actor"]["id"]):
        return Refusal(Code.ROLE_DENIED, "only the ticket owner, owners and maintainers close a ticket")
    if t.status == "closed":
        return Refusal(Code.STATUS_TRANSITION, "already closed")
    if "duplicate_of" in e and not known_before(core, t, e["duplicate_of"], e):
        return Refusal(Code.TICKET_BAD_REFERENCE, f"duplicate_of {e['duplicate_of']}")
    t.status = "closed"
    if t.claim is not None:
        t.claim.ended = "ticket_closed"
    t.leases.clear()
    return None


def reopened(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    if not claims.may_manage(ws, t, e["actor"]["id"]):
        return Refusal(Code.ROLE_DENIED, "only the ticket owner, owners and maintainers reopen a ticket")
    if t.status not in ("done", "closed"):
        return Refusal(Code.STATUS_TRANSITION, f"ticket.reopened from {t.status}")
    t.status = "open"
    t.workers = set()  # "since the ticket was last reopened" (§5.7)
    generations.mark(t, *GATES)
    return None


def _role_token(role: str) -> str:
    return "ticket_owner" if role == "owner" else role


def people_changed(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    if not claims.may_manage(ws, t, e["actor"]["id"]):
        return Refusal(Code.ROLE_DENIED, "only the ticket owner, owners and maintainers change people")
    role, token = e["role"], _role_token(e["role"])
    named = {g: policies.named_roles(policies.effective(ws, t, g)) for g in GATES}
    if t.status in ("done", "closed") and any(token in n for n in named.values()):
        return Refusal(Code.TICKET_FROZEN, f"{role} is named by a gate policy and the ticket is {t.status}")
    unknown = [p for p in e["add"] if p not in ws.members]
    if unknown:
        return Refusal(Code.MEMBER_UNKNOWN, f"not members: {unknown}")
    if role == "owner":
        if len(e["add"]) != 1:
            return Refusal(Code.PEOPLE_INVALID, "owner: `add` has exactly one id")
        t.owner = e["add"][0]
    else:
        t.people[role] = (t.people[role] | set(e["add"])) - set(e["remove"])
        if role == "assignees":
            t.workers |= set(e["add"])
    generations.mark(t, *[g for g in GATES if token in named[g]])
    return None


def policy_changed(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    if not claims.may_manage(ws, t, e["actor"]["id"]):
        return Refusal(Code.ROLE_DENIED, "only the ticket owner, owners and maintainers set an override")
    if (r := policies.check_override(ws, t, e["gates"])) is not None:
        return r
    t.overrides.update(copy.deepcopy(e["gates"]))
    generations.mark(t, *e["gates"])
    return None


def handoff(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    if (r := claims.require_holder(ws, t, e)) is not None:
        return r
    try:
        h = section_hash(e["text"])
    except HashError as err:
        return Refusal(Code.GATE_INCOMPLETE, str(err))
    t.handoff = e["text"]
    t.sections["current_state"] = {"hash": h, "refs": []}
    return None


def restore(ws: WsCore, t: TCore, lc: LogCore, e: dict[str, Any]) -> Refusal | None:
    if ws.members[e["actor"]["id"]].role != "owner":
        return Refusal(Code.ROLE_DENIED, "restore is owner only")
    if e["head"] != lc.head or e["from_seq"] != lc.seq:
        return Refusal(Code.RESTORE_BAD_HEAD, "from_seq/head must name the last event on disk")
    generations.mark(t, *GATES)
    return None

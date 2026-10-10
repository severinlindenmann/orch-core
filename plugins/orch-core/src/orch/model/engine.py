"""One code path for append and replay (ticket-format §5.5, §5.11).

``apply_event`` takes the next event of a log and either accepts it (state changes) or refuses it with a code.
``admit`` runs it on a scratch copy and throws the scratch away; ``replay``/``advance`` commit what it accepts and
record what it refuses as an invalid event: absent for state, in its place in the chain, reported, and freezing
person decisions until ``invalid.acknowledged`` (§5.11). A chain failure (``seq``, ``prev``, ``ws_seq``, ``host_sig``)
breaks the log: nothing after it counts.

Every event is applied to a scratch copy, so a refusal can never leave half an effect behind.
"""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
from typing import Any

from orch.canon import HashError, event_head
from orch.schema import LOG_TYPES

from . import authz, claims, edits, gates, generations, lifecycle, questions, source, tasks, visibility, workspace
from .codes import Code, Refusal
from .types import WORKSPACE, Core, InvalidEvent, LogCore, TCore, WsCore, position, ts
from .verifier import Verifier

CHAIN_CODES = frozenset({Code.CHAIN_BROKEN, Code.CHAIN_BAD_WS_SEQ, Code.EVENT_UNKNOWN_TYPE})
# Workspace events that change every ticket: those get a copy of all tickets.
CROSS_TICKETS = frozenset(
    {
        "member.removed",
        "role.changed",
        "device.revoked",
        "policy.changed",
        "addon.granted",
        "addon.disabled",
        "addon.purged",
        "restore",
    }
)


@dataclass(frozen=True)
class Ctx:
    verifier: Verifier
    expected_genesis: str | None = None
    admit: bool = False  # pre-append: no host_sig yet, ws_seq must be the workspace head


def _head(e: dict[str, Any]) -> str | None:
    try:
        return event_head(e)
    except (HashError, KeyError, TypeError):
        return None


def _log_of(core: Core, log: str) -> LogCore:
    return core.logs.get(log) or LogCore()


def _chain_check(core: Core, log: str, lc: LogCore, e: dict[str, Any], ctx: Ctx) -> Refusal | None:
    kind = "workspace" if log == WORKSPACE else "ticket"
    if lc.broken is not None:
        return Refusal(Code.CHAIN_BROKEN, f"the log is broken ({lc.broken})")
    if e["type"] not in LOG_TYPES[kind]:
        return Refusal(Code.EVENT_UNKNOWN_TYPE, f"{e['type']} is not an event of the {kind} log")
    if e["seq"] != lc.seq + 1 or e["prev"] != lc.head:
        return Refusal(Code.CHAIN_BROKEN, "seq/prev do not continue the log")
    if not ctx.admit:
        genesis = e["type"] == "workspace.created" and not core.ws.created  # a second one is checked like any event
        if "host_sig" not in e:
            return Refusal(Code.CHAIN_BROKEN, "event has no host_sig")
        if not genesis and not core.ws.created:
            return Refusal(Code.CHAIN_BROKEN, "no genesis yet, so no workspace key to check host_sig with")
        wsk = None if genesis else base64.urlsafe_b64decode(core.ws.wsk_pub + "=" * (-len(core.ws.wsk_pub) % 4))
        if not ctx.verifier.verify_host(e, log=log, wsk_pub=wsk):
            return Refusal(Code.CHAIN_BROKEN, "host_sig does not verify")
    if kind == "ticket":
        wsh = core.logs[WORKSPACE].seq if WORKSPACE in core.logs else 0
        if e["ws_seq"] < lc.last_ws_seq or e["ws_seq"] > wsh or (ctx.admit and e["ws_seq"] != wsh):
            return Refusal(Code.CHAIN_BAD_WS_SEQ, f"ws_seq {e['ws_seq']} (workspace head {wsh})")
        if ctx.admit and core.last_pos is not None and position(e, log) <= core.last_pos:
            # replay walks events by (ws_seq, at, uid, seq); appending out of that order would make replay see
            # cross-ticket state in another order than admit did, so the host must pick a later `at`
            return Refusal(Code.CHAIN_BAD_WS_SEQ, "the event would sort before an earlier append (ws_seq, at, uid)")
    return None


def _envelope_check(lc: LogCore, e: dict[str, Any]) -> Refusal | None:
    if e["id"] in lc.ids:
        return Refusal(Code.EVENT_DUPLICATE_ID, e["id"])
    based = e["based_on"]
    if (e["seq"] == 1) != (based is None) or (based is not None and lc.heads.get(based, e["seq"]) >= e["seq"]):
        return Refusal(Code.EVENT_BAD_BASE, "based_on must be the head of an earlier event of this log")
    return None


def _scratch(core: Core, log: str, e: dict[str, Any]) -> Core:
    typ = e["type"]
    a = e["actor"]
    unattended = a["kind"] == "agent" and a.get("unattended") is True
    is_ws = log == WORKSPACE
    sc = Core(
        ws=copy.deepcopy(core.ws) if (is_ws or unattended) else core.ws,
        tickets=dict(core.tickets),
        keys=dict(core.keys) if typ == "ticket.created" else core.keys,
        created_at=dict(core.created_at) if typ == "ticket.created" else core.created_at,
        logs=core.logs,
    )
    if is_ws and typ in CROSS_TICKETS:
        sc.tickets = copy.deepcopy(core.tickets)
    elif not is_ws and log in core.tickets:
        sc.tickets[log] = copy.deepcopy(core.tickets[log])
    return sc


def _ticket_handler(core: Core, t: TCore, lc: LogCore, e: dict[str, Any]) -> Refusal | None:
    ws, typ = core.ws, e["type"]
    h = {
        "ticket.updated": lambda: edits.updated(core, t, e),
        "status.changed": lambda: lifecycle.status_changed(ws, t, e),
        "ticket.submitted": lambda: lifecycle.submitted(ws, t, e),
        "ticket.closed": lambda: lifecycle.closed(core, t, e),
        "ticket.reopened": lambda: lifecycle.reopened(ws, t, e),
        "visibility.changed": lambda: visibility.changed(ws, t, e),
        "people.changed": lambda: lifecycle.people_changed(ws, t, e),
        "policy.changed": lambda: lifecycle.policy_changed(ws, t, e),
        "claim.taken": lambda: claims.taken(ws, t, e),
        "claim.released": lambda: claims.released(ws, t, e),
        "handoff.written": lambda: lifecycle.handoff(ws, t, e),
        "log.added": lambda: None,
        "question.asked": lambda: questions.asked(ws, t, e),
        "question.answered": lambda: questions.answered(ws, t, e),
        "gate.approved": lambda: gates.decision(ws, t, e),
        "gate.changes_requested": lambda: gates.decision(ws, t, e),
        "verdict.given": lambda: gates.decision(ws, t, e),
        "gate.invalidated": lambda: gates.invalidated(ws, t, e),
        "branch.pushed": lambda: source.branch_pushed(ws, t, e),
        "edit.external": lambda: edits.external(ws, t, e),
        "projection.repaired": lambda: None,
        "restore": lambda: lifecycle.restore(ws, t, lc, e),
        "invalid.acknowledged": lambda: _ack(ws, lc, e),
    }
    for k in ("task.started", "task.done", "task.skipped", "task.blocked", "task.reopened"):
        h[k] = lambda: tasks.task_event(ws, t, e)
    for k in ("artifact.added", "artifact.replaced"):
        h[k] = lambda: tasks.artifact_event(ws, t, e)
    return h[typ]()


def _ack(ws: WsCore, lc: LogCore, e: dict[str, Any]) -> Refusal | None:
    if ws.members[e["actor"]["id"]].role != "owner":
        return Refusal(Code.ROLE_DENIED, "invalid.acknowledged is owner only")
    bad = next((i for i in lc.invalid if i.seq == e["invalid_seq"]), None)
    if bad is None or bad.head != e["invalid_head"]:
        return Refusal(Code.ACK_UNKNOWN, "no such invalid event in this log")
    return None


def _workspace_handler(core: Core, lc: LogCore, e: dict[str, Any], ctx: Ctx) -> Refusal | None:
    ws, typ, v = core.ws, e["type"], ctx.verifier
    h = {
        "member.added": lambda: workspace.member_added(core, e, v),
        "member.removed": lambda: workspace.member_removed(core, e),
        "role.changed": lambda: workspace.role_changed(core, e),
        "device.added": lambda: workspace.device_added(core, e, v),
        "device.removed": lambda: workspace.device_removed(ws, e),
        "device.revoked": lambda: workspace.device_revoked(core, e, v),
        "policy.changed": lambda: workspace.policy_changed(core, e),
        "settings.changed": lambda: workspace.settings_changed(ws, e),
        "grant.issued": lambda: workspace.grant_issued(ws, e),
        "grant.revoked": lambda: workspace.grant_revoked(ws, e),
        "addon.granted": lambda: workspace.addon_event(core, e),
        "addon.disabled": lambda: workspace.addon_event(core, e),
        "addon.purged": lambda: workspace.addon_event(core, e),
        "restore": lambda: workspace.restore(core, lc, e),
        "invalid.acknowledged": lambda: _ack(ws, lc, e),
        "projection.repaired": lambda: None,
    }
    return h[typ]()


def _authorize(core: Core, log: str, t: TCore | None, e: dict[str, Any], ctx: Ctx) -> Refusal | None:
    if (r := authz.check_actor_kind(e)) is not None:
        return r
    a, ws = e["actor"], core.ws
    if e["type"] == "workspace.created":
        return None if not ws.created else Refusal(Code.GENESIS_INVALID, "the workspace already has a genesis")
    if not ws.created:
        return Refusal(Code.GENESIS_INVALID, "the first event of a workspace is workspace.created")
    if a["kind"] == "person":
        if (r := authz.authorize_person(core, log, e, ctx.verifier)) is not None:
            return r
    elif a["kind"] == "agent":
        if a.get("unattended") is True:
            if (r := authz.authorize_unattended(ws, t, e)) is not None:
                return r
        elif (r := authz.authorize_agent(core, t, e)) is not None:
            return r
    if (r := authz.freeze_check(core, log, e)) is not None:
        return r
    return authz.visible_to_actor(core, t, e)


def _apply(core: Core, log: str, lc: LogCore, e: dict[str, Any], ctx: Ctx) -> Refusal | None:
    """Authorize and apply ``e`` to ``core`` (a scratch copy). Mutates ``core`` even when it refuses midway."""
    typ, ws = e["type"], core.ws
    is_ws = log == WORKSPACE
    t = None if is_ws else core.tickets.get(log)
    if not is_ws and typ != "ticket.created" and t is None:
        return Refusal(Code.TICKET_UNKNOWN, log)
    if not is_ws and typ == "ticket.created" and t is not None:
        return Refusal(Code.TICKET_EXISTS, log)
    if (r := _authorize(core, log, t, e, ctx)) is not None:
        return r
    if is_ws:
        if typ == "workspace.created":
            return workspace.genesis(core, e, ctx.verifier, ctx.expected_genesis)
        return _workspace_handler(core, lc, e, ctx)
    if typ == "ticket.created":
        return lifecycle.created(core, log, e)
    assert t is not None
    before = generations.snapshot(ws, t)
    if (r := _ticket_handler(core, t, lc, e)) is not None:
        return r
    settled = generations.settle(ws, t, before)
    if typ == "edit.external" and sorted(e["voided_gates"]) != sorted(settled.voided):
        return Refusal(Code.AUTH_INVALID_EVENT, f"voided_gates should be {sorted(settled.voided)}")
    if typ in ("gate.approved", "verdict.given"):
        gates.after_decision(ws, t, e)
    claims.touch(ws, t, e)
    _note_workers(t, e)
    t.last_at = max(t.last_at, ts(e["at"]))
    authz.record_unattended(ws, e)
    return None


def _note_workers(t: TCore, e: dict[str, Any]) -> None:
    """§5.7 workers: the `for` person of any agent event on the ticket, and the claim holder whose session's
    commits a host `branch.pushed` records. The set only grows until the ticket is reopened."""
    a = e["actor"]
    if a["kind"] == "agent" and "for" in a:
        t.workers.add(a["for"])
    elif a["kind"] == "host" and e["type"] == "branch.pushed" and t.claim is not None:
        t.workers.add(t.claim.for_person)


def _place(lc: LogCore, e: dict[str, Any]) -> None:
    head = _head(e)
    lc.seq = e["seq"]
    lc.head = head
    if head is not None:
        lc.heads[head] = e["seq"]
    lc.ids.add(e["id"])
    lc.last_ws_seq = e.get("ws_seq", lc.last_ws_seq)


def apply_event(
    core: Core, log: str, e: dict[str, Any], ctx: Ctx, *, commit: bool, cow: bool = False
) -> Refusal | None:
    """Accept or refuse the next event of ``log``. With ``commit`` the core takes the event (accepted: its effects;
    refused as auth failure: an invalid-event record; chain failure: the log is marked broken)."""
    lc = _log_of(core, log)
    if cow:  # the caller shares logs with an earlier State: never mutate those
        lc = copy.deepcopy(lc)
    if (r := _chain_check(core, log, lc, e, ctx)) is not None:
        if commit and lc.broken is None:
            lc.broken = r.detail
            core.logs[log] = lc
        return r
    r = _envelope_check(lc, e)
    sc = None
    if r is None:
        sc = _scratch(core, log, e)
        r = _apply(sc, log, lc, e, ctx)
    if not commit:
        return r
    core.logs[log] = lc
    if log != WORKSPACE:
        core.last_pos = position(e, log)
    if r is None:
        assert sc is not None
        core.ws, core.tickets, core.keys, core.created_at = sc.ws, sc.tickets, sc.keys, sc.created_at
        _place(lc, e)
        if e["type"] == "invalid.acknowledged":
            lc.acked.add(e["invalid_seq"])
        elif e["type"] == "restore":
            lc.acked |= {i.seq for i in lc.invalid}
    else:
        lc.invalid.append(
            InvalidEvent(
                log, e["seq"], e["id"], e["type"], _head(e), r.code.value, r.detail, r.code != Code.FREEZE_ACTIVE
            )
        )
        _place(lc, e)
    return r

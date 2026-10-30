"""Frozen views of the replay core: what callers (store, ops, CLI, dashboard) read. Nothing here is mutable."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from orch.canon import gate_hash

from . import (
    claims,
    followups,
    followups_wire,
    gates,
    generations,
    needs,
    policies,
    questions,
    schedules_wire,
    source,
    tasks,
)
from .needs import Need
from .types import GATES, WORKSPACE, Core, TCore, WsCore, ts


def freeze(o: Any) -> Any:
    if isinstance(o, dict):
        return MappingProxyType({k: freeze(v) for k, v in o.items()})
    if isinstance(o, list):
        return tuple(freeze(v) for v in o)
    if isinstance(o, set | frozenset):
        return tuple(sorted(o))
    return o


@dataclass(frozen=True)
class DecisionView:
    id: str
    gate: str
    kind: str
    person: str
    gen: int
    counting: bool
    voided: bool
    hash: str
    policy_hash: str
    source_sha: tuple[Mapping[str, str], ...] | None


@dataclass(frozen=True)
class GateView:
    gate: str
    applies: bool
    gen: int
    policy: Mapping[str, Any]
    policy_hash: str
    blocked: bool
    hash: str | None
    input: Mapping[str, Any] | None  # G (§5.7)
    needed: int
    approved: bool
    counting: tuple[str, ...]  # persons with counting approvals
    eligible: tuple[str, ...]
    decisions: tuple[DecisionView, ...]
    waiting: bool
    revoked_decisions: tuple[str, ...]  # a settled ticket: ids of a revoked device's decisions still counting here
    missing: tuple[
        str, ...
    ] = ()  # what an approval of requirements or plan still lacks: section ids, acceptance, tasks


@dataclass(frozen=True)
class ClaimView:
    session: str
    for_person: str
    grant: str
    live: bool
    lapsed: str | None


@dataclass(frozen=True)
class TaskView:
    id: str
    text: str
    state: str
    holder: str | None
    leased_by: str | None
    proves: tuple[str, ...]
    done_event: str | None
    verify_by: Mapping[str, Any] | None = None  # who made the last change to its verify (model.edits)


@dataclass(frozen=True)
class AcView:
    id: str
    text: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class QuestionView:
    id: str
    qid: str
    hash: str
    to: str
    blocking: bool
    answered: bool
    answer: Mapping[str, Any] | None
    addressees: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactView:
    name: str
    kind: str
    digest: str | None
    ac: str | None
    task: str | None
    evidence: bool
    by: str
    bytes: int | None = None


@dataclass(frozen=True)
class TicketView:
    uid: str
    key: str
    title: str
    type: str
    status: str
    owner: str
    people: Mapping[str, tuple[str, ...]]
    visibility: Any
    fields: Mapping[str, Any]
    sections: Mapping[str, Any]
    gates: Mapping[str, GateView]
    claim: ClaimView | None
    takeovers: tuple[Mapping[str, Any], ...]
    tasks: tuple[TaskView, ...]
    acceptance: tuple[AcView, ...]
    questions: tuple[QuestionView, ...]
    artifacts: tuple[ArtifactView, ...]
    source_list: tuple[Mapping[str, str], ...]
    needs: tuple[Need, ...]
    waiting: bool
    frozen: bool  # an unacknowledged invalid event freezes person decisions on this ticket
    handoff: str | None
    followups: tuple[Mapping[str, Any], ...] = ()
    schedules: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class MemberView:
    person: str
    name: str
    role: str


@dataclass(frozen=True)
class DeviceView:
    id: str
    person: str
    removed: bool
    revoked: str | None
    scopes: tuple[str, ...]
    expires_ms: int | None = None  # the certificate's expiry (None: never)
    exposed: bool = False  # #1097: signed evidence that this key was in convenience custody (nothing clears it)


@dataclass(frozen=True)
class GrantView:
    id: str
    person: str
    scope: str
    verbs: Any
    expires_at: str
    revoked: bool
    active: bool  # not revoked and not expired at ``now``
    #: the ``grant.issued`` label (H11c: only ``"runner"`` can be handed to the runner) and issue time
    label: str | None = None
    issued_at: str | None = None
    #: a follow-up grant (`fgr_`) names the grant it was derived from (Schedules, #720)
    parent: str | None = None


@dataclass(frozen=True)
class InvalidView:
    log: str
    seq: int
    id: str
    type: str
    code: str
    detail: str
    acknowledged: bool


@dataclass(frozen=True)
class WorkspaceView:
    workspace_id: str
    prefix: str
    genesis: str | None
    roster_v: int
    members: Mapping[str, MemberView]
    former: Mapping[str, MemberView]
    devices: Mapping[str, DeviceView]
    grants: Mapping[str, GrantView]
    policies: Mapping[str, Mapping[str, Any]]
    settings: Mapping[str, Any]
    repos: Mapping[str, Mapping[str, str]]
    addons: Mapping[str, Mapping[str, Any]]
    invalid: tuple[InvalidView, ...]
    frozen: bool
    needs: tuple[Need, ...]
    hub: Mapping[str, Any] = MappingProxyType({})  # the workspace's hub (hub contract 9.1); empty if it never joined
    remote: Mapping[str, Any] = MappingProxyType({})  # remote access policy and devices (R2); empty until the first


def _gate_view(ws: WsCore, t: TCore, g: str) -> GateView:
    pol = policies.effective(ws, t, g)
    gc = t.gates[g]
    count = {d.id for d in generations.counting(t, g, ws)}
    try:
        raw = gates.gate_input(ws, t, g)
        h, g_in = gate_hash(raw), freeze(raw)
    except Exception:  # noqa: BLE001 - an unbuildable G is shown as None (e.g. duplicate repo identity)
        h, g_in = None, None
    return GateView(
        g,
        policies.applies_to(pol, t.ticket_type),
        gc.gen,
        freeze(pol),
        policies.effective_hash(ws, t, g),
        not pol["approvers"],
        h,
        g_in,
        pol["count"],
        gates.approved(ws, t, g),
        tuple(d.person for d in generations.counting(t, g, ws)),
        tuple(policies.eligible_persons(ws, t, g)),
        tuple(
            DecisionView(
                d.id,
                d.gate,
                d.kind,
                d.person,
                d.gen,
                d.id in count,
                d.voided,
                d.hash,
                d.policy_hash,
                freeze(d.source_sha) if d.source_sha is not None else None,
            )
            for d in gc.decisions
        ),
        needs.gate_waiting(ws, t, g),
        tuple(sorted(gc.revoked_flag & {d.id for d in generations.counting(t, g, ws)})),
        tuple(gates.missing_for_gate(ws, t, g)),
    )


def ticket_view(core: Core, t: TCore, now: int) -> TicketView:
    ws = core.ws
    lc = core.logs.get(t.uid)
    frozen = bool(lc and lc.frozen())
    claim = None
    if t.claim is not None:
        why = claims.lapse_reason(ws, t, now)
        claim = ClaimView(t.claim.session, t.claim.for_person, t.claim.grant, why is None, why)
    ev = tasks.ac_evidence(ws, t)
    ns = needs.needs_of(ws, t, frozen)
    task_views = []
    for x in t.fields["tasks"]:
        tk = t.tasks.get(x["id"])
        lease = t.leases.get(x["id"])
        task_views.append(
            TaskView(
                x["id"],
                x["text"],
                tk.state if tk else "open",
                tk.holder if tk else None,
                lease.session if lease and tasks.lease_active(ws, lease, now) else None,
                tuple(x["proves"]),
                tk.done_event if tk else None,
                freeze(t.verify_by[x["id"]]) if x["id"] in t.verify_by else None,
            )
        )
    return TicketView(
        t.uid,
        t.key,
        t.fields["title"],
        t.ticket_type,
        t.status,
        t.owner,
        MappingProxyType({"owner": (t.owner,), **{r: tuple(sorted(p)) for r, p in t.people.items()}}),
        freeze(t.fields["visibility"]),
        freeze(t.fields),
        freeze(t.sections),
        MappingProxyType({g: _gate_view(ws, t, g) for g in GATES}),
        claim,
        tuple(freeze(x) for x in t.takeovers),
        tuple(task_views),
        tuple(AcView(a["id"], a["text"], tuple(ev[a["id"]])) for a in t.fields["acceptance"]),
        tuple(
            QuestionView(
                q.question["id"],
                q.qid,
                q.hash,
                q.question["to"],
                q.question["blocking"],
                q.answer is not None,
                freeze(q.answer) if q.answer else None,
                tuple(questions.addressees(ws, t, q.question)),
            )
            for _, q in sorted(t.questions.items(), key=lambda kv: int(kv[0][1:]))
        ),
        tuple(
            ArtifactView(a.name, a.kind, a.digest, a.ac, a.task, a.evidence_ok, a.by, a.bytes)
            for _, a in sorted(t.artifacts.items())
        ),
        tuple(freeze(x) for x in source.source_list(t)),
        tuple(ns),
        any((n.kind == "question" and n.detail == "blocking") or n.kind == "approve" for n in ns),
        frozen,
        t.handoff,
        tuple(
            freeze(
                {
                    "id": r.id,
                    "state": followups_wire.view_state(core, t, r, now)[0],
                    "trigger": r.trigger,
                    "due": r.due,
                    "deadline": r.deadline,
                    "brief": r.brief,
                    "set_at": r.set_at,
                    "fired_at": r.fired_at,
                    "late_by": followups.late_by(ts(r.fired_at), ts(r.due or r.deadline))
                    if r.fired_at and (r.due or r.deadline)
                    else None,
                    "run": r.run,
                    "outcome": r.outcome,
                    "reason": followups_wire.view_state(core, t, r, now)[1] or r.reason,
                    "grant": r.grant,
                    "person": r.person,
                    "setter_session": r.setter_session,
                    **({"label": r.label} if r.label is not None else {}),
                    **{k: v for k, v in (("event", r.event), ("fallback", r.fallback)) if v},
                    # the hook hash is not shown to readers (B3): the match without it, and a short prefix
                    **({"on": {k: v for k, v in r.on.items() if k != "hook"}} if r.on else {}),
                    **({"hook_prefix": r.hook[:8]} if r.hook else {}),
                }
            )
            for r in t.followups.values()
        ),
        tuple(
            freeze(
                {
                    "id": r.id,
                    "person": r.person,
                    "rule": r.rule,
                    "starts": r.starts,
                    "ends": r.ends,
                    "brief": r.brief,
                    "run_hours": r.run_hours,
                    "label": r.label,
                    "state": "cancelled" if r.cancelled else "expired" if now > ts(r.ends) else "live",
                    "set_at": r.set_at,
                    **({"target": r.target, "title": r.title} if r.target != "existing" else {}),
                    "occurrences": r.occurrences,
                    # every settled occurrence, oldest first, as a plain list: the last one is the latest outcome
                    "outcomes": [
                        {"occurrence": o, "outcome": row["outcome"], "reason": row.get("reason")}
                        for o, row in sorted(r.occurrences.items())
                        if row.get("outcome")
                    ],
                    # the longest the firing lets this setter's run last (the member's grant_hours, at most 8)
                    "grant_hours_cap": schedules_wire.grant_hours_cap(ws, r.person),
                }
            )
            for r in t.schedules.values()
        ),
    )


def workspace_view(core: Core, now: int) -> WorkspaceView:
    ws = core.ws
    lc = core.logs.get(WORKSPACE)
    invalid = tuple(
        InvalidView(i.log, i.seq, i.id, i.type, i.code, i.detail, i.seq in lc.acked) for i in (lc.invalid if lc else [])
    )
    frozen = bool(lc and lc.frozen())
    return WorkspaceView(
        ws.workspace_id,
        ws.prefix,
        ws.genesis,
        ws.roster_v,
        MappingProxyType({p: MemberView(p, m.name, m.role) for p, m in ws.members.items()}),
        MappingProxyType({p: MemberView(p, m.name, m.role) for p, m in ws.former.items()}),
        MappingProxyType(
            {
                i: DeviceView(
                    i,
                    d.person,
                    d.removed,
                    d.revoked,
                    tuple(d.cert["o"]["scopes_max"]),
                    d.cert["o"].get("expires_ms"),
                    d.exposed,
                )  # fmt: skip
                for i, d in ws.devices.items()
            }
        ),
        MappingProxyType(
            {
                i: GrantView(
                    i,
                    g.person,
                    g.scope,
                    freeze(g.verbs),
                    g.expires_at,
                    g.revoked or g.ended,
                    not (g.revoked or g.ended) and ts(g.expires_at) > now,
                    g.label,
                    g.issued_at,
                    g.parent,
                )
                for i, g in ws.grants.items()
                if g.root_kind != "schedule"
            }
        ),
        freeze(ws.policies),
        freeze(ws.settings),
        freeze(ws.repos),
        MappingProxyType(
            {
                n: freeze(
                    {
                        "version": a.version,
                        "enabled": a.enabled and not a.purged,
                        "binds": a.binds,
                        "fields": a.fields,
                        "sections": a.sections,
                        "artifact_kinds": a.artifact_kinds,
                        "package_sha256": a.package_sha256,
                        "capabilities": a.capabilities,
                        "purged": a.purged,
                        "runtime": a.runtime,
                        "events": a.events,
                    }
                )
                for n, a in ws.addons.items()
            }
        ),
        invalid,
        frozen,
        tuple(needs.workspace_needs(ws, frozen)),
        freeze(ws.hub),
        freeze(ws.remote),
    )

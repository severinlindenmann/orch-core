"""Pure follow-up rules (FU1, FU3–FU8, FU14–FU16).

All times are supplied epoch seconds. Parent expiry deliberately does not cancel descendants (FU4).
FU2 session binding and FU9–FU13 host/relay custody belong to the later wiring, not this model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from orch.canon.text import is_clean_text

FU_MAX_DAYS = 14  # FU5 scheduling horizon.
FU_CHAIN_MAX = 5  # FU7 follow-ups without progress.
FU_CHAIN_CEILING_DAYS = 30  # owner decision 2026-10-21 (Q1/Q5)
FU_CHAIN_CEILING_COUNT = 10  # owner decision 2026-10-21 (Q1/Q5)
FU_PENDING_MAX = 20  # owner decision 2026-10-21 (Q1/Q5)
FU_RUNS_PER_DAY = 24  # owner decision 2026-10-21 (Q1/Q5)
FU_CONCURRENT_RUNS = 2  # owner decision 2026-10-21 (Q1/Q5)
FU_RUN_HOURS = 8  # owner decision 2026-10-21 (Q2)
FU_MIN_LEAD_S = 3600  # FU5 minimum scheduling lead.
FU_TIME_EARLY_S = 300  # FU8 early firing tolerance.
FU_LATE_S = 86400  # FU8 late firing tolerance.
FU_BRIEF_MAX_BYTES = 4096  # FU5 UTF-8 brief limit.
SET_TRIGGERS = ("time", "orch", "addon", "webhook")  # FU5 accepted triggers.
FIRED_TRIGGERS = SET_TRIGGERS + ("deadline",)  # FU8 includes deadline fallback.
OUTCOMES = ("ran", "failed", "did_not_run")  # FU14 terminal outcomes.
REASONS = (  # FU14 closed outcome reasons.
    "no_host",
    "harness_failed",
    "budget",
    "runner_unavailable",
    "runner_restarted",
    "host_restarted",
    "grant_revoked",
    "ticket_moved_on",
    "no_writes",
    "error",
)
DERIVED_CANCEL_REASONS = (  # FU14 derived cancellation vocabulary.
    "replaced",
    "ticket_left_in_progress",
    "claimed_by_other",
    "grant_revoked",
    "member_removed",
    "addon_disabled",
    "chain_cap",
)
BROAD_SCOPES = ("all", "workable")  # person-grant scopes (D60); a follow-up grant is never one of these
FALLBACK_REASONS = ("no_event", "no_ring", "addon_silent")  # FU8 orch, webhook, addon respectively.
STATES = ("pending", "needs_person", "fired", *OUTCOMES, "cancelled")  # FU3 state vocabulary.
DENIAL_CODES = (  # Local strings; the engine maps these to its public codes.
    "not_agent",
    "no_grant",
    "unattended",
    "not_holder",
    "not_in_progress",
    "addon_off",
    "bad_trigger",
    "missing_due",
    "too_soon",
    "missing_deadline",
    "bad_time",
    "deadline_before_due",
    "too_far",
    "deadline_past",
    "bad_brief",
    "pending_cap",
    "not_host",
    "not_pending",
    "bad_secret_hash",
    "chain_invalid",
    "out_of_window",
    "bad_fallback",
    "bad_outcome",
    "bad_reason",
    "bad_state",
)


@dataclass(frozen=True, slots=True)
class Denial:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class GrantTerms:
    id: str
    person: str
    scope: str
    verbs: str | tuple[str, ...]  # the runner grant's verbs are the plain string "agent"; carried through as is
    issued_at: int
    expires_at: int
    parent: str | None
    revoked: bool = False
    secret_hash: str | None = None  # FU1: set by derive_grant from followup.fired; None on person-issued grants


@dataclass(frozen=True, slots=True)
class Caps:
    max_days: int
    chain_max: int
    pending_max: int
    runs_per_day: int
    min_lead_s: int = FU_MIN_LEAD_S
    early_s: int = FU_TIME_EARLY_S


#: DEV-ONLY (workspace prefix SBX, the signed genesis of a throwaway dev-pair sandbox; see ``followups_wire.caps_of``):
#: the minimum lead is 30 s and a host fires exactly at the due time, so an end-to-end run needs seconds, not an hour.
DEV_MIN_LEAD_S = 30
DEV_EARLY_S = 0


def effective_caps(settings: Mapping[str, object], *, dev: bool = False) -> Caps:
    """FU6: signed settings may lower, but never raise, the code ceilings. ``dev`` (a dev sandbox workspace only)
    shortens the minimum lead and the early-fire tolerance."""
    values = []
    for key, ceiling in zip(
        ("max_days", "chain_max", "pending_max", "runs_per_day"),
        (FU_MAX_DAYS, FU_CHAIN_MAX, FU_PENDING_MAX, FU_RUNS_PER_DAY),
        strict=True,
    ):
        value = settings.get(key, ceiling)
        if type(value) is not int or value < 1:
            raise ValueError(f"{key} must be an integer >= 1")
        values.append(min(value, ceiling))
    return Caps(*values, DEV_MIN_LEAD_S, DEV_EARLY_S) if dev else Caps(*values)


def _norm_verbs(verbs: object) -> str | tuple[str, ...]:
    """The runner grant's verbs string ("agent") passes through unchanged; else a list or tuple of str; else refuse."""
    if isinstance(verbs, str):
        return verbs
    if isinstance(verbs, (list, tuple)) and all(isinstance(v, str) for v in verbs):
        return tuple(verbs)
    raise ValueError("verbs must be a string or a list of strings")


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def derive_grant(
    parent: GrantTerms,
    *,
    ticket_uid: str,
    grant_id: str,
    fired_at: int,
    secret_hash: str,
    run_hours: int = FU_RUN_HOURS,
) -> GrantTerms:
    """FU1: derive rights and duration, never clamp to the parent's expiry instant."""
    if not _is_hash(secret_hash):
        raise ValueError("secret_hash must be 64 lowercase hex characters")
    if type(run_hours) is not int or type(fired_at) is not int:
        raise ValueError("run_hours and fired_at must be integers")
    duration = min(parent.expires_at - parent.issued_at, run_hours * 3600, FU_RUN_HOURS * 3600)
    if duration <= 0:
        raise ValueError("grant duration must be positive")
    if not isinstance(ticket_uid, str) or not ticket_uid or ticket_uid in BROAD_SCOPES:
        raise ValueError("ticket_uid must name one ticket")
    if parent.scope not in BROAD_SCOPES and parent.scope != ticket_uid:
        raise ValueError("a ticket-scoped parent cannot reach another ticket")
    return GrantTerms(
        grant_id,
        parent.person,
        ticket_uid,
        _norm_verbs(parent.verbs),
        fired_at,
        fired_at + duration,
        parent.id,
        secret_hash=secret_hash,
    )


def grant_widening(parent: GrantTerms, child: GrantTerms) -> list[str]:
    """FU1 oracle: person, verbs, scope (one ticket; never wider than a ticket-scoped parent) and duration."""
    fields = []
    if child.person != parent.person:
        fields.append("person")
    try:
        pv, cv = _norm_verbs(parent.verbs), _norm_verbs(child.verbs)
    except ValueError:
        pv = cv = None
    if pv is None or isinstance(pv, str) or isinstance(cv, str):
        # a string is an opaque verb set: the child must carry exactly the parent's value
        if pv is None or pv != cv:
            fields.append("verbs")
    elif not set(cv).issubset(pv):
        fields.append("verbs")
    if child.scope in BROAD_SCOPES or (parent.scope not in BROAD_SCOPES and child.scope != parent.scope):
        fields.append("scope")
    if child.expires_at - child.issued_at > min(parent.expires_at - parent.issued_at, FU_RUN_HOURS * 3600):
        fields.append("duration")
    return fields


def ancestors(grants: Mapping[str, GrantTerms], grant_id: str) -> tuple[GrantTerms, ...] | None:
    """FU4: child first, with at most 32 grants; missing links and cycles fail closed."""
    result = []
    seen = set()
    while grant_id is not None:
        if grant_id in seen or len(result) >= 32:
            return None
        grant = grants.get(grant_id)
        if grant is None:
            return None
        seen.add(grant_id)
        result.append(grant)
        grant_id = grant.parent
    return tuple(result)


def chain_valid(chain: Sequence[GrantTerms] | None, *, root_person_active: bool) -> bool:
    """FU4: expiry of every grant is intentionally ignored."""
    if not chain or root_person_active is not True or chain[-1].parent is not None:
        return False
    # Fail closed on a broken list: each grant must name the next one as its parent.
    linked = all(g.parent == nxt.id for g, nxt in zip(chain, chain[1:], strict=False))
    same_person = all(g.person == chain[-1].person for g in chain)  # one person all the way up
    return linked and same_person and all(g.revoked is False for g in chain)


def fgrant_usable(
    grant: GrantTerms,
    *,
    at: int,
    outcome_recorded: bool,
    chain: Sequence[GrantTerms] | None,
    root_person_active: bool,
) -> bool:
    # FU3: a recorded outcome ends the grant by LEDGER POSITION (the caller knows it from replay state), never by
    # comparing timestamps. The chain must start with this very grant (FU4), not an unrelated one.
    return (
        outcome_recorded is False
        and type(at) is int
        and grant.issued_at <= at < grant.expires_at
        and bool(chain)
        and chain[0] == grant
        and chain_valid(chain, root_person_active=root_person_active)
    )


@dataclass(frozen=True, slots=True)
class SetContext:
    actor_is_agent: bool
    has_grant: bool
    unattended: bool
    holds_claim: bool
    ticket_status: str
    schedules_enabled: bool
    followups_enabled: bool
    pending_in_workspace: int
    replaces_pending: bool = False  # True when this set replaces a pending one (it is in the count above)


@dataclass(frozen=True, slots=True)
class SetRequest:
    at: int
    trigger: str
    due: int | None
    deadline: int | None
    brief: str


def effective_deadline(req: SetRequest) -> int | None:
    return req.due if req.trigger == "time" and req.deadline is None else req.deadline


def check_set(ctx: SetContext, req: SetRequest, caps: Caps) -> Denial | None:
    """FU5–FU6: ordered refusals, including malformed request field types."""
    for allowed, code, detail in (
        (ctx.actor_is_agent is True, "not_agent", "only agents may set follow-ups"),
        (ctx.has_grant is True, "no_grant", "a grant is required"),
        (ctx.unattended is False, "unattended", "unattended agents cannot set follow-ups"),
        (ctx.holds_claim is True, "not_holder", "the agent must hold the claim"),
        (ctx.ticket_status == "in_progress", "not_in_progress", "the ticket must be in progress"),
        (ctx.schedules_enabled is True and ctx.followups_enabled is True, "addon_off", "follow-ups must be enabled"),
    ):
        if not allowed:
            return Denial(code, detail)
    if req.trigger not in SET_TRIGGERS:
        return Denial("bad_trigger", "unknown set trigger")
    if req.trigger == "time" and req.due is None:
        return Denial("missing_due", "time triggers need a due time")
    if req.trigger != "time" and req.deadline is None:
        return Denial("missing_deadline", "event triggers need a deadline")
    deadline = effective_deadline(req)
    if type(req.at) is not int or type(deadline) is not int or (req.due is not None and type(req.due) is not int):
        return Denial("bad_time", "times must be integer epoch seconds")
    if req.due is not None and req.due < req.at + caps.min_lead_s:
        return Denial("too_soon", "due must be at least one hour ahead")
    if req.due is not None and deadline < req.due:
        return Denial("deadline_before_due", "deadline must be at or after due")
    if deadline > req.at + caps.max_days * 86400:
        return Denial("too_far", "deadline exceeds the scheduling horizon")
    if deadline <= req.at:
        return Denial("deadline_past", "deadline must be in the future")
    if (
        not isinstance(req.brief, str)
        or not is_clean_text(req.brief)
        or len(req.brief.encode("utf-8")) > FU_BRIEF_MAX_BYTES
        or not req.brief.strip()
    ):
        return Denial("bad_brief", "brief must be non-empty clean text of at most 4096 UTF-8 bytes")
    if (
        type(ctx.pending_in_workspace) is not int
        or ctx.pending_in_workspace - (1 if ctx.replaces_pending is True else 0) >= caps.pending_max
    ):
        return Denial("pending_cap", "workspace pending capacity is unavailable")
    return None


@dataclass(frozen=True, slots=True)
class ChainSet:
    at: int


@dataclass(frozen=True, slots=True)
class ChainDone:
    at: int
    task: str


@dataclass(frozen=True, slots=True)
class ChainState:
    sets_total: int
    without_progress: int
    first_set_at: int | None
    last_progress_at: int | None
    needs_person: bool
    reason: str | None
    ceiling_start_at: int | None = None  # the last person action when the caller gave it; else the first set is used


def chain_state(
    events: Sequence[ChainSet | ChainDone],
    *,
    open_tasks_at_start: frozenset[str],
    caps: Caps,
    now: int,
    since: int | None = None,
) -> ChainState:
    """FU7: only original tasks reset the progress counter; absolute ceilings never reset.

    `since` is the time of the last person event on the ticket (the chain start, owner decision Q1: the ceiling counts
    "since the last person action"); without it the first set stands in for it.
    """
    if type(now) is not int or (since is not None and type(since) is not int):
        raise ValueError("now and since must be integer epoch seconds")
    total = without = 0
    first = progress = None
    credited: set[str] = set()  # a start task counts as progress once; done, reopen, done does not count again
    for event in events:
        if type(event.at) is not int:
            raise ValueError("chain event times must be integer epoch seconds")
        if isinstance(event, ChainSet):
            total += 1
            without += 1
            if first is None:
                first = event.at
        elif (
            first is not None and event.at > first and event.task in open_tasks_at_start and event.task not in credited
        ):
            credited.add(event.task)
            without = 0
            progress = event.at
    start = since if since is not None else first
    needs = (
        without >= caps.chain_max
        or total >= FU_CHAIN_CEILING_COUNT
        or (start is not None and now - start > FU_CHAIN_CEILING_DAYS * 86400)
    )
    return ChainState(total, without, first, progress, needs, "chain_cap" if needs else None, since)


def set_state(chain: ChainState, req: SetRequest, caps: Caps) -> Literal["pending", "needs_person"]:
    """FU7: use the state BEFORE this set; caps were already applied by chain_state."""
    deadline = effective_deadline(req)
    start = chain.ceiling_start_at if chain.ceiling_start_at is not None else chain.first_set_at
    if chain.needs_person or (
        start is not None and deadline is not None and deadline > start + FU_CHAIN_CEILING_DAYS * 86400
    ):
        return "needs_person"
    return "pending"


def next_state(state: str, event: str) -> str | None:
    """FU3: set creation is handled separately; terminal states have no transitions."""
    if state == "pending" and event == "fired":
        return "fired"
    if state in ("pending", "needs_person") and event in ("cancel", "derived_cancel"):
        return "cancelled"
    if state == "needs_person" and event == "run_now":
        return "cancelled"
    for outcome in OUTCOMES:
        if event == f"outcome:{outcome}" and (state == "fired" or (state == "pending" and outcome == "did_not_run")):
            return outcome
    return None


@dataclass(frozen=True, slots=True)
class FollowupFacts:
    state: str
    trigger: str
    set_at: int
    due: int | None
    deadline: int | None


@dataclass(frozen=True, slots=True)
class FiredRequest:
    at: int
    trigger: str
    fallback_reason: str | None
    actor_is_host: bool
    secret_hash: str


def check_fired(
    fu: FollowupFacts,
    fired: FiredRequest,
    *,
    chain: Sequence[GrantTerms] | None,
    root_person_active: bool,
) -> Denial | None:
    """FU8: host-only, single fire, bounded window and matching fallback."""
    if fired.actor_is_host is not True:
        return Denial("not_host", "only the workspace host may fire")
    if fu.state != "pending":
        return Denial("not_pending", "only pending follow-ups may fire")
    if fired.trigger not in FIRED_TRIGGERS or (fired.trigger != "deadline" and fired.trigger != fu.trigger):
        return Denial("bad_trigger", "firing trigger does not match")
    if not _is_hash(fired.secret_hash):
        return Denial("bad_secret_hash", "a 64-character lowercase hex secret hash is required")
    if not chain_valid(chain, root_person_active=root_person_active):
        return Denial("chain_invalid", "grant chain is not active")
    deadline = fu.due if fu.trigger == "time" and fu.deadline is None else fu.deadline
    if fired.trigger == "deadline":
        expected = {"orch": "no_event", "webhook": "no_ring", "addon": "addon_silent"}.get(fu.trigger)
        if expected is None or fired.fallback_reason != expected:
            return Denial("bad_fallback", "deadline fallback must match the event trigger")
        start = deadline - FU_TIME_EARLY_S if type(deadline) is int else None
    else:
        if fired.fallback_reason is not None:
            return Denial("bad_fallback", "only deadline fires have a fallback reason")
        start = fu.due - FU_TIME_EARLY_S if fired.trigger == "time" and type(fu.due) is int else fu.set_at
        if fired.trigger == "time" and type(fu.due) is not int:
            start = None
    if (
        type(fired.at) is not int
        or type(start) is not int
        or type(deadline) is not int
        or not start <= fired.at <= deadline + FU_LATE_S
    ):
        return Denial("out_of_window", "fire is outside the permitted window")
    return None


def late_by(fired_at: int, due_or_deadline: int) -> int:
    return max(0, fired_at - due_or_deadline)


def check_outcome(state: str, outcome: str, reason: str | None, *, ran_has_reason: bool = False) -> Denial | None:
    if outcome not in OUTCOMES:
        return Denial("bad_outcome", "unknown outcome")
    if (outcome == "ran" and (reason is not None or ran_has_reason)) or (outcome != "ran" and reason not in REASONS):
        return Denial("bad_reason", "reason does not match the outcome")
    if next_state(state, f"outcome:{outcome}") is None:
        return Denial("bad_state", "outcome is not allowed in this state")
    return None


def derived_cancel_reason(
    *,
    state: str,
    replaced: bool,
    ticket_status: str,
    claimed_by_other: bool,
    chain: Sequence[GrantTerms] | None,
    root_person_active: bool,
    member_removed: bool,
    addon_enabled: bool,
    chain_needs_person: bool,
) -> str | None:
    """FU4/FU14: first matching cancellation wins, independent of parent expiry."""
    if state not in ("pending", "needs_person"):
        return None
    for applies, reason in (
        (replaced is not False, "replaced"),
        (member_removed is not False, "member_removed"),
        (not chain_valid(chain, root_person_active=root_person_active), "grant_revoked"),
        (addon_enabled is not True, "addon_disabled"),
        (ticket_status != "in_progress", "ticket_left_in_progress"),
        (claimed_by_other is not False, "claimed_by_other"),
        (state == "pending" and chain_needs_person is not False, "chain_cap"),
    ):
        if applies:
            return reason
    return None

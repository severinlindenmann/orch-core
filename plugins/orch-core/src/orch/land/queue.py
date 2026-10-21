"""Immutable Landing state machine, without storage, clocks, git or engine wiring.

Inputs are replayed, authenticated values. Callers supply ancestry, applicable
source-bound gates, and time; this module does not establish signature authority.
Times use the mock's ISO 8601 strings (with an offset), ordered chronologically.
Outcome recording and idempotent queue cleanup are separate crash-safe steps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Literal, overload

from .policy import QueueError, check_owner_flag, norm_target

Phase = Literal["begin", "pushing", "outcome"]
Outcome = Literal["merged", "failed", "requeued"]
MAX_REQUEUES = 3
Reason = Literal["conflict", "red_checks", "timeout", "target_moved", "owner_required", "invalid_target"]
CheckResult = Literal["pass", "fail", "running", "waiting", "timeout"]


def range_digest(commit_ids: Sequence[str]) -> str:
    """SHA-256 of ordered UTF-8 ids, each prefixed by an eight-byte byte length."""
    digest = sha256()
    for commit in commit_ids:
        encoded = commit.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True)
class QueuedEntry:
    id: str
    ticket: str
    branch: str
    source_sha: str
    enqueued: str
    by: str
    remote: str
    repo: str
    target: str
    target_head: str
    commits: int
    range_digest: str
    settings_gen: int
    owner_target: bool
    stacked_on: str | None = None
    parent_landed_sha: str | None = None


@dataclass(frozen=True)
class Later:
    """An event later in this entry's ticket/repo log, scoped by the caller."""

    kind: Literal["dequeued", "attempt", "branch_pushed"]
    queued: str | None = None
    phase: Phase | None = None
    outcome: Outcome | None = None


def consumed(entry_id: str, later: Iterable[Later]) -> bool:
    return any(e.kind == "attempt" and e.queued == entry_id and e.outcome == "merged" for e in later)


def live_entry(
    entry: QueuedEntry,
    *,
    ticket_done: bool,
    gates: Mapping[str, str | None],
    settings_gen: int,
    later: Iterable[Later],
) -> bool:
    """Derive I2, including settings invalidation and one-use enqueue authority."""
    return (
        ticket_done
        and "verify" in gates  # the verify gate always applies; an empty map is never live
        and all(sha == entry.source_sha for sha in gates.values())
        and settings_gen == entry.settings_gen
        and not any(
            e.kind == "branch_pushed"
            or e.queued == entry.id
            and (e.kind == "dequeued" or e.kind == "attempt" and e.outcome in ("merged", "failed"))
            for e in later
        )
    )


def require_live_entry(
    entry: QueuedEntry,
    *,
    ticket_done: bool,
    gates: Mapping[str, str | None],
    settings_gen: int,
    later: Iterable[Later],
) -> None:
    """Explain liveness refusal for callers that need a public error code."""
    events = tuple(later)
    if consumed(entry.id, events):
        raise QueueError("land.consumed", entry.id)
    if not ticket_done or "verify" not in gates or any(sha != entry.source_sha for sha in gates.values()):
        raise QueueError("land.not_approved", entry.ticket)
    if not live_entry(entry, ticket_done=ticket_done, gates=gates, settings_gen=settings_gen, later=events):
        raise QueueError("land.stale", entry.id)


@dataclass(frozen=True)
class Check:
    name: str
    level: Literal["T2", "CI"]
    result: CheckResult
    url: str = ""
    seconds: float | None = None
    # Missing in old UI fixtures: displayable, but never push-authorizing.
    candidate_sha: str | None = None


@dataclass(frozen=True)
class Attempt:
    n: int
    ticket: str
    queue: str
    remote: str
    target: str
    source_sha: str
    target_sha: str
    candidate_sha: str
    rebase: Literal["clean", "conflict"]
    started: str
    timeout_at: str
    checks: tuple[Check, ...] = ()
    outcome: Outcome | None = None
    reason: Reason | None = None
    file: str | None = None
    ended: str | None = None
    queued: str = ""
    repo: str = ""
    target_head: str = ""
    commits: int = 0
    range_digest: str = ""
    owner_target: bool = False
    settings_gen: int = 0
    parent_landed_sha: str | None = None
    phase: Phase = "begin"
    required_checks: tuple[str, ...] = ()
    check_policy_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "required_checks", tuple(self.required_checks))


@dataclass(frozen=True)
class Queue:
    id: str
    remote: str
    target: str
    head: str
    entries: tuple[QueuedEntry, ...] = ()
    foreign: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))


@dataclass(frozen=True)
class Worker:
    current: int | None = None


@dataclass(frozen=True)
class QueueState:
    queues: tuple[Queue, ...] = ()
    attempts: tuple[Attempt, ...] = ()
    worker: Worker = Worker()
    seq: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "queues", tuple(self.queues))
        object.__setattr__(self, "attempts", tuple(self.attempts))

    @property
    def consumed_ids(self) -> frozenset[str]:
        return frozenset(a.queued for a in self.attempts if a.outcome == "merged")


def queue_for(state: QueueState, remote: str, target: str, *, head: str = "") -> Queue:
    """Return an existing queue or a deterministic empty value; never insert it."""
    return next(
        (q for q in state.queues if (q.remote, q.target) == (remote, target)),
        Queue(f"q-{range_digest((remote, target))}", remote, target, head),
    )


def enqueue(
    state: QueueState, entry: QueuedEntry, *, settings: Mapping[str, Any], consumed_ids: Iterable[str]
) -> QueueState:
    if entry.id in state.consumed_ids or entry.id in consumed_ids:
        raise QueueError("land.consumed", entry.id)
    check_owner_flag(settings, entry.target, entry.owner_target)
    target = norm_target(entry.target)
    targets = settings.get("targets", [])
    if (
        target is None
        or target != entry.target  # exact canonical ref only: never rewrite the signed destination
        or not isinstance(targets, list)
        or target not in {norm_target(t) for t in targets if isinstance(t, str)}
    ):
        raise QueueError("land.no_target", entry.target)
    q = queue_for(state, entry.remote, target, head=entry.target_head)
    if any(e.ticket == entry.ticket for e in q.entries):
        raise QueueError("land.target_busy", entry.ticket)
    if any(e.id == entry.id for queue in state.queues for e in queue.entries):
        raise QueueError("land.target_busy", entry.id)
    updated = replace(q, entries=(*q.entries, entry))
    queues = tuple(updated if x.id == q.id else x for x in state.queues)
    if not any(x.id == q.id for x in state.queues):
        queues = (*queues, updated)
    return replace(state, queues=queues)


def _attempt(state: QueueState, n: int) -> Attempt:
    for attempt in state.attempts:
        if attempt.n == n:
            return attempt
    raise QueueError("land.bad_transition", f"unknown attempt {n}")


def dequeue(state: QueueState, ticket: str) -> QueueState:
    if state.worker.current is not None and _attempt(state, state.worker.current).ticket == ticket:
        raise QueueError("land.not_taken", "cannot dequeue while checking")
    return replace(
        state, queues=tuple(replace(q, entries=tuple(e for e in q.entries if e.ticket != ticket)) for q in state.queues)
    )


def range_matches(entry: QueuedEntry, commit_ids_now: Sequence[str]) -> bool:
    """Caller supplies the recomputed forward-rebased range; compare digests only."""
    return range_digest(commit_ids_now) == entry.range_digest


@dataclass(frozen=True)
class Block:
    parent: str
    why: Literal["ahead", "failed"]


def blocked(
    state: QueueState,
    entry: QueuedEntry,
    *,
    landed: Mapping[str, str] | None = None,
    target_ancestry: frozenset[str],
) -> Block | None:
    """Only a same-target merged record plus ancestry satisfies a stack binding.

    ``landed`` may be supplied as a cache hint; it is never authority on its own.
    """
    parent = entry.stacked_on
    if parent is None:
        return None
    if (
        entry.parent_landed_sha is not None
        and entry.parent_landed_sha in target_ancestry
        and any(
            a.ticket == parent
            and a.remote == entry.remote
            and a.target == entry.target
            and a.outcome == "merged"
            and a.candidate_sha == entry.parent_landed_sha
            for a in state.attempts
        )
    ):
        return None
    ahead = any(e.ticket == parent for q in state.queues for e in q.entries) or (
        state.worker.current is not None and _attempt(state, state.worker.current).ticket == parent
    )
    return Block(parent, "ahead" if ahead else "failed")


@dataclass(frozen=True)
class Selection:
    entry: QueuedEntry | None
    drop: tuple[QueuedEntry, ...] = ()


def next_entry(
    state: QueueState,
    *,
    targets: Iterable[str],
    is_live: Callable[[QueuedEntry], bool],
    blocked_fn: Callable[[QueuedEntry], Block | None],
) -> Selection:
    allowed = frozenset(targets)
    eligible: list[QueuedEntry] = []
    drop: list[QueuedEntry] = []
    for q in state.queues:
        if q.target not in allowed:
            continue
        for entry in q.entries:
            if not is_live(entry):
                drop.append(entry)
            elif blocked_fn(entry) is None:
                eligible.append(entry)
    return Selection(
        min(eligible, key=lambda e: (datetime.fromisoformat(e.enqueued), e.ticket), default=None), tuple(drop)
    )


def begin_attempt(
    state: QueueState,
    entry: QueuedEntry,
    *,
    target_sha: str,
    candidate_sha: str,
    now: str,
    timeout_minutes: float,
    rebase: Literal["clean", "conflict"],
    required_checks: Sequence[str],
    descends: bool,
) -> tuple[QueueState, Attempt]:
    if state.worker.current is not None or any(a.phase != "outcome" for a in state.attempts):
        raise QueueError("land.target_busy", "worker is occupied")
    if entry.id in state.consumed_ids:
        raise QueueError("land.consumed", entry.id)
    q = queue_for(state, entry.remote, entry.target)
    if entry not in q.entries:
        raise QueueError("land.stale", "entry is no longer queued or has changed")
    if sum(a.queued == entry.id and a.outcome == "requeued" for a in state.attempts) >= MAX_REQUEUES:
        raise QueueError("land.changed", "requeue cap reached; fail the entry or enqueue again")
    if not descends:
        raise QueueError("land.changed", "target does not descend from the approved target head")
    if entry.owner_target and candidate_sha != entry.source_sha:
        raise QueueError("land.owner_required", "owner targets must land the signed source unchanged")
    n = max(state.seq, *(a.n for a in state.attempts), 0) + 1
    timeout = (datetime.fromisoformat(now) + timedelta(minutes=timeout_minutes)).isoformat().replace("+00:00", "Z")
    a = Attempt(
        n=n,
        ticket=entry.ticket,
        queue=q.id,
        remote=entry.remote,
        target=entry.target,
        source_sha=entry.source_sha,
        target_sha=target_sha,
        candidate_sha=candidate_sha,
        rebase=rebase,
        started=now,
        timeout_at=timeout,
        queued=entry.id,
        repo=entry.repo,
        target_head=entry.target_head,
        commits=entry.commits,
        range_digest=entry.range_digest,
        owner_target=entry.owner_target,
        settings_gen=entry.settings_gen,
        parent_landed_sha=entry.parent_landed_sha,
        required_checks=tuple(required_checks),
    )
    return replace(state, attempts=(*state.attempts, a), worker=Worker(n), seq=n), a


def _update_attempt(state: QueueState, attempt: Attempt) -> QueueState:
    return replace(state, attempts=tuple(attempt if a.n == attempt.n else a for a in state.attempts))


def _active(state: QueueState, n: int, phases: tuple[Phase, ...]) -> Attempt:
    a = _attempt(state, n)
    if state.worker.current != n or a.phase not in phases or a.outcome is not None:
        raise QueueError("land.bad_transition", f"attempt {n} is not active in {phases}")
    return a


def _passing(attempt: Attempt, checks: Sequence[Check], required_checks: Sequence[str]) -> bool:
    # Duplicate names are ambiguous. Only the signed required list gates a push;
    # informational CI is not required (Codex challenge row 12).
    names = [c.name for c in checks]
    return (
        attempt.rebase == "clean"
        and len(names) == len(set(names))
        and set(required_checks).issubset(names)
        and all(
            c.result == "pass" and c.candidate_sha == attempt.candidate_sha for c in checks if c.name in required_checks
        )
    )


def record_checks(state: QueueState, n: int, checks: Sequence[Check]) -> QueueState:
    a = _active(state, n, ("begin",))
    if any(c.candidate_sha != a.candidate_sha for c in checks):
        raise QueueError("land.bad_transition", "checks are not bound to this candidate")
    return _update_attempt(state, replace(a, checks=tuple(checks)))


def mark_pushing(state: QueueState, n: int, checks: Sequence[Check], check_policy_digest: str) -> QueueState:
    a = _active(state, n, ("begin",))
    if not check_policy_digest or not _passing(a, checks, a.required_checks):
        raise QueueError("land.bad_transition", "complete passing candidate checks and policy digest required")
    return _update_attempt(
        state, replace(a, phase="pushing", checks=tuple(checks), check_policy_digest=check_policy_digest)
    )


def record_outcome(
    state: QueueState,
    n: int,
    outcome: Outcome,
    *,
    now: str,
    reason: Reason | None = None,
    file: str | None = None,
) -> QueueState:
    a = _active(state, n, ("begin", "pushing"))
    if outcome not in ("merged", "failed", "requeued") or outcome == "merged" and a.phase != "pushing":
        raise QueueError("land.bad_transition", "merge requires a durable pushing record")
    state = _update_attempt(state, replace(a, phase="outcome", outcome=outcome, reason=reason, file=file, ended=now))
    if outcome == "merged":
        state = replace(
            state, queues=tuple(replace(q, head=a.candidate_sha) if q.id == a.queue else q for q in state.queues)
        )
    return state


def tidy(state: QueueState, n: int) -> QueueState:
    """Clean up a durable outcome, by enqueue id; safe even after a new enqueue."""
    a = _attempt(state, n)
    if a.phase != "outcome" or a.outcome is None:
        raise QueueError("land.bad_transition", "tidy requires an outcome")
    queues = tuple(
        replace(q, entries=tuple(e for e in q.entries if e.id != a.queued))
        if q.id == a.queue and a.outcome != "requeued"
        else q
        for q in state.queues
    )
    return replace(state, queues=queues, worker=Worker() if state.worker.current == n else state.worker)


def resume_decision(
    attempt: Attempt,
    *,
    remote_target_sha: str,
    candidate_reached: bool,
    required_checks: Sequence[str],
    check_policy_digest: str,
) -> Literal["rerun", "retry_push", "merged", "target_moved", "done"]:
    if attempt.phase == "outcome":
        return "done"
    if attempt.phase != "pushing":
        return "rerun"
    if candidate_reached:
        return "merged"
    if remote_target_sha != attempt.target_sha:
        return "target_moved"
    if (
        check_policy_digest
        and attempt.check_policy_digest == check_policy_digest
        and _passing(attempt, attempt.checks, required_checks)
    ):
        return "retry_push"
    return "rerun"


@dataclass(frozen=True)
class LogMark:
    length: int
    head: str


def check_log_progress(
    mark: LogMark | None,
    length: int,
    head: str,
    *,
    prefix_head_at: Callable[[int], str | None] | None = None,
) -> LogMark:
    if mark is not None and prefix_head_at is None:
        raise QueueError("land.log_regressed", "continuity proof (prefix_head_at) required")
    if (
        length < 0
        or mark is not None
        and prefix_head_at is not None
        and (
            length < mark.length
            or (length == mark.length and head != mark.head)
            or prefix_head_at(mark.length) != mark.head
        )
    ):
        raise QueueError("land.log_regressed", "log is shorter or its recorded prefix changed")
    return LogMark(length, head)


def to_json(value: QueuedEntry | Attempt) -> dict[str, Any]:
    """Return JSON-compatible snake_case data (not an encoded JSON string)."""
    result = asdict(value)
    if isinstance(value, Attempt):
        result["checks"] = [asdict(check) for check in value.checks]
        result["required_checks"] = list(value.required_checks)
    return result


@overload
def from_json(data: Mapping[str, Any], kind: type[QueuedEntry]) -> QueuedEntry: ...


@overload
def from_json(data: Mapping[str, Any], kind: type[Attempt]) -> Attempt: ...


@overload
def from_json(data: Mapping[str, Any], kind: None = None) -> QueuedEntry | Attempt: ...


def from_json(
    data: Mapping[str, Any],
    kind: type[QueuedEntry] | type[Attempt] | None = None,
    *,
    defaults: Mapping[str, Any] | None = None,
) -> QueuedEntry | Attempt:
    """Parse model or legacy mock data; missing check bindings never imply pass.

    Legacy attempts with an outcome are completed; other legacy attempts start
    at begin, regardless of their displayed check results. Simulator-only script
    fields are ignored. A caller can specify the type or let ``n`` identify it.
    """
    values = dict(data)
    if kind is Attempt or kind is None and "n" in values:
        values.pop("script", None)
        values["checks"] = tuple(Check(**c) for c in values.get("checks", ()))
        values["required_checks"] = tuple(values.get("required_checks", ()))
        values.setdefault("phase", "outcome" if values.get("outcome") is not None else "begin")
        return Attempt(**values)
    # A mock entry lacks what its queue carries (remote, target) and the new signed bindings:
    # `defaults` supplies the queue's values; the rest default to display-only blanks.
    values = {**(defaults or {}), **values}
    for simulator_only in ("script", "signed_event"):
        values.pop(simulator_only, None)
    values.setdefault("id", values.get("ticket", ""))
    values.setdefault("repo", values.get("remote", ""))
    for key, blank in (
        ("target_head", ""),
        ("commits", 0),
        ("range_digest", ""),
        ("settings_gen", 0),
        ("owner_target", False),
    ):
        values.setdefault(key, blank)
    return QueuedEntry(**values)

"""Properties and pure behavior ports for Landing queues and crash recovery."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from orch.land.queue import (
    Attempt,
    Block,
    Check,
    CheckResult,
    Later,
    Live,
    LogMark,
    Outcome,
    Phase,
    QueuedEntry,
    QueueError,
    QueueState,
    begin_attempt,
    blocked,
    check_log_progress,
    check_policy_digest,
    consumed,
    dequeue,
    enqueue,
    from_json,
    live_entry,
    mark_pushing,
    next_entry,
    queue_for,
    range_digest,
    range_matches,
    record_checks,
    record_outcome,
    require_live_entry,
    resume_decision,
    tidy,
    to_json,
)

NOW = "2026-10-21T12:00:00Z"
SETTINGS = {"targets": ["develop", "main"]}
RESULTS: tuple[CheckResult, ...] = ("pass", "fail", "running", "waiting", "timeout")


def entry(**changes: object) -> QueuedEntry:
    base = QueuedEntry(
        "e1",
        "T1",
        "feature/t1",
        "source",
        NOW,
        "person",
        "ssh://example/repo",
        "repo",
        "develop",
        "base",
        2,
        range_digest(("a", "b")),
        1,
        False,
    )
    return replace(base, **changes)


def queued(e: QueuedEntry | None = None) -> QueueState:
    return enqueue(QueueState(), e or entry(), settings=SETTINGS, consumed_ids=())


LIVE = Live(True, {"verify": "source", "code": "source"}, 1, ())


REASON = {"merged": None, "failed": "red_checks", "requeued": "target_moved"}


def push(state: QueueState, n: int, cs: object, **kw: object) -> QueueState:
    args = dict(settings=SETTINGS, live=LIVE)
    args.update(kw)
    return mark_pushing(state, n, cs, **args)


def begin(
    state: QueueState | None = None, e: QueuedEntry | None = None, **changes: object
) -> tuple[QueueState, Attempt]:
    args = dict(
        target_sha="target",
        candidate_sha="candidate",
        now=NOW,
        timeout_minutes=30,
        rebase="clean",
        required_checks=("t2", "ci"),
        descends=True,
        settings=SETTINGS,
        live=LIVE,
    )
    args.update(changes)
    return begin_attempt(state or queued(e), e or entry(), **args)


def checks(result: CheckResult = "pass", sha: str = "candidate") -> tuple[Check, ...]:
    return (Check("t2", "T2", result, candidate_sha=sha), Check("ci", "CI", "pass", candidate_sha=sha))


def merged(state: QueueState | None = None, e: QueuedEntry | None = None) -> QueueState:
    state, a = begin(state, e)
    state = push(state, a.n, checks())
    return tidy(record_outcome(state, a.n, "merged", now=NOW), a.n)


@given(st.lists(st.text(max_size=30), max_size=12), st.text(max_size=30))
def test_digest_content_order_and_boundaries(ids: list[str], extra: str) -> None:
    assert range_digest(ids) == range_digest(tuple(ids))
    assert range_digest(ids) != range_digest([*ids, extra])
    if len(ids) > 1 and ids != ids[::-1]:
        assert range_digest(ids) != range_digest(ids[::-1])
    assert range_digest([extra, ""]) != range_digest([extra])
    assert range_digest(["ab", "c"]) != range_digest(["a", "bc"])
    e = entry(range_digest=range_digest(ids))
    assert range_matches(e, ids)
    assert not range_matches(e, [*ids, extra])


later_events = st.builds(
    Later,
    kind=st.sampled_from(["dequeued", "attempt", "branch_pushed"]),
    queued=st.sampled_from(["e1", "other", None]),
    phase=st.sampled_from(["begin", "pushing", "outcome", None]),
    outcome=st.sampled_from(["merged", "failed", "requeued", None]),
)


def live(events: list[Later] | tuple[Later, ...], **changes: object) -> bool:
    args = dict(ticket_done=True, gates={"verify": "source", "code": "source"}, settings_gen=1, later=events)
    args.update(changes)
    return live_entry(entry(), **args)


@given(
    st.sampled_from(
        [
            Later("dequeued", "e1"),
            Later("branch_pushed"),
            Later("attempt", "e1", "outcome", "merged"),
            Later("attempt", "e1", "outcome", "failed"),
        ]
    ),
    st.lists(later_events),
    st.lists(later_events),
)
def test_liveness_permanently_invalidated(killer: Later, before: list[Later], after: list[Later]) -> None:
    assert not live([*before, killer])
    assert not live([*before, killer, *after])


@given(st.one_of(st.none(), st.text().filter(lambda s: s != "source")), st.integers().filter(lambda n: n != 1))
def test_gate_and_settings_changes_invalidate(sha: str | None, generation: int) -> None:
    assert not live([], gates={"verify": "source", "code": sha})
    assert not live([], settings_gen=generation)
    assert not live([], ticket_done=False)


def test_requeued_and_unrelated_events_keep_live() -> None:
    events = [
        Later("attempt", "e1", "outcome", "requeued"),
        Later("dequeued", "other"),
        Later("attempt", "other", "outcome", "failed"),
    ]
    assert live(events)
    assert not consumed("e1", events)
    assert consumed("e1", [*events, Later("attempt", "e1", "outcome", "merged")])


@given(st.text(min_size=1))
def test_consumed_once(queued_id: str) -> None:
    e = entry(id=queued_id)
    state = merged(queued(e), e)
    assert state.consumed_ids == {queued_id}
    for base, external in ((state, ()), (QueueState(), (queued_id,))):
        with pytest.raises(QueueError, match="land.consumed"):
            enqueue(base, e, settings=SETTINGS, consumed_ids=external)


@given(st.integers(min_value=1, max_value=100000), st.text(), st.text())
def test_log_progress(length: int, head: str, next_head: str) -> None:
    mark = LogMark(length, head)
    with pytest.raises(QueueError, match="land.log_regressed"):
        check_log_progress(mark, length - 1, head)
    if next_head != head:
        with pytest.raises(QueueError, match="land.log_regressed"):
            check_log_progress(mark, length, next_head)
    assert check_log_progress(mark, length, head, prefix_head_at=lambda _: head) == mark
    with pytest.raises(QueueError, match="land.log_regressed"):  # no continuity proof: refused
        check_log_progress(mark, length + 1, next_head)
    assert check_log_progress(mark, length + 1, next_head, prefix_head_at=lambda _: head) == LogMark(
        length + 1, next_head
    )
    with pytest.raises(QueueError, match="land.log_regressed"):
        check_log_progress(mark, length + 1, next_head, prefix_head_at=lambda _: head + "changed")
    assert check_log_progress(None, length, head) == mark


@given(st.sampled_from(RESULTS[1:]), st.integers(min_value=0, max_value=1))
def test_push_requires_every_check(result: CheckResult, index: int) -> None:
    state, a = begin()
    bad = list(checks())
    bad[index] = replace(bad[index], result=result)
    for invalid in (bad, checks()[:index] + checks()[index + 1 :]):
        with pytest.raises(QueueError, match="land.bad_transition"):
            push(state, a.n, invalid)


def test_checks_bound_to_candidate_and_unambiguous() -> None:
    state, a = begin()
    for invalid in (checks(sha="other"), checks(sha=""), (*checks(), checks()[0])):
        with pytest.raises(QueueError, match="land.bad_transition"):
            push(state, a.n, invalid)
    with pytest.raises(QueueError, match="land.bad_transition"):
        record_checks(state, a.n, checks(sha="other"))
    with pytest.raises(QueueError, match="land.not_approved"):
        push(state, a.n, checks(), live=Live(False, {"verify": "source"}, 1, ()))
    recorded = record_checks(state, a.n, checks("running"))
    assert recorded.attempts[-1].checks[0].result == "running"
    assert state.attempts[-1].checks == ()


def test_resume_exhaustive() -> None:
    _, base = begin()
    phases: tuple[Phase, ...] = ("begin", "pushing", "outcome")
    for phase, result, missing, reached, same_target, same_policy, same_sha in product(
        phases, RESULTS, (False, True), (False, True), (False, True), (False, True), (False, True)
    ):
        cs = checks(result, sha="candidate" if same_sha else "other")
        a = replace(
            base,
            phase=phase,
            checks=cs[:1] if missing else cs,
            check_policy_digest=check_policy_digest(("t2", "ci")) if same_policy else "other",
        )
        decision = resume_decision(
            a,
            entry(),
            settings=SETTINGS,
            live=LIVE,
            remote_target_sha="target" if same_target else "moved",
            candidate_reached=reached,
            required_checks=("t2", "ci"),
        )
        expected = (
            "done"
            if phase == "outcome"
            else "rerun"
            if phase == "begin"
            else "merged"
            if reached
            else "target_moved"
            if not same_target
            else "retry_push"
            if result == "pass" and not missing and same_policy and same_sha
            else "rerun"
        )
        assert decision == expected


@given(st.booleans(), st.booleans(), st.booleans(), st.booleans())
def test_parent_binding(landed: bool, same_remote: bool, same_target: bool, ancestor: bool) -> None:
    parent = entry(ticket="parent")
    state = merged(queued(parent), parent)
    attempt = replace(
        state.attempts[0],
        outcome="merged" if landed else "failed",
        remote=parent.remote if same_remote else "ssh://elsewhere/repo",
        target="develop" if same_target else "other",
    )
    child = entry(id="child", ticket="child", stacked_on="parent", parent_landed_sha="candidate")
    state = enqueue(replace(state, attempts=(attempt,)), child, settings=SETTINGS, consumed_ids=())
    ancestry = frozenset({"candidate"} if ancestor else ())
    selection = next_entry(
        state,
        targets=["develop"],
        is_live=lambda _: True,
        blocked_fn=lambda e: blocked(state, e, landed={"parent": "candidate"}, target_ancestry=ancestry),
    )
    assert (selection.entry == child) == (landed and same_remote and same_target and ancestor)
    assert blocked(state, replace(child, parent_landed_sha="wrong"), target_ancestry=ancestry) == Block(
        "parent", "failed"
    )


def test_oldest_first_drop_parent_first_and_queue_identity() -> None:
    parent = entry(ticket="parent", enqueued="2026-10-21T12:01:00Z")
    child = entry(id="child", ticket="child", stacked_on="parent", parent_landed_sha="candidate")
    stale = entry(id="stale", ticket="stale", remote="ssh://other/repo", enqueued="2026-10-20T00:00:00Z")
    state = queued(parent)
    for e in (child, stale):
        state = enqueue(state, e, settings=SETTINGS, consumed_ids=())
    assert len(state.queues) == 2
    assert queue_for(state, parent.remote, parent.target) == state.queues[0]
    assert blocked(state, child, target_ancestry=frozenset()) == Block("parent", "ahead")
    selection = next_entry(
        state,
        targets=["develop"],
        is_live=lambda e: e.id != "stale",
        blocked_fn=lambda e: blocked(state, e, target_ancestry=frozenset()),
    )
    assert selection.entry == parent
    assert selection.drop == (stale,)
    assert next_entry(state, targets=[], is_live=lambda _: True, blocked_fn=lambda _: None).entry is None
    assert next_entry(state, targets=["develop"], is_live=lambda _: True, blocked_fn=lambda _: None).entry == stale
    state, a = begin(state, parent)
    assert blocked(state, child, target_ancestry=frozenset()) == Block("parent", "ahead")
    state = tidy(record_outcome(push(state, a.n, checks()), a.n, "merged", now=NOW), a.n)
    assert blocked(state, child, target_ancestry=frozenset({"candidate"})) is None


def test_tie_order_is_ticket_not_insertion_order() -> None:
    state = queued(entry(ticket="Z"))
    state = enqueue(state, entry(id="a", ticket="A"), settings=SETTINGS, consumed_ids=())
    assert next_entry(state, targets=["develop"], is_live=lambda _: True, blocked_fn=lambda _: None).entry.ticket == "A"


@given(st.lists(st.sampled_from(["requeued", "failed", "merged"]), min_size=1, max_size=15))
def test_serial_transitions_and_tidy(outcomes: list[Outcome]) -> None:
    state = QueueState()
    for index, outcome in enumerate(outcomes):
        e = entry(id=f"e{index}", ticket=f"T{index}")
        state = enqueue(state, e, settings=SETTINGS, consumed_ids=())
        old = state
        state, a = begin(state, e)
        assert old.worker.current is None
        assert a.phase == "begin"
        assert sum(x.phase != "outcome" for x in state.attempts) == 1
        with pytest.raises(QueueError, match="land.target_busy"):
            begin(state, e)
        with pytest.raises(QueueError, match="land.bad_transition"):
            record_outcome(state, a.n, "merged", now=NOW)
        if outcome == "merged":
            state = push(state, a.n, checks())
            assert state.attempts[-1].phase == "pushing"
            with pytest.raises(QueueError, match="land.bad_transition"):
                push(state, a.n, checks())
            with pytest.raises(QueueError, match="land.bad_transition"):
                record_checks(state, a.n, checks())
        state = record_outcome(state, a.n, outcome, now=NOW, reason=REASON[outcome])
        assert state.worker.current == a.n  # durable record precedes cleanup
        with pytest.raises(QueueError, match="land.bad_transition"):
            record_outcome(state, a.n, outcome, now=NOW, reason=REASON[outcome])
        state = tidy(state, a.n)
        assert state == tidy(state, a.n)
        assert state.worker.current is None
        assert (e in state.queues[0].entries) == (outcome == "requeued")
        assert state.attempts[-1].phase == "outcome"


def test_requeue_rebuild_and_late_tidy_do_not_remove_new_entry() -> None:
    state, first = begin()
    state = tidy(record_outcome(state, first.n, "requeued", now=NOW, reason="target_moved"), first.n)
    state, second = begin(state, target_sha="new-target", candidate_sha="new-candidate")
    assert second.n == first.n + 1
    assert second.checks == ()
    assert tidy(state, first.n).worker.current == second.n
    state = tidy(record_outcome(state, second.n, "failed", now=NOW, reason="red_checks"), second.n)
    new = entry(id="new")
    state = enqueue(state, new, settings=SETTINGS, consumed_ids=())
    assert tidy(state, second.n) == state
    assert new in state.queues[0].entries


def test_errors_and_owner_ff() -> None:
    state, a = begin()
    with pytest.raises(QueueError, match="land.not_taken"):
        dequeue(state, a.ticket)
    with pytest.raises(QueueError, match="land.bad_transition"):
        tidy(state, a.n)
    with pytest.raises(QueueError, match="land.bad_transition"):
        record_checks(state, 999, ())
    with pytest.raises(QueueError, match="land.target_busy"):
        enqueue(queued(), entry(), settings=SETTINGS, consumed_ids=())
    with pytest.raises(QueueError, match="land.no_target"):
        enqueue(QueueState(), entry(target="release"), settings=SETTINGS, consumed_ids=())
    with pytest.raises(QueueError, match="land.owner_required"):
        enqueue(QueueState(), entry(target="MAIN"), settings=SETTINGS, consumed_ids=())
    with pytest.raises(QueueError, match="land.changed"):
        begin(descends=False)
    with pytest.raises(QueueError, match="land.stale"):
        begin(dequeue(queued(), "T1"))
    owner = entry(target="main", owner_target=True)
    with pytest.raises(QueueError, match="land.owner_required"):
        begin(queued(owner), owner)
    _, a = begin(queued(owner), owner, candidate_sha=owner.source_sha)
    assert a.candidate_sha == a.source_sha
    assert a.timeout_at == "2026-10-21T12:30:00Z"
    for overrides, code in [
        ({"ticket_done": False}, "land.not_approved"),
        ({"gates": {"verify": None}}, "land.not_approved"),
        ({"settings_gen": 2}, "land.stale"),
        ({"later": [Later("dequeued", "e1")]}, "land.stale"),
        ({"later": [Later("attempt", "e1", "outcome", "merged")]}, "land.consumed"),
    ]:
        args = dict(ticket_done=True, gates={"verify": "source"}, settings_gen=1, later=[])
        args.update(overrides)
        with pytest.raises(QueueError) as exc:
            require_live_entry(entry(), **args)
        assert exc.value.code == code
        assert exc.value.detail
    require_live_entry(entry(), ticket_done=True, gates={"verify": "source"}, settings_gen=1, later=[])
    with pytest.raises(QueueError, match="land.not_approved"):
        require_live_entry(entry(), ticket_done=True, gates={}, settings_gen=1, later=[])


@pytest.mark.parametrize("outcome", ["failed", "requeued"])
def test_nonmerge_outcomes_from_pushing(outcome: Outcome) -> None:
    state, a = begin()
    state = push(state, a.n, checks())
    assert record_outcome(state, a.n, outcome, now=NOW, reason=REASON[outcome]).attempts[-1].outcome == outcome


def test_conflict_cannot_push_even_with_green_checks() -> None:
    state, a = begin(rebase="conflict")
    with pytest.raises(QueueError, match="land.bad_transition"):
        push(state, a.n, checks())
    state = record_outcome(state, a.n, "failed", now=NOW, reason="conflict", file="x.sql")
    assert tidy(state, a.n).queues[0].entries == ()


def test_json_round_trip_and_mock_fixture() -> None:
    state, a = begin()
    state = push(state, a.n, checks())
    for value in (entry(), state.attempts[-1]):
        payload = json.loads(json.dumps(to_json(value)))
        assert from_json(payload) == value
        assert from_json(payload, type(value)) == value
    # The mock's current attempt shape, including optional UI fields and script.
    fixture = dict(
        n=14,
        ticket="DEMO-0052",
        queue="q-dbt",
        remote="github.com/acme/energy-dbt",
        target="develop",
        source_sha="abc1234",
        target_sha="def1234",
        candidate_sha="123abcd",
        rebase="clean",
        checks=[
            dict(name="T2 integration checks", level="T2", result="pass", url="https://example/checks", seconds=384)
        ],
        outcome=None,
        started=NOW,
        timeout_at="2026-10-21T12:30:00Z",
        script="clean",
    )
    parsed = from_json(fixture, Attempt)
    assert parsed.phase == "begin"
    assert parsed.checks[0].seconds == 384
    assert (
        resume_decision(
            parsed,
            entry(),
            settings=SETTINGS,
            live=LIVE,
            remote_target_sha=parsed.target_sha,
            candidate_reached=False,
            required_checks=("T2 integration checks",),
        )
        == "rerun"
    )
    parsed = replace(
        parsed, queued="e1", phase="pushing", check_policy_digest=check_policy_digest(("T2 integration checks",))
    )
    assert (
        resume_decision(
            parsed,
            entry(source_sha=parsed.source_sha, remote=parsed.remote, target=parsed.target),
            settings=SETTINGS,
            live=Live(True, {"verify": parsed.source_sha}, 1, ()),
            remote_target_sha=parsed.target_sha,
            candidate_reached=False,
            required_checks=("T2 integration checks",),
        )
        == "rerun"
    )
    assert from_json({**fixture, "outcome": "merged"}, Attempt).phase == "outcome"


def test_state_values_are_immutable_and_copy_input_sequences() -> None:
    state, a = begin()
    source = list(checks())
    copy = replace(a, checks=source)
    source.clear()
    assert copy.checks == checks()
    with pytest.raises(FrozenInstanceError):
        state.seq = 99
    with pytest.raises(FrozenInstanceError):
        state.queues[0].head = "rewrite"


@pytest.mark.parametrize("result", RESULTS)
def test_informational_ci_does_not_change_signed_requirements(result: CheckResult) -> None:
    state, a = begin(required_checks=("t2",))
    results = (checks()[0], replace(checks()[1], result=result))
    state = push(state, a.n, results)
    assert (
        resume_decision(
            state.attempts[-1],
            entry(),
            settings=SETTINGS,
            live=LIVE,
            remote_target_sha="target",
            candidate_reached=False,
            required_checks=("t2",),
        )
        == "retry_push"
    )
    if result != "pass":
        assert (
            resume_decision(
                state.attempts[-1],
                entry(),
                settings=SETTINGS,
                live=LIVE,
                remote_target_sha="target",
                candidate_reached=False,
                required_checks=("t2", "ci"),
            )
            == "rerun"
        )


def test_requeue_cap_canonical_target_and_legacy_entry() -> None:
    e = entry()
    state = queued(e)
    for _ in range(3):
        state, a = begin(state, e)
        state = tidy(record_outcome(state, a.n, "requeued", now="2026-10-21T12:00:00Z", reason="target_moved"), a.n)
    with pytest.raises(QueueError, match="land.changed"):
        begin(state, e)
    with pytest.raises(QueueError, match="land.no_target"):
        enqueue(QueueState(), entry(target="Develop"), settings=SETTINGS, consumed_ids=())
    from orch.land.queue import from_json

    legacy = {
        "ticket": "T9",
        "branch": "feat/x",
        "source_sha": "s",
        "enqueued": "2026-10-21T12:00:00Z",
        "by": "p",
        "script": ["clean"],
    }
    got = from_json(legacy, QueuedEntry, defaults={"remote": "r", "target": "develop"}, settings=SETTINGS)
    assert (got.remote, got.target, got.ticket) == ("r", "develop", "T9")


def test_empty_required_checks_and_digest_computed() -> None:
    with pytest.raises(QueueError, match="land.bad_transition"):
        begin(required_checks=())
    state, a = begin()
    assert push(state, a.n, checks()).attempts[-1].check_policy_digest == check_policy_digest(("ci", "t2"))
    assert check_policy_digest(("t2",)) != check_policy_digest(("t2", "ci"))


def test_begin_and_push_rederive_owner_and_liveness() -> None:
    main = entry(target="main", owner_target=False)  # a legacy-style entry with the flag wrong
    settings = {"targets": ["develop", "main"]}
    with pytest.raises(QueueError, match="land.owner_required"):
        begin_attempt(
            QueueState(),
            main,
            settings=settings,
            live=LIVE,
            target_sha="t",
            candidate_sha="rebased",
            now=NOW,
            timeout_minutes=30,
            rebase="clean",
            required_checks=("t2",),
            descends=True,
        )
    with pytest.raises(QueueError, match="land.stale"):
        begin(live=Live(True, {"verify": "source"}, 2, ()))
    with pytest.raises(QueueError, match="land.not_approved"):
        begin(live=Live(True, {"verify": "changed"}, 1, ()))
    with pytest.raises(QueueError, match="land.stale"):
        begin(live=Live(True, {"verify": "source"}, 1, (Later("dequeued", "e1"),)))
    state, a = begin()
    for stale in (Live(True, {"verify": "other"}, 1, ()), Live(True, {"verify": "source"}, 3, ())):
        with pytest.raises(QueueError):
            push(state, a.n, checks(), live=stale)
    pushed = push(state, a.n, checks())
    args = dict(remote_target_sha="target", candidate_reached=False, required_checks=("t2", "ci"), settings=SETTINGS)
    at = pushed.attempts[-1]
    assert resume_decision(at, entry(), live=LIVE, **args) == "retry_push"
    assert resume_decision(at, entry(), live=Live(True, {"verify": "x"}, 1, ()), **args) == "stale"
    assert resume_decision(at, None, live=LIVE, **args) == "stale"


def test_legacy_entry_owner_flag_never_defaults() -> None:
    legacy = {"ticket": "T9", "branch": "b", "source_sha": "s", "enqueued": NOW, "by": "p"}
    with pytest.raises(QueueError, match="land.owner_required"):
        from_json(legacy, QueuedEntry, defaults={"remote": "r", "target": "main"})
    got = from_json(legacy, QueuedEntry, defaults={"remote": "r", "target": "main"}, settings=SETTINGS)
    assert got.owner_target is True


def test_strict_types_reason_timeout_and_cap_skip() -> None:
    for bad in (1, 0, "yes"):
        with pytest.raises(QueueError):
            begin(descends=bad)
        with pytest.raises(QueueError):
            live_entry(entry(), ticket_done=bad, gates={"verify": "source"}, settings_gen=1, later=[])
    with pytest.raises(QueueError):
        live_entry(entry(), ticket_done=True, gates={"verify": "source"}, settings_gen=True, later=[])
    for t in (0, -1, 10**6, True):
        with pytest.raises(QueueError):
            begin(timeout_minutes=t)
    state, a = begin()
    for outcome, reason in (("merged", "conflict"), ("requeued", None), ("failed", None)):
        with pytest.raises(QueueError, match="land.bad_transition"):
            record_outcome(state, a.n, outcome, now=NOW, reason=reason)
    e = entry()
    state = queued(e)
    for _ in range(3):
        state, a = begin(state, e)
        state = tidy(record_outcome(state, a.n, "requeued", now=NOW, reason="target_moved"), a.n)
    sel = next_entry(state, targets=["develop"], is_live=lambda _: True, blocked_fn=lambda _: None)
    assert sel.entry is None and sel.capped == (e,)


def test_live_later_required_and_resume_binding() -> None:
    with pytest.raises(TypeError):
        Live(True, {"verify": "source"}, 1)  # type: ignore[call-arg]
    state, a = begin()
    at = push(state, a.n, checks()).attempts[-1]
    args = dict(remote_target_sha="target", candidate_reached=False, required_checks=("t2", "ci"), live=LIVE)
    settings = {"targets": ["develop", "main"]}
    for other in (entry(source_sha="x"), entry(remote="ssh://elsewhere/r")):
        assert resume_decision(at, other, settings=settings, **args) == "stale"
    # entry says develop but the attempt pushes to main: the flag is checked on attempt.target
    assert resume_decision(replace(at, target="main"), entry(), settings=settings, **args) == "stale"
    with pytest.raises(QueueError, match="land.bad_transition"):
        record_outcome(state, a.n, "failed", now=NOW, reason="made_up")  # type: ignore[arg-type]

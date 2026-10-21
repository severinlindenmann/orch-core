"""FU1–FU8 and FU14: examples, boundary checks, and independent property oracles."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from orch.model import followups as f

HASH = "ab" * 32
CAPS = f.effective_caps({})
ROOT = f.GrantTerms("root", "person", "all", ("read", "write"), 0, 3600, None)
CTX = f.SetContext(True, True, False, True, "in_progress", True, True, 0)
REQ = f.SetRequest(0, "time", 3600, None, "Check the task")
FACTS = f.FollowupFacts("pending", "time", 0, 3600, None)
FIRE = f.FiredRequest(3600, "time", None, True, HASH)


def denied(result, code):
    assert isinstance(result, f.Denial)
    assert result.code == code
    assert result.code in f.DENIAL_CODES
    assert result.detail


def fire(fu=FACTS, fired=FIRE, **kwargs):
    return f.check_fired(fu, fired, **({"chain": (ROOT,), "root_person_active": True} | kwargs))


def cancel(**kwargs):
    return f.derived_cancel_reason(
        **(
            {
                "state": "pending",
                "replaced": False,
                "ticket_status": "in_progress",
                "claimed_by_other": False,
                "chain": (ROOT,),
                "root_person_active": True,
                "member_removed": False,
                "addon_enabled": True,
                "chain_needs_person": False,
            }
            | kwargs
        )
    )


def state(events=(), *, now=0, caps=CAPS):
    return f.chain_state(events, open_tasks_at_start=frozenset({"old"}), caps=caps, now=now)


def test_constants_and_immutable_records():
    assert (f.FU_MAX_DAYS, f.FU_CHAIN_MAX, f.FU_CHAIN_CEILING_DAYS, f.FU_CHAIN_CEILING_COUNT) == (14, 5, 30, 10)
    assert (f.FU_PENDING_MAX, f.FU_RUNS_PER_DAY, f.FU_CONCURRENT_RUNS, f.FU_RUN_HOURS) == (20, 24, 2, 8)
    assert (f.FU_MIN_LEAD_S, f.FU_TIME_EARLY_S, f.FU_LATE_S, f.FU_BRIEF_MAX_BYTES) == (3600, 300, 86400, 4096)
    for record in (
        ROOT,
        CTX,
        REQ,
        FACTS,
        FIRE,
        CAPS,
        state(),
        f.ChainSet(0),
        f.ChainDone(1, "old"),
        f.Denial("x", "y"),
    ):
        assert not hasattr(record, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(record, next(iter(record.__dataclass_fields__)), None)
    for vocabulary in (
        f.SET_TRIGGERS,
        f.FIRED_TRIGGERS,
        f.OUTCOMES,
        f.REASONS,
        f.DERIVED_CANCEL_REASONS,
        f.FALLBACK_REASONS,
        f.STATES,
        f.DENIAL_CODES,
    ):
        assert isinstance(vocabulary, tuple)
        assert len(vocabulary) == len(set(vocabulary))


def test_caps_defaults_lowering_and_clamping():
    assert CAPS == f.Caps(14, 5, 20, 24)
    assert f.effective_caps({"max_days": 2, "chain_max": 100, "ignored": None}) == f.Caps(2, 5, 20, 24)


@pytest.mark.parametrize("key", ("max_days", "chain_max", "pending_max", "runs_per_day"))
@pytest.mark.parametrize("value", (0, -1, True, False, None, "2", 2.0, [], {}))
def test_bad_cap_values(key, value):
    with pytest.raises(ValueError):
        f.effective_caps({key: value})


def test_grant_derivation_and_widening_oracle():
    child = f.derive_grant(ROOT, ticket_uid="ticket", grant_id="child", fired_at=10000, secret_hash=HASH)
    assert child == f.GrantTerms("child", "person", "ticket", ("read", "write"), 10000, 13600, "root", secret_hash=HASH)
    for bad_scope in ("all", "workable", "", None):
        with pytest.raises(ValueError):
            f.derive_grant(ROOT, ticket_uid=bad_scope, grant_id="c", fired_at=0, secret_hash=HASH)
    with pytest.raises(ValueError):
        f.derive_grant(
            replace(ROOT, scope="ticket_a"), ticket_uid="ticket_b", grant_id="c", fired_at=0, secret_hash=HASH
        )
    assert f.derive_grant(
        replace(ROOT, scope="ticket_a"), ticket_uid="ticket_a", grant_id="c", fired_at=0, secret_hash=HASH
    )
    assert f.grant_widening(ROOT, replace(child, scope="all")) == ["scope"]
    assert f.grant_widening(replace(ROOT, scope="a"), replace(child, scope="b")) == ["scope"]
    assert f.grant_widening(ROOT, child) == []
    assert f.grant_widening(ROOT, replace(child, person="other", verbs=("admin",), expires_at=100000)) == [
        "person",
        "verbs",
        "duration",
    ]
    assert f.grant_widening(ROOT, replace(child, verbs=("read",), scope="anything")) == []
    assert f.grant_widening(replace(ROOT, expires_at=1000000), replace(child, expires_at=100000)) == ["duration"]
    assert set(inspect.signature(f.derive_grant).parameters) == {
        "parent",
        "ticket_uid",
        "grant_id",
        "fired_at",
        "secret_hash",
        "run_hours",
    }


@pytest.mark.parametrize(
    "kwargs",
    (
        {"secret_hash": ""},
        {"secret_hash": None},
        {"secret_hash": 12},
        {"run_hours": 0},
        {"run_hours": -1},
        {"run_hours": True},
        {"fired_at": 0.5},
    ),
)
def test_bad_derivation(kwargs):
    with pytest.raises(ValueError):
        f.derive_grant(ROOT, **({"ticket_uid": "t", "grant_id": "g", "fired_at": 0, "secret_hash": "h"} | kwargs))


@pytest.mark.parametrize("expiry", (0, -1))
def test_nonpositive_parent_duration(expiry):
    with pytest.raises(ValueError):
        f.derive_grant(replace(ROOT, expires_at=expiry), ticket_uid="t", grant_id="g", fired_at=0, secret_hash=HASH)


def test_ancestors_and_chain_validation():
    child = replace(ROOT, id="child", parent="root")
    assert f.ancestors({"child": child, "root": ROOT}, "child") == (child, ROOT)
    assert f.ancestors({}, "missing") is None
    assert f.ancestors({"child": child}, "child") is None
    assert f.ancestors({"root": replace(ROOT, parent="root")}, "root") is None
    assert f.ancestors({"child": child, "root": replace(ROOT, parent="child")}, "root") is None
    grants = {str(i): replace(ROOT, id=str(i), parent=str(i - 1) if i else None) for i in range(33)}
    assert len(f.ancestors(grants, "31")) == 32
    assert f.ancestors(grants, "32") is None
    assert f.chain_valid((child, ROOT), root_person_active=True)
    for chain in (None, (), (child,), (child, replace(ROOT, revoked=True))):
        assert not f.chain_valid(chain, root_person_active=True)
    assert not f.chain_valid((ROOT,), root_person_active=False)
    # a list whose links do not match (child names a parent that is not the next entry) fails closed
    assert not f.chain_valid((replace(child, parent="other"), ROOT), root_person_active=True)


@pytest.mark.parametrize(
    "at,outcome,expected",
    (
        (-1, False, False),
        (0, False, True),
        (3599, False, True),
        (3600, False, False),
        (99, True, False),
        (0, True, False),
    ),
)
def test_grant_use_boundaries(at, outcome, expected):
    assert f.fgrant_usable(ROOT, at=at, outcome_recorded=outcome, chain=(ROOT,), root_person_active=True) is expected
    assert not f.fgrant_usable(ROOT, at=at, outcome_recorded=outcome, chain=None, root_person_active=True)
    assert not f.fgrant_usable(ROOT, at=at, outcome_recorded=outcome, chain=(ROOT,), root_person_active=False)
    # an unrelated chain does not authorise this grant
    other = replace(ROOT, id="other")
    assert not f.fgrant_usable(ROOT, at=at, outcome_recorded=outcome, chain=(other,), root_person_active=True)


def test_strict_flags_and_ints():
    assert not f.chain_valid((ROOT,), root_person_active="false")
    assert not f.chain_valid((replace(ROOT, revoked=None),), root_person_active=True)
    denied(fire(fired=replace(FIRE, actor_is_host=1)), "not_host")
    with pytest.raises(ValueError):
        state([f.ChainSet(None)])
    with pytest.raises(ValueError):
        state([f.ChainSet(1)], now=False)


def test_ceiling_counts_from_the_last_person_action():
    day = 86400
    caps = CAPS
    s = f.chain_state([f.ChainSet(29 * day)], open_tasks_at_start=frozenset(), caps=caps, now=29 * day, since=0)
    assert not s.needs_person
    assert f.chain_state(
        [f.ChainSet(29 * day)], open_tasks_at_start=frozenset(), caps=caps, now=31 * day, since=0
    ).needs_person
    assert not f.chain_state(
        [f.ChainSet(29 * day)], open_tasks_at_start=frozenset(), caps=caps, now=31 * day
    ).needs_person
    far = replace(REQ, at=29 * day, due=None, trigger="orch", deadline=29 * day + 5 * day)
    assert f.set_state(s, far, caps) == "needs_person"


SET_DENIALS = (
    ({"actor_is_agent": False}, {}, "not_agent"),
    ({"has_grant": False}, {}, "no_grant"),
    ({"unattended": True}, {}, "unattended"),
    ({"holds_claim": False}, {}, "not_holder"),
    ({"ticket_status": "open"}, {}, "not_in_progress"),
    ({"schedules_enabled": False}, {}, "addon_off"),
    ({"followups_enabled": False}, {}, "addon_off"),
    ({}, {"trigger": "unknown"}, "bad_trigger"),
    ({}, {"due": None}, "missing_due"),
    ({}, {"due": 3599}, "too_soon"),
    ({}, {"trigger": "orch"}, "missing_deadline"),
    ({}, {"deadline": "tomorrow"}, "bad_time"),
    ({}, {"deadline": 3599}, "deadline_before_due"),
    ({}, {"deadline": 14 * 86400 + 1}, "too_far"),
    ({}, {"trigger": "addon", "due": None, "deadline": 0}, "deadline_past"),
    ({}, {"brief": " "}, "bad_brief"),
    ({"pending_in_workspace": 20}, {}, "pending_cap"),
)


@pytest.mark.parametrize("ctx,req,code", SET_DENIALS)
def test_set_denials(ctx, req, code):
    denied(f.check_set(replace(CTX, **ctx), replace(REQ, **req), CAPS), code)


def test_set_success_deadline_and_denial_order():
    assert f.check_set(CTX, REQ, CAPS) is None
    assert f.effective_deadline(REQ) == 3600
    assert f.effective_deadline(replace(REQ, deadline=0)) == 0
    assert f.effective_deadline(replace(REQ, trigger="orch")) is None
    for trigger in f.SET_TRIGGERS:
        req = replace(REQ, trigger=trigger, deadline=14 * 86400)
        assert f.check_set(CTX, req, CAPS) is None
        if trigger != "time":
            assert f.check_set(CTX, replace(req, due=None), CAPS) is None
        denied(f.check_set(CTX, replace(req, due=3599), CAPS), "too_soon")
        denied(f.check_set(CTX, replace(req, due=req.deadline + 1), CAPS), "deadline_before_due")
    # Actor/claim/enable checks precede all request validation.
    combined = {}
    for changes, _, code in reversed(SET_DENIALS[:7]):
        combined.update(changes)
        denied(f.check_set(replace(CTX, **combined), replace(REQ, trigger="invalid"), CAPS), code)
    denied(f.check_set(CTX, replace(REQ, due=None, brief=""), CAPS), "missing_due")
    denied(f.check_set(CTX, replace(REQ, due=3599, deadline=14 * 86400 + 1), CAPS), "too_soon")
    denied(f.check_set(CTX, replace(REQ, deadline=14 * 86400 + 1, brief=""), CAPS), "too_far")
    denied(f.check_set(replace(CTX, pending_in_workspace=20), replace(REQ, brief=""), CAPS), "bad_brief")


@pytest.mark.parametrize(
    "brief", (None, 1, [], {}, "", " \t\n", "e\u0301", "\ud800", "\x00", "\u202e", "x" * 4097, "é" * 2049)
)
def test_bad_briefs(brief):
    denied(f.check_set(CTX, replace(REQ, brief=brief), CAPS), "bad_brief")


def test_brief_byte_limit_and_clean_multiline():
    for brief in ("é" * 2048, "x" * 4096, "Read\n\tthe task"):
        assert f.check_set(CTX, replace(REQ, brief=brief), CAPS) is None


@pytest.mark.parametrize("value", (True, False, "1", 1.0, [], {}, None))
@pytest.mark.parametrize("field", ("at", "due", "deadline", "trigger", "brief"))
def test_malformed_request_types_never_raise(field, value):
    req = replace(REQ, **{field: value})
    result = f.check_set(CTX, req, CAPS)
    if (field == "deadline" and value is None) or (field == "brief" and value == "1"):
        assert result is None
    else:
        assert isinstance(result, f.Denial)


def test_chain_progress_and_set_state():
    assert state() == f.ChainState(0, 0, None, None, False, None)
    events = [f.ChainDone(0, "old"), f.ChainSet(10), f.ChainDone(10, "old"), f.ChainSet(11), f.ChainDone(12, "new")]
    assert state(events) == f.ChainState(2, 2, 10, None, False, None)
    events += [f.ChainDone(13, "old"), f.ChainSet(14)]
    assert state(events) == f.ChainState(3, 1, 10, 13, False, None)
    capped = state([f.ChainSet(i) for i in range(5)])
    assert capped.needs_person and capped.reason == "chain_cap"
    assert f.set_state(capped, REQ, CAPS) == "needs_person"
    assert f.set_state(state(), REQ, CAPS) == "pending"
    assert f.set_state(state([f.ChainSet(0)] * 4), REQ, CAPS) == "pending"
    ceiling = 30 * 86400
    prior = state([f.ChainSet(0)])
    assert f.set_state(prior, replace(REQ, due=ceiling), CAPS) == "pending"
    assert f.set_state(prior, replace(REQ, due=ceiling + 1), CAPS) == "needs_person"
    assert f.set_state(prior, replace(REQ, deadline=ceiling + 1), CAPS) == "needs_person"
    assert not state([f.ChainSet(0)], now=ceiling).needs_person
    assert state([f.ChainSet(0)], now=ceiling + 1).needs_person
    progressing = [e for i in range(10) for e in (f.ChainSet(i * 2), f.ChainDone(i * 2 + 1, f"t{i}"))]
    many = frozenset(f"t{i}" for i in range(10))
    s = f.chain_state(progressing, open_tasks_at_start=many, caps=CAPS, now=0)
    assert s.without_progress == 0 and s.needs_person  # the 10-set ceiling is never reset by progress
    assert f.chain_state(progressing[:4], open_tasks_at_start=many, caps=CAPS, now=ceiling + 1).needs_person


def test_state_machine_examples():
    expected = {
        ("pending", "fired"): "fired",
        ("pending", "outcome:did_not_run"): "did_not_run",
        ("needs_person", "run_now"): "cancelled",
        **{(s, e): "cancelled" for s in ("pending", "needs_person") for e in ("cancel", "derived_cancel")},
        **{("fired", f"outcome:{o}"): o for o in f.OUTCOMES},
    }
    for s in (*f.STATES, "unknown"):
        for e in (
            "set",
            "fired",
            "cancel",
            "derived_cancel",
            "run_now",
            "unknown",
            *[f"outcome:{o}" for o in f.OUTCOMES],
        ):
            assert f.next_state(s, e) == expected.get((s, e))


@pytest.mark.parametrize(
    "fu,fired,kwargs,code",
    (
        (FACTS, replace(FIRE, actor_is_host=False), {}, "not_host"),
        (replace(FACTS, state="needs_person"), FIRE, {}, "not_pending"),
        (FACTS, replace(FIRE, trigger="invalid"), {}, "bad_trigger"),
        (FACTS, replace(FIRE, trigger="orch"), {}, "bad_trigger"),
        (FACTS, replace(FIRE, secret_hash=""), {}, "bad_secret_hash"),
        (FACTS, replace(FIRE, secret_hash=None), {}, "bad_secret_hash"),
        (FACTS, FIRE, {"chain": None}, "chain_invalid"),
        (FACTS, FIRE, {"root_person_active": False}, "chain_invalid"),
        (FACTS, replace(FIRE, at=3299), {}, "out_of_window"),
        (FACTS, replace(FIRE, fallback_reason="no_event"), {}, "bad_fallback"),
        (FACTS, replace(FIRE, trigger="deadline", fallback_reason="no_event"), {}, "bad_fallback"),
    ),
)
def test_firing_denials(fu, fired, kwargs, code):
    denied(fire(fu, fired, **kwargs), code)


@pytest.mark.parametrize(
    "trigger,fallback", (("time", None), ("orch", "no_event"), ("webhook", "no_ring"), ("addon", "addon_silent"))
)
def test_firing_windows_and_fallback_matching(trigger, fallback):
    fu = f.FollowupFacts("pending", trigger, 100, 3600, 7200)
    for actual, reason, lower in ((trigger, None, 3300 if trigger == "time" else 100), ("deadline", fallback, 6900)):
        if trigger == "time" and actual == "deadline":
            continue
        for at in (lower, lower + 1, 7200 + 86400):
            assert fire(fu, f.FiredRequest(at, actual, reason, True, HASH)) is None
        for at in (lower - 1, 7200 + 86400 + 1):
            denied(fire(fu, f.FiredRequest(at, actual, reason, True, HASH)), "out_of_window")
        if actual == "deadline":
            for wrong in (None, "unknown", *(r for r in f.FALLBACK_REASONS if r != reason)):
                denied(fire(fu, f.FiredRequest(7200, actual, wrong, True, HASH)), "bad_fallback")
    assert fire() is None  # Missing time deadline defaults to due.


def test_lateness_and_outcomes():
    assert [f.late_by(at, 100) for at in (0, 100, 101)] == [0, 0, 1]
    for outcome in f.OUTCOMES:
        reason = None if outcome == "ran" else "error"
        assert f.check_outcome("fired", outcome, reason) is None
        denied(f.check_outcome("cancelled", outcome, reason), "bad_state")
    for outcome in ("failed", "did_not_run"):
        for reason in f.REASONS:
            assert f.check_outcome("fired", outcome, reason) is None
        for reason in (None, "unknown"):
            denied(f.check_outcome("fired", outcome, reason), "bad_reason")
    denied(f.check_outcome("fired", "unknown", None), "bad_outcome")
    denied(f.check_outcome("fired", "ran", "error"), "bad_reason")
    denied(f.check_outcome("fired", "ran", None, ran_has_reason=True), "bad_reason")
    assert f.check_outcome("pending", "did_not_run", "no_host") is None


def test_derived_cancel_priority_and_terminal_states():
    conditions = (
        ({"replaced": True}, "replaced"),
        ({"member_removed": True}, "member_removed"),
        ({"chain": None}, "grant_revoked"),
        ({"addon_enabled": False}, "addon_disabled"),
        ({"ticket_status": "done"}, "ticket_left_in_progress"),
        ({"claimed_by_other": True}, "claimed_by_other"),
        ({"chain_needs_person": True}, "chain_cap"),
    )
    assert cancel() is None
    for i, (_, reason) in enumerate(conditions):
        kwargs = {}
        for changes, _ in conditions[i:]:
            kwargs.update(changes)
        assert cancel(**kwargs) == reason
    assert cancel(state="needs_person", chain_needs_person=True) is None
    assert cancel(state="needs_person", replaced=True) == "replaced"
    assert cancel(root_person_active=False) == "grant_revoked"
    for s in (*f.OUTCOMES, "cancelled", "fired", "unknown"):
        assert cancel(state=s, replaced=True, member_removed=True, chain=None) is None


@settings(max_examples=100, deadline=None)
@given(
    st.sets(st.sampled_from(("read", "write", "task", "claim"))),
    st.integers(1),
    st.integers(),
    st.integers(1),
    st.one_of(st.sampled_from(f.BROAD_SCOPES), st.text(min_size=1)),
    st.text(min_size=1),
    st.text(),
)
def test_property_no_widening(verbs, duration, fired_at, hours, scope, ticket, person):
    if ticket in f.BROAD_SCOPES:
        ticket += "-t"
    if scope not in f.BROAD_SCOPES:
        ticket = scope  # a ticket-scoped parent reaches only its own ticket
    parent = replace(ROOT, person=person, scope=scope, verbs=tuple(sorted(verbs)), expires_at=duration)
    child = f.derive_grant(
        parent, ticket_uid=ticket, grant_id="child", fired_at=fired_at, secret_hash=HASH, run_hours=hours
    )
    assert f.grant_widening(parent, child) == []
    assert child.scope == ticket and child.verbs == parent.verbs and child.person == person
    assert 0 < child.expires_at - child.issued_at <= f.FU_RUN_HOURS * 3600


@settings(max_examples=100, deadline=None)
@given(
    st.one_of(st.tuples(*(st.integers() for _ in range(4))), st.tuples(*(st.integers(1) for _ in range(4)))),
    st.integers(),
    st.integers(1),
)
def test_property_caps(values, at, excess):
    keys = ("max_days", "chain_max", "pending_max", "runs_per_day")
    config = dict(zip(keys, values, strict=True))
    if any(v < 1 for v in values):
        with pytest.raises(ValueError):
            f.effective_caps(config)
        return
    caps = f.effective_caps(config)
    for key in keys:
        assert 1 <= getattr(caps, key) <= getattr(CAPS, key)
    req = replace(REQ, at=at, due=at + 3600, deadline=at + caps.max_days * 86400 + excess)
    denied(f.check_set(CTX, req, caps), "too_far")
    req = replace(req, deadline=None)
    denied(f.check_set(replace(CTX, pending_in_workspace=caps.pending_max), req, caps), "pending_cap")


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            (
                "set",
                "fired",
                "cancel",
                "derived_cancel",
                "run_now",
                "unknown",
                "outcome:ran",
                "outcome:failed",
                "outcome:did_not_run",
            )
        ),
        max_size=100,
    )
)
def test_property_one_fire(events):
    current, count = "pending", 0
    terminal = (*f.OUTCOMES, "cancelled")
    for event in events:
        following = f.next_state(current, event)
        if current in terminal:
            assert following is None
        if following is not None:
            count += event == "fired"
            current = following
    assert count <= 1
    for s in f.STATES:
        if s == "pending":
            assert fire(replace(FACTS, state=s)) is None
        else:
            denied(fire(replace(FACTS, state=s)), "not_pending")


@settings(max_examples=100, deadline=None)
@given(st.integers(1, 32), st.integers(0, 31))
def test_property_ancestor_revocation(length, index):
    index %= length
    grants = {
        str(i): replace(ROOT, id=str(i), parent=str(i - 1) if i else None, revoked=i == index) for i in range(length)
    }
    for i in range(length):
        chain = f.ancestors(grants, str(i))
        assert f.chain_valid(chain, root_person_active=True) is (i < index)
        assert cancel(chain=chain) == (None if i < index else "grant_revoked")


@settings(max_examples=100, deadline=None)
@given(st.lists(st.integers(), min_size=1, max_size=32))
def test_property_expiry_never_cancels(expiries):
    chain = tuple(
        replace(ROOT, id=str(i), parent=str(i + 1) if i + 1 < len(expiries) else None, expires_at=expiry)
        for i, expiry in enumerate(expiries)
    )
    assert f.chain_valid(chain, root_person_active=True)
    assert cancel(chain=chain) is None
    assert not f.chain_valid(chain, root_person_active=False)
    assert cancel(chain=chain, root_person_active=False) == "grant_revoked"


def reference_chain(events, tasks, caps, now):
    # Independent loop: retain the sets since progress instead of maintaining a counter.
    sets, pending_sets = [], []
    last = None
    done_once = set()
    for event in events:
        if isinstance(event, f.ChainSet):
            sets.append(event.at)
            pending_sets.append(event.at)
        elif sets and event.task in tasks and event.task not in done_once and event.at > sets[0]:
            done_once.add(event.task)
            pending_sets.clear()
            last = event.at
    first = sets[0] if sets else None
    needs = len(pending_sets) >= caps.chain_max or len(sets) >= 10
    if first is not None and now > first + 30 * 86400:
        needs = True
    return f.ChainState(len(sets), len(pending_sets), first, last, needs, "chain_cap" if needs else None)


@settings(max_examples=100, deadline=None)
@given(
    st.lists(st.tuples(st.integers(-10, 3000000), st.sampled_from(("set", "old", "new"))), max_size=60),
    st.integers(-10, 4000000),
    st.integers(1, 5),
)
def test_property_progress_only_original_tasks(raw, now, cap):
    events = [f.ChainSet(at) if kind == "set" else f.ChainDone(at, kind) for at, kind in raw]
    caps = replace(CAPS, chain_max=cap)
    actual = state(events, now=now, caps=caps)
    assert actual == reference_chain(events, frozenset({"old"}), caps, now)
    without_new = [e for e in events if not isinstance(e, f.ChainDone) or e.task != "new"]
    assert actual == state(without_new, now=now, caps=caps)
    assert state([*events, f.ChainDone(now + 1, "new")], now=now, caps=caps) == actual
    if actual.sets_total >= 10 or (actual.first_set_at is not None and now > actual.first_set_at + 30 * 86400):
        assert actual.needs_person


@settings(max_examples=100, deadline=None)
@given(st.text(min_size=1), st.text(min_size=1))
def test_property_ticket_scoped_parent_never_reaches_another_ticket(own, other):
    if own in f.BROAD_SCOPES or other == own:
        return
    with pytest.raises(ValueError):
        f.derive_grant(replace(ROOT, scope=own), ticket_uid=other, grant_id="c", fired_at=0, secret_hash=HASH)


RUNNER = replace(ROOT, verbs="agent")


def test_runner_grant_verbs_string_passes_through():
    child = f.derive_grant(RUNNER, ticket_uid="t", grant_id="c", fired_at=0, secret_hash=HASH)
    assert child.verbs == "agent"
    assert f.grant_widening(RUNNER, child) == []
    # a split string or a list is not the parent's value
    assert f.grant_widening(RUNNER, replace(child, verbs=tuple("agent"))) == ["verbs"]
    assert f.grant_widening(RUNNER, replace(child, verbs="agent2")) == ["verbs"]
    assert f.grant_widening(ROOT, replace(child, verbs="agent")) == ["verbs"]
    for bad in (None, 5, ["a", 1], {"agent"}):
        with pytest.raises(ValueError):
            f.derive_grant(replace(ROOT, verbs=bad), ticket_uid="t", grant_id="c", fired_at=0, secret_hash=HASH)
    assert f.derive_grant(
        replace(ROOT, verbs=["read"]), ticket_uid="t", grant_id="c", fired_at=0, secret_hash=HASH
    ).verbs == ("read",)


@pytest.mark.parametrize("bad", ("", "h", "AB" * 32, "ab" * 31, "zz" * 32, None))
def test_secret_hash_format(bad):
    with pytest.raises(ValueError):
        f.derive_grant(ROOT, ticket_uid="t", grant_id="c", fired_at=0, secret_hash=bad)
    denied(fire(fired=replace(FIRE, secret_hash=bad)), "bad_secret_hash")


def test_replacing_set_at_pending_max_is_allowed():
    full = replace(CTX, pending_in_workspace=CAPS.pending_max)
    denied(f.check_set(full, REQ, CAPS), "pending_cap")
    assert f.check_set(replace(full, replaces_pending=True), REQ, CAPS) is None


def test_person_consistency_and_full_grant_equality():
    child = replace(ROOT, id="child", parent="root", person="someone-else")
    assert not f.chain_valid((child, ROOT), root_person_active=True)
    own = replace(ROOT, id="g", parent="root", scope="t", issued_at=0, expires_at=10)
    assert f.fgrant_usable(own, at=1, outcome_recorded=False, chain=(own, ROOT), root_person_active=True)
    forged = replace(own, scope="other")
    assert not f.fgrant_usable(own, at=1, outcome_recorded=False, chain=(forged, ROOT), root_person_active=True)


def test_strict_cancel_flags():
    assert cancel(addon_enabled="no") == "addon_disabled"
    assert cancel(replaced="yes") == "replaced"
    assert cancel(member_removed=1) == "member_removed"
    assert cancel(claimed_by_other=1) == "claimed_by_other"
    assert cancel(chain_needs_person=1) == "chain_cap"


def test_reopen_loop_counts_once():
    caps = replace(CAPS, chain_max=5)
    ev = [f.ChainSet(10)]
    for i in range(4):  # done, reopen (no event), done again, with a set between
        ev += [f.ChainDone(20 + i, "old"), f.ChainSet(30 + i)]
    s = f.chain_state(ev, open_tasks_at_start=frozenset({"old"}), caps=caps, now=100)
    # only the first done counts: after it came 4 more sets... sets after first done = 1 + 3 = 4
    assert s.without_progress == 4 and s.last_progress_at == 20
    # a task that was not open at chain start never counts
    s = f.chain_state(ev, open_tasks_at_start=frozenset(), caps=caps, now=100)
    assert s.without_progress == 5 and s.last_progress_at is None


@settings(max_examples=100, deadline=None)
@given(st.lists(st.tuples(st.integers(1, 1000), st.sampled_from(("set", "old", "old2", "new"))), max_size=60))
def test_property_each_start_task_credits_once(raw):
    events = [f.ChainSet(at) if k == "set" else f.ChainDone(at, k) for at, k in raw]
    s = f.chain_state(events, open_tasks_at_start=frozenset({"old", "old2"}), caps=CAPS, now=1000)
    # never more than two progress resets, so at least (sets - sets-before-the-last-credit) remain
    first = next((e.at for e in events if isinstance(e, f.ChainSet)), None)
    credited, last = set(), None
    seen_set = False
    for i, e in enumerate(events):
        seen_set = seen_set or isinstance(e, f.ChainSet)
        if (
            seen_set
            and isinstance(e, f.ChainDone)
            and e.task in ("old", "old2")
            and e.task not in credited
            and first is not None
            and e.at > first
        ):
            credited.add(e.task)
            last = i
    expected = sum(isinstance(e, f.ChainSet) for e in events[(last + 1 if last is not None else 0) :])
    assert s.without_progress == expected

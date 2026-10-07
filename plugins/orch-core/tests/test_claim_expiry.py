"""#217: a claim expires `claims.ttl_hours` after its holder's last sign of life (not after `claim.at`), never while
the ticket is waiting, and the refusal says whose claim it was and why."""
from datetime import timedelta

import pytest

from orch.clock import now as real_now
from orch.core import check as check_mod, query, store
from orch.core.events import read_events
from orch.core.ops import Ops
from orch.errors import ClaimError


def _later(monkeypatch, hours):
    """Move every clock the claim rules read forward."""
    monkeypatch.setattr(query, "clock_now", lambda: real_now() + timedelta(hours=hours))


def _age_claim(ws, tid, at="2020-01-01T00:00Z"):
    path, t = store.load(ws, tid)
    t.meta["claim"]["at"] = at
    store.save(ws, t, path)


def test_own_expired_claim_is_named_and_says_nobody_else_holds_it(ws, aops, working, monkeypatch, plan_approved):
    aops.task_add(working, [{"text": "a"}])
    plan_approved(working)
    _later(monkeypatch, 5)                      # ttl is 4 h and the last event is older than that
    with pytest.raises(ClaimError) as e:
        aops.task_start(working, "T1")
    msg = str(e.value)
    assert "your claim" in msg and "expired after 4 h" in msg and "nobody else holds it" in msg
    assert f"orch claim {working}" in msg and "another session" not in msg


def test_claim_of_another_live_session_is_named(ws, aops, other_agent, working, plan_approved):
    aops.task_add(working, [{"text": "a"}])
    plan_approved(working)
    with pytest.raises(ClaimError) as e:
        Ops(ws, other_agent).task_start(working, "T1")
    assert "held by" in str(e.value) and "claude-code" in str(e.value) and "7f3c9a21" in str(e.value)


def test_activity_keeps_a_claim_alive_past_its_age(ws, aops, working, plan_approved):
    aops.task_add(working, [{"text": "a"}])
    plan_approved(working)
    _age_claim(ws, working)                     # claim.at is years old, but the agent just wrote events
    aops.task_start(working, "T1")              # not refused


def test_waiting_ticket_never_expires(ws, aops, working, monkeypatch):
    aops.ask(working, [{"text": "Go?", "type": "confirm", "blocking": True}])
    assert store.resolve(ws, working).status == "waiting"
    _age_claim(ws, working)
    _later(monkeypatch, 100)
    aops.task_add(working, [{"text": "while waiting"}])   # still the holder's


def test_dead_session_claim_expires_and_another_session_takes_over(ws, aops, other_agent, working, monkeypatch):
    _later(monkeypatch, 5)
    t = Ops(ws, other_agent).claim(working)
    assert t.meta["claim"]["harness"] == "copilot"


def test_takeover_refused_while_the_holder_is_active(ws, aops, other_agent, working):
    _age_claim(ws, working)                     # old claim.at, fresh events: still alive
    with pytest.raises(ClaimError):
        Ops(ws, other_agent).claim(working)


def test_check_and_held_ticket_use_the_same_rule(ws, aops, working, monkeypatch):
    from orch.core import quick
    events = read_events(ws, working)
    claim = store.load(ws, working)[1].meta["claim"]
    assert not query.claim_is_expired(ws, working, "in-progress", claim, events)
    assert quick._held_ticket(ws, "7f3c9a21-0000") == working
    _later(monkeypatch, 5)
    assert query.claim_is_expired(ws, working, "in-progress", claim, events)
    assert not query.claim_is_expired(ws, working, "waiting", claim, events)
    assert quick._held_ticket(ws, "7f3c9a21-0000") is None
    codes = [f.code for f in check_mod.run_checks(ws)]
    assert "claim-expired" in codes


def _shift(monkeypatch, hours):
    """Every clock reads `hours` later from now on, so events written afterwards are dated in that future."""
    fake = lambda: real_now() + timedelta(hours=hours)  # noqa: E731
    monkeypatch.setattr("orch.clock.now", fake)
    monkeypatch.setattr(query, "clock_now", fake)


def _expired(ws, tid):
    t = store.load(ws, tid)[1]
    return query.claim_is_expired(ws, tid, t.status, t.meta["claim"])


def test_human_events_do_not_extend_a_claim(ws, aops, hops, working, monkeypatch):
    aops.ask(working, [{"text": "Which?", "type": "text", "blocking": False}])
    _shift(monkeypatch, 5)
    hops.answer(working, "Q1", "this one")             # the human keeps answering; the agent is silent
    assert read_events(ws, working)[-1].actor.startswith("human:")
    assert _expired(ws, working)


def test_the_claiming_sessions_own_events_extend_it(ws, aops, working, monkeypatch):
    _shift(monkeypatch, 5)
    assert _expired(ws, working)
    aops.log(working, "still here")
    assert not _expired(ws, working)


def test_another_sessions_events_do_not_extend_it(ws, aops, other_agent, working, monkeypatch):
    _shift(monkeypatch, 5)
    Ops(ws, other_agent).log(working, "someone else")
    assert read_events(ws, working)[-1].actor.startswith("agent:copilot")
    assert _expired(ws, working)


def test_waiting_still_never_expires_even_without_own_events(ws, aops, working, monkeypatch):
    aops.ask(working, [{"text": "Go?", "type": "confirm", "blocking": True}])
    _shift(monkeypatch, 100)
    assert not _expired(ws, working)


def test_check_does_not_flag_old_claim_with_recent_activity(ws, aops, working):
    _age_claim(ws, working)
    assert "claim-expired" not in [f.code for f in check_mod.run_checks(ws)]

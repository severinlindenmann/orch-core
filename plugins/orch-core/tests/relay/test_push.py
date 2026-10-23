"""H13a: the host's push planner, sealing, dispatch and send classes (orch-v2-host 11A.16.4) against a fake relay."""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from orch import canon
from orch.relay import keys as relay_keys
from orch.relay import outbound
from orch.relay.push import DEVICE_BUDGET, ID_INTERVAL_MS, MAX_RESEALS, PLAN_EVERY_S
from orch.relay.session import Fenced
from tests.store.helpers import WS

from .pushkit import Kit, is_transport_error, refused


@pytest.fixture
def kit(tmp_path):
    k = Kit(tmp_path)
    k.advance(10)  # the fence is at construction; events below are after it
    yield k
    k.close()


def intents(k: Kit, state: str | None = None):
    rows = k.ob._db.execute("SELECT key FROM push_intent ORDER BY n").fetchall()  # noqa: SLF001
    out = [k.ob.get(r[0]) for r in rows]
    return [i for i in out if state is None or i.state == state]


# -- the planner ------------------------------------------------------------------------------------------------------


def test_a_question_becomes_one_intent_with_its_deadline_and_addressees(kit):
    qid = kit.ask(text="deploy   now?")
    assert kit.plan() == 1
    (it,) = intents(kit)
    assert (it.kind, it.id, it.state, it.priority) == ("question", qid, "planned", "high")
    assert it.label == "deploy now?" and it.ticket == kit.uid
    assert it.narrow == {kit.env.owner.ref} and it.exclude == frozenset()
    at = kit.env.read_events(kit.uid)[-1]["at"]
    from orch.relay.push import _at_s  # noqa: PLC0415, SLF001

    assert it.deadline_ms == _at_s(at) * 1000 + 86_400_000
    assert kit.plan() == 0  # a second pass finds the same key
    assert len(intents(kit)) == 1


def test_the_cursor_and_the_intents_advance_in_one_transaction(kit):
    kit.ask()
    kit.plan()
    seq, head = kit.ob.cursor(kit.uid)
    assert seq == kit.store.head_seq(kit.uid) and head == kit.store.log_head(kit.uid)


def test_the_start_fence_pushes_no_history(tmp_path):
    k = Kit(tmp_path, fence_after=3600)  # the outbox starts an hour after the question
    try:
        k.ask()
        k.advance(3601)
        assert k.plan() == 0 and intents(k) == []
        k.ask("Q2", "later?")
        assert k.plan() == 1 and intents(k)[0].label == "later?"
    finally:
        k.close()


def test_an_answer_is_a_closed_intent_narrowed_like_the_question(kit):
    qid = kit.ask()
    kit.plan()
    kit.advance(1)
    kit.answer()
    kit.plan()
    q, closed = intents(kit)
    assert (q.state, q.reason) == ("merged", "answered_before_seal")  # nothing was sealed: nothing is shown
    assert (closed.kind, closed.id, closed.label, closed.state) == ("question.closed", qid, "", "merged")
    assert closed.exclude == {kit.env.owner.device[2:]}  # the answering device already knows


def test_a_closed_push_after_a_sealed_question_goes_out(kit):
    kit.ask()
    kit.plan()
    kit.thread.step()
    assert [i.state for i in intents(kit)] == ["sent"]
    kit.advance(1)
    kit.answer()
    kit.plan()
    assert [(i.kind, i.state) for i in intents(kit)] == [("question", "sent"), ("question.closed", "planned")]


def test_a_newer_question_for_the_same_id_replaces_an_unsent_one(kit):
    kit.ask("Q1", "first?")
    kit.plan()
    kit.advance(1)
    kit.ask("Q1", "second?")  # a re-ask with other text: another hash, the same qid
    kit.plan()
    old, new = intents(kit)
    assert (old.state, old.reason, new.state, new.label) == ("merged", "newer_for_id", "planned", "second?")


def test_an_abandoned_branch_is_invalidated_and_the_replacement_planned(kit):
    kit.ask()
    kit.plan()
    kit.thread.step()  # the question is sent, so the answer's intent is not merged away
    kit.advance(1)
    kit.answer("Q1", "yes")
    kit.plan()
    old = intents(kit)[-1]
    assert (old.kind, old.state) == ("question.closed", "planned")
    kit.rollback(old.seq - 1)  # a restore cut the answer: another event now stands at that seq
    kit.advance(1)
    kit.answer("Q1", "no")
    kit.plan()
    closed = [i for i in intents(kit) if i.kind == "question.closed"]
    assert [(i.key, i.state, i.reason) for i in closed[:1]] == [(old.key, "invalidated", "abandoned")]
    assert closed[1].seq == old.seq + 1 and closed[1].state == "planned" and closed[1].event_hash != old.event_hash


def test_a_sent_intent_of_an_abandoned_branch_stays_sent(kit):
    ref, _ = kit.env.add_device()  # the answering device is left out of its own closed push: this one gets it
    kit.view.add(kit.env.owner.ref, ref)
    kit.ask()
    kit.plan()
    kit.thread.step()
    kit.advance(1)
    kit.answer("Q1", "yes")
    kit.plan()
    kit.thread.step()
    old = intents(kit)[-1]
    assert (old.kind, old.state) == ("question.closed", "sent")
    kit.rollback(old.seq - 1)
    kit.advance(1)
    kit.answer("Q1", "no")
    kit.plan()
    assert [i.state for i in intents(kit)] == ["sent", "sent", "planned"]  # a push cannot be recalled


# -- sealing ----------------------------------------------------------------------------------------------------------


def test_sealing_persists_the_outer_before_any_send_and_the_phone_opens_it(kit):
    kit.ask("Q1", "deploy now?")
    kit.plan()
    sent_before = len(kit.session.calls)
    from orch.relay.push import seal_step

    assert seal_step(kit.thread, kit.now) == 1
    assert len(kit.session.calls) == sent_before == 0  # sealed, not sent
    (it,) = intents(kit)
    assert it.state == "sealed" and it.epoch == 1 and it.targets == (kit.env.owner.device[2:],)
    payload = kit.open(it.outer)
    assert payload["kind"] == "question" and payload["label"].rstrip() == "deploy now?" and len(payload["label"]) == 80
    assert payload["ts_ms"] == it.ts_ms and len(it.outer) <= 3072


def test_ts_ms_rises_per_id(kit):
    kit.ask("Q1", "a?")
    kit.plan()
    from orch.relay.push import seal_step

    seal_step(kit.thread, kit.now)
    (a,) = intents(kit)
    kit.ask("Q1", "b?")  # same qid again, within the same millisecond clock
    kit.plan()
    seal_step(kit.thread, kit.now)
    b = intents(kit, "sealed")[-1]
    assert b.key != a.key and b.ts_ms == a.ts_ms + 1


def test_at_most_32_are_sealed_per_job(kit):
    for i in range(34):
        kit.ask(f"Q{i + 1}", f"q{i}?")
    kit.plan()
    from orch.relay.push import seal_step

    assert seal_step(kit.thread, kit.now) == 32
    assert seal_step(kit.thread, kit.now) == 2


def test_a_gated_snapshot_leaves_intents_planned(kit):
    kit.ask()
    kit.plan()
    kit.epochs.gated = True
    from orch.relay.push import seal_step

    assert seal_step(kit.thread, kit.now) == 0
    assert [i.state for i in intents(kit)] == ["planned"]
    kit.epochs.gated = False
    assert seal_step(kit.thread, kit.now) == 1


def test_no_target_and_deadline_drop_before_sealing(kit):
    kit.view.members.clear()
    kit.ask()
    kit.plan()
    from orch.relay.push import seal_step

    seal_step(kit.thread, kit.now)
    assert [(i.state, i.reason) for i in intents(kit)] == [("dropped", "no_target")]
    kit.view.add(kit.env.owner.ref, kit.env.owner.device)
    kit.ask("Q2", "late?")
    kit.plan()
    kit.now += 90_000_000  # past 24 h
    seal_step(kit.thread, kit.now)
    assert intents(kit)[-1].reason == "deadline"


def test_the_sealer_is_built_only_by_push_sealing_and_refuses_a_bad_payload(kit):
    with pytest.raises(TypeError):
        relay_keys._PushSealer(object(), kit.keys, kit.signer, kit.epochs.seal_state(), b"\0" * 16, lambda e: b"")  # noqa: SLF001
    root = Path(relay_keys.__file__).parent
    builders = []
    for path in sorted(root.rglob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_PushSealer":
                        builders.append((path.name, fn.name))
    assert builders == [("keys.py", "push_sealing")]
    with kit.keys.push_sealing(kit.signer) as sealer:
        for bad in ({"kind": "join", "id": "x", "label": "l", "ts_ms": 1},
                    {"kind": "question", "id": "x", "label": "l"}):  # fmt: skip
            with pytest.raises(Exception):  # noqa: B017, PT011 - PushError, whatever its class
                sealer.seal_push(bad)


def test_the_signer_signs_a_push_only_for_a_held_epoch_a_host_kind_and_a_padded_label(kit):
    from orch.crypto import push as cpush
    from orch.wsproto.sign import SignerRefused

    good = cpush.push_payload("question", "qid", "hello", 5)
    assert len(kit.signer.push(1, good)) == 64
    for bad_epoch in (0, 2, 9):
        with pytest.raises(SignerRefused):
            kit.signer.push(bad_epoch, good)
    with pytest.raises(SignerRefused):
        kit.signer.push(1, {**good, "label": "hello"})  # not padded
    with pytest.raises(SignerRefused):
        kit.signer.push(1, {**good, "kind": "join"})  # never sent by the host (D2)
    with pytest.raises(SignerRefused):
        kit.signer.push(1, {"kind": "question", "id": "x"})


# -- the send classes -------------------------------------------------------------------------------------------------


def sealed_one(k: Kit):
    k.ask()
    k.plan()
    from orch.relay.push import seal_step

    seal_step(k.thread, k.now)
    return intents(k, "sealed")[0]


def test_a_send_posts_the_persisted_outer_with_the_holder(kit):
    it = sealed_one(kit)
    kit.thread.step()
    (call,) = kit.session.calls
    assert call["method"] == "POST" and call["path"] == f"/v2/workspaces/{WS}/push" and call["holder"] is True
    body = call["body"]
    assert set(body) == {"targets", "outer", "priority", "ttl_s"} and body["priority"] == "high"
    assert canon.dumps(body["outer"]) == it.outer and body["targets"] == [kit.env.owner.device[2:]]
    assert 0 < body["ttl_s"] <= 86_400
    done = kit.ob.get(it.key)
    assert (done.state, done.accepted) == ("sent", 1)
    assert it.label and done.label == ""  # a final intent keeps no label text (user content)


def test_a_transport_error_resends_the_same_bytes_with_backoff(kit):
    it = sealed_one(kit)
    kit.session.script = [is_transport_error(), refused(503, "overloaded", 5), None]
    waits = []
    for _ in range(3):
        kit.thread.step()
        cur = kit.ob.get(it.key)
        waits.append(cur.next_ms - kit.now if cur.state == "sealed" else None)
        kit.now += 60_000
    assert waits[0] == 1000 and waits[1] == 5000  # 1 s, then Retry-After as the floor
    bodies = [canon.dumps(c["body"]["outer"]) for c in kit.session.calls]
    assert len(bodies) == 3 and set(bodies) == {it.outer}  # never resealed
    assert kit.ob.get(it.key).state == "sent"


def test_stale_epoch_reseals_three_times_then_push_epoch_loop(kit):
    it = sealed_one(kit)
    kit.session.script = [refused(409, "stale_epoch")]
    outers = {it.outer}
    for n in range(1, MAX_RESEALS + 2):
        kit.thread.step()
        cur = kit.ob.get(it.key)
        if n <= MAX_RESEALS:
            assert cur.reseals == n
        outers |= {cur.outer} if cur.outer else set()
    final = kit.ob.get(it.key)
    assert (final.state, final.reason) == ("failed", "push_epoch_loop") and "push_epoch_loop" in kit.alarms.codes()
    sent = [canon.dumps(c["body"]["outer"]) for c in kit.session.calls]
    assert len(set(sent)) == len(sent) == MAX_RESEALS + 1  # every reseal is a new outer
    assert final.deadline_ms == it.deadline_ms  # the deadline never moves


def test_a_stale_epoch_followed_by_success_sends_the_new_epoch(kit):
    it = sealed_one(kit)
    kit.epochs.current = 2
    kit.session.script = [refused(409, "stale_epoch"), None]
    kit.thread.step()
    kit.thread.step()
    done = kit.ob.get(it.key)
    assert done.state == "sent" and done.epoch == 2 and kit.open(done.outer, 2)["id"] == it.id


def test_404_delays_intents_and_probes_after_60s_then_backs_off(kit):
    it = sealed_one(kit)
    kit.session.script = [refused(404, "not_found"), refused(404, "not_found"), None]
    kit.thread.step()
    assert kit.thread.state == "off_at_relay"
    held = kit.ob.get(it.key)
    assert held.state == "sealed" and held.label != "" and held.next_ms == kit.now + 60_000  # kept, not dropped
    kit.advance(1)
    kit.ask("Q2", "next?")
    kit.plan()
    kit.thread.step()
    assert len(kit.session.calls) == 1  # no probe inside the first 60 s; the new intent is sealed and waits
    kit.advance(60)
    kit.thread.step()
    assert len(kit.session.calls) == 2 and kit.thread.state == "off_at_relay"  # probe 2: 404 again
    assert kit.thread._off_until_ms == kit.now + 120_000  # noqa: SLF001 - backed off
    kit.advance(121)
    kit.thread.step()
    kit.thread.step()
    assert kit.thread.state == "on" and kit.ob.get(it.key).state == "sent"  # delayed, then delivered within the TTL


def test_404_probe_interval_caps_at_an_hour(kit):
    sealed_one(kit)
    kit.session.script = [refused(404, "not_found")]
    for _ in range(12):
        kit.advance(3601)
        kit.thread.step()
    assert kit.thread._probe_ms == 3_600_000  # noqa: SLF001


def test_a_relink_reprobes_at_once(kit):
    it = sealed_one(kit)
    kit.session.script = [refused(404, "not_found"), None]
    kit.thread.step()
    assert len(kit.session.calls) == 1 and kit.thread.state == "off_at_relay"
    kit.session.gen = 2  # a new link (take-over)
    kit.thread.step()
    assert kit.thread.state == "on" and kit.ob.get(it.key).state == "sent" and len(kit.session.calls) == 2


def test_a_fresh_thread_probes_at_once(kit):
    assert kit.thread._off_until_ms == 0  # noqa: SLF001 - a worker (re)start begins with push on


def test_fenced_stops_the_thread_and_keeps_the_intent(kit):
    it = sealed_one(kit)
    kit.session.script = [Fenced("fenced")]
    with pytest.raises(Fenced):
        kit.thread.step()
    assert kit.ob.get(it.key).state == "sealed"


def test_malformed_fails_the_intent_and_alarms(kit):
    it = sealed_one(kit)
    kit.session.script = [refused(400, "malformed")]
    kit.thread.step()
    assert (kit.ob.get(it.key).state, "push_malformed" in kit.alarms.codes()) == ("failed", True)
    assert kit.ob.get(it.key).label == ""
    kit.thread.step()
    assert len(kit.session.calls) == 1  # never sent again


def test_a_deadline_passed_intent_is_dropped_unsent(kit):
    it = sealed_one(kit)
    kit.now = it.deadline_ms + 1
    kit.thread.step()
    assert not kit.session.calls and kit.ob.get(it.key).reason == "deadline"


# -- the host budgets -------------------------------------------------------------------------------------------------


def test_one_push_per_id_per_minute(kit):
    kit.ask("Q1", "a?")
    kit.plan()
    kit.thread.step()
    kit.advance(1)
    kit.ask("Q1", "b?")
    kit.plan()
    kit.thread.step()
    assert len(kit.session.calls) == 1  # the second waits for the minute
    kit.now += ID_INTERVAL_MS
    kit.thread.step()
    assert len(kit.session.calls) == 2


def test_a_device_over_budget_is_left_out(kit):
    for i in range(DEVICE_BUDGET):
        kit.ask(f"Q{i + 1}", f"q{i}?")
    kit.plan()
    kit.thread.step()
    assert len(kit.session.calls) == DEVICE_BUDGET
    kit.ask("Q9", "one too many?")
    kit.plan()
    kit.thread.step()
    assert len(kit.session.calls) == DEVICE_BUDGET and intents(kit)[-1].reason == "over_budget"
    kit.now += 600_001
    kit.ask("Q10", "fresh window?")
    kit.thread.step()
    assert len(kit.session.calls) == DEVICE_BUDGET + 1


# -- the audience -----------------------------------------------------------------------------------------------------


def test_the_audience_is_the_intersection(kit):
    from orch.relay.audience import PushContext, devices_for
    from orch.store.store import device_standing_ok

    env, state = kit.env, kit.store.state
    me = env.owner.device[2:]
    ctx = lambda **kw: devices_for(state, kit.view.member_list(), PushContext(**kw), standing=device_standing_ok)  # noqa: E731
    assert ctx(ticket=kit.uid) == [me]
    assert ctx(ticket=kit.uid, exclude=frozenset({me})) == []
    assert ctx(ticket=kit.uid, narrow=frozenset({"p_" + "0" * 32})) == []
    assert ctx(ticket="no-such-ticket") == []
    assert devices_for(state, None, PushContext(), standing=device_standing_ok) == []
    kit.view.add(env.owner.ref, env.owner.device, scopes=("decide",))  # no look in the list entry
    assert ctx(ticket=kit.uid) == []
    kit.view.add(env.owner.ref, env.owner.device, cert_scopes=("decide",))  # no look in the certificate
    assert ctx(ticket=kit.uid) == []
    kit.view.add(env.owner.ref, env.owner.device)
    ref, _ = env.add_device()
    kit.view.add(env.owner.ref, ref)
    both = devices_for(kit.store.state, kit.view.member_list(), PushContext(), standing=device_standing_ok)
    assert both == sorted([me, ref[2:]])
    env.revoke_device(ref)  # standing lost: the device leaves the audience at the next dispatch
    assert devices_for(kit.store.state, kit.view.member_list(), PushContext(), standing=device_standing_ok) == [me]


def test_a_device_that_lost_standing_after_sealing_is_narrowed_out_at_send(kit):
    ref, _ = kit.env.add_device()
    kit.view.add(kit.env.owner.ref, ref)
    kit.ask()
    kit.plan()
    it = sealed_one_from_planned(kit)
    assert set(it.targets) == {kit.env.owner.device[2:], ref[2:]}
    kit.env.revoke_device(ref)
    kit.thread.step()
    assert kit.session.calls[0]["body"]["targets"] == [kit.env.owner.device[2:]]
    assert set(kit.ob.get(it.key).targets) == {kit.env.owner.device[2:]}


def sealed_one_from_planned(k: Kit):
    from orch.relay.push import seal_step

    seal_step(k.thread, k.now)
    return intents(k, "sealed")[0]


def test_auth_gen_is_raised_only_by_the_state_setter():
    import orch.store.store as store_mod

    tree = ast.parse(Path(store_mod.__file__).read_text())
    where = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, (ast.AugAssign, ast.Assign)):
                    tgts = [node.target] if isinstance(node, ast.AugAssign) else node.targets
                    if any(isinstance(t, ast.Attribute) and t.attr == "auth_gen" for t in tgts):
                        where.append(fn.name)
    assert sorted(where) == ["__init__", "_state"]


def test_the_state_setter_raises_auth_gen(kit):
    g = kit.store.auth_gen
    kit.ask()
    assert kit.store.auth_gen > g


# -- restore and the outbox -------------------------------------------------------------------------------------------


def test_another_instance_id_clears_the_outbox_and_restarts_the_fence(kit, tmp_path):
    kit.ask()
    kit.plan()
    kit.ob.close()
    from orch.relay.outbound import OutboundStore

    ob = OutboundStore(kit.state_dir, instance_id="j" * 32, now_ms=kit.now + 5000)
    try:
        assert ob.restored and ob.start_ms == kit.now + 5000 and ob.cursor(kit.uid) is None and ob.counts() == {}
    finally:
        ob.close()
    kit.ob = OutboundStore(kit.state_dir, instance_id="j" * 32, now_ms=kit.now + 5000)


def test_the_intent_key_covers_log_seq_hash_kind_and_id():
    k = outbound.intent_key
    base = k("l", 1, "h", "question", "i")
    assert base == k("l", 1, "h", "question", "i")
    others = [k("l", 2, "h", "question", "i"), k("l", 1, "g", "question", "i"), k("l", 1, "h", "question.closed", "i"),
              k("l", 1, "h", "question", "j"), k("l1", 1, "h", "question", "i")]  # fmt: skip
    assert len({base, *others}) == 6


# -- the thread -------------------------------------------------------------------------------------------------------


def test_the_thread_plans_seals_and_sends_on_a_wake(kit):
    import time

    kit.thread.start()
    kit.ask()
    kit.thread.wake()
    deadline = time.time() + 10
    while not kit.session.calls and time.time() < deadline:
        time.sleep(0.02)
    kit.thread.stop()
    assert kit.thread.join(5) and kit.session.calls and kit.session.calls[0]["holder"] is True
    assert PLAN_EVERY_S == 30.0


# -- the fix round ----------------------------------------------------------------------------------------------------


def test_a_non_session_error_alarms_and_retries_without_stopping_the_thread(kit):
    import time

    calls = []
    real = kit.thread.planner.run

    def flaky(now_ms):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(now_ms)

    kit.thread.planner.run = flaky
    stopped = []
    kit.thread._on_error = stopped.append  # noqa: SLF001
    kit.thread.start()
    kit.ask()
    kit.thread.wake()
    deadline = time.time() + 15
    while not kit.session.calls and time.time() < deadline:
        time.sleep(0.02)
        kit.thread.wake()
    kit.thread.stop()
    assert kit.thread.join(5)
    assert "push_error" in kit.alarms.codes() and not stopped and kit.session.calls  # retried, then sent


def _drive(kit, exc_factory, until, secs=30):
    import time

    kit.thread.step = lambda: (_ for _ in ()).throw(exc_factory())  # every pass fails
    stopped = []
    kit.thread._on_error = stopped.append  # noqa: SLF001
    kit.thread.start()
    deadline = time.time() + secs
    while not until() and time.time() < deadline:
        time.sleep(0.02)
        kit.thread.wake()
    return stopped


def test_a_custody_refusal_stops_the_push_thread_without_retry(kit):
    from orch.relay.keys import SealRefused

    stopped = _drive(kit, lambda: SealRefused("gate up"), lambda: ("push_stopped", "SealRefused") in kit.alarms)
    assert kit.thread.join(10)  # the thread ended; no push_error, no retry, the worker's on_error was not called
    assert "push_error" not in kit.alarms.codes() and not stopped and kit.thread.state == "stopped"


def test_a_state_error_also_stops_the_push_thread(kit):
    from orch.wsproto.state import StateError

    _drive(kit, lambda: StateError("custody"), lambda: ("push_stopped", "StateError") in kit.alarms)
    assert kit.thread.join(10) and "push_error" not in kit.alarms.codes()


def test_a_transient_error_alarms_push_error_and_never_stops_the_thread(kit):
    stopped = _drive(kit, lambda: sqlite3.OperationalError("locked"), lambda: "push_error" in kit.alarms.codes())
    alive = kit.thread._thread.is_alive()  # noqa: SLF001
    kit.thread.stop()
    kit.thread.join(5)
    assert "push_error" in kit.alarms.codes() and "push_stopped" not in kit.alarms.codes() and alive and not stopped


def test_an_unknown_error_retries_then_stops_after_ten_consecutive_failures(kit, monkeypatch):
    from orch.relay import push

    monkeypatch.setattr(push, "BACKOFF_MAX_S", 0.0)
    _drive(kit, lambda: RuntimeError("odd"), lambda: ("push_stopped", "RuntimeError") in kit.alarms)
    assert kit.thread.join(10)
    assert kit.alarms.codes().count("push_error") == push.UNKNOWN_FAILURES_MAX - 1


def test_a_session_error_stops_the_thread(kit):
    import time

    from orch.relay.session import SessionError

    kit.thread.planner.run = lambda now_ms: (_ for _ in ()).throw(SessionError("auth"))
    stopped = []
    kit.thread._on_error = stopped.append  # noqa: SLF001
    kit.thread.start()
    kit.thread.wake()
    deadline = time.time() + 10
    while not stopped and time.time() < deadline:
        time.sleep(0.02)
    assert len(stopped) == 1 and isinstance(stopped[0], SessionError)
    kit.thread.join(5)


def test_an_unreadable_log_is_retried_not_treated_as_rewritten(kit):
    kit.ask()
    kit.plan()
    (it,) = intents(kit)
    kit.advance(1)
    kit.ask("Q2", "more?")
    store = kit.store
    real_head, real_seq = store.head_at, store.log_seq
    store.head_at = lambda log, seq: None  # a transient read error
    store.log_seq = lambda log: None
    try:
        kit.plan()
        assert kit.ob.get(it.key).state == "planned" and len(intents(kit)) == 1  # nothing invalidated or planned
    finally:
        store.head_at, store.log_seq = real_head, real_seq
    kit.plan()
    assert len(intents(kit)) == 2 and kit.ob.get(it.key).state in ("planned", "merged")


def test_the_closed_push_is_exempt_from_the_id_interval_and_the_budget(kit):
    ref, _ = kit.env.add_device()
    kit.view.add(kit.env.owner.ref, ref)
    kit.ask()
    kit.plan()
    kit.thread.step()
    assert len(kit.session.calls) == 1
    kit.advance(1)
    kit.answer("Q1", "yes")
    kit.plan()
    kit.thread.step()  # no ID_INTERVAL_MS wait: the stale "New question" is replaced promptly
    assert len(kit.session.calls) == 2 and kit.session.calls[1]["body"]["priority"] == "high"


def test_the_id_interval_applies_between_pushes_of_the_same_kind_only(kit):
    from orch.relay import push as rp

    ref, _ = kit.env.add_device()
    kit.view.add(kit.env.owner.ref, ref)
    kit.ask()
    kit.plan()
    kit.thread.step()
    kit.advance(1)
    kit.ask("Q1", "again?")  # same kind, same id: waits
    kit.plan()
    kit.thread.step()
    assert len(kit.session.calls) == 1
    key = intents(kit)[-1].key
    kit.now += rp.ID_INTERVAL_MS
    kit.thread.step()
    assert kit.ob.get(key).state == "sent"


def test_the_closed_push_does_not_use_the_device_budget(kit):
    ref, _ = kit.env.add_device()
    kit.view.add(kit.env.owner.ref, ref)
    for i in range(DEVICE_BUDGET):
        kit.ask(f"Q{i + 1}", f"q{i}?")
        kit.plan()
        kit.thread.step()
        kit.advance(1)
    assert len(kit.session.calls) == DEVICE_BUDGET
    kit.answer("Q1", "yes")
    kit.plan()
    kit.thread.step()
    assert len(kit.session.calls) == DEVICE_BUDGET + 1  # the closed one is not over budget


def test_seal_step_loops_past_an_all_dropped_batch(kit):
    from orch.relay.push import SEAL_BATCH

    for i in range(SEAL_BATCH):
        kit.ask(f"Q{i + 1}", f"d{i}?")
    kit.plan()
    kit.view.members.clear()  # no audience: the first batch is dropped whole
    drops = [i for i in intents(kit, "planned")]
    assert len(drops) == SEAL_BATCH
    kit.view.add(kit.env.owner.ref, kit.env.owner.device)
    from orch.relay.push import seal_step

    # make the first 32 undeliverable by deadline, then add a good one behind them
    for it in drops:
        kit.ob._db.execute("UPDATE push_intent SET deadline_ms=? WHERE key=?", (kit.now - 1, it.key))  # noqa: SLF001
    kit.advance(1)
    kit.ask("Q99", "good?")
    kit.plan()
    kit.thread.step()
    states = [i.state for i in intents(kit)]
    assert states.count("dropped") == SEAL_BATCH and states[-1] in ("sealed", "sent") and not kit.ob.planned()
    assert seal_step(kit.thread, kit.now) == 0


def test_a_newer_outbox_schema_is_refused_with_a_clear_error(tmp_path):
    from orch.wsproto.state import StateDir

    sd = StateDir(tmp_path / "st")
    ob = outbound.OutboundStore(sd, instance_id="i" * 32, now_ms=1)
    assert ob._db.execute("PRAGMA user_version").fetchone()[0] == outbound.SCHEMA_VERSION  # noqa: SLF001
    ob._db.execute(f"PRAGMA user_version={outbound.SCHEMA_VERSION + 1}")  # noqa: SLF001
    ob.close()
    with pytest.raises(outbound.OutboxVersionError, match="outbound_schema_newer"):
        outbound.OutboundStore(sd, instance_id="i" * 32, now_ms=1)
    sd.close()

"""Lazy replay: the workspace log is always replayed, a ticket log when touched (with the tickets it refers to)."""

from __future__ import annotations

import multiprocessing as mp
import time

import pytest

from orch import canon
from orch.store import StoreError
from tests.store import workers
from tests.store.helpers import WS, ts


@pytest.fixture
def trio(env):
    s = env.bootstrap()
    uids = [env.new_ticket(f"t{i}") for i in range(3)]
    for u in uids:
        env.log(u)
    s.close()
    return env, uids


def test_a_lazy_open_replays_the_workspace_and_no_ticket(trio):
    env, uids = trio
    s = env.open(load="lazy")
    assert s._loaded == set() and s.state.tickets == {} and s.state.workspace.members
    assert s.normalise_ref("2") == "DEMO-0002" and s._loaded == set()  # text only: nothing is replayed
    assert s.uid_of("2") == uids[1] and s._loaded == {uids[1]}  # a hint names it, the replay confirms it
    v = s.ticket("DEMO-0002")
    assert v.uid == uids[1] and s._loaded == {uids[1]} and set(s.state.tickets) == {uids[1]}
    assert s.ticket("nope") is None
    s.load_all()
    assert set(s.state.tickets) == set(uids) and s.chain_errors() == []


def test_a_broken_ticket_is_found_when_it_is_touched_and_does_not_hurt_the_others(trio):
    env, uids = trio
    p = env.path(uids[1], "events.jsonl")
    e = canon.parse_event_line(p.read_bytes().splitlines(keepends=True)[1])
    e["text"] = "forged"
    p.write_bytes(p.read_bytes().splitlines(keepends=True)[0] + canon.event_line(e))
    s = env.open(load="lazy")
    assert s.chain_errors() == []  # not looked at yet
    s.ticket(uids[0])
    assert s.chain_errors() == []
    s.ticket(uids[1])
    assert [c[0] for c in s.chain_errors()] == [uids[1]]
    with pytest.raises(StoreError) as ex:
        s.append({"type": "log.added", "actor": env.agent, "text": "x"}, log=uids[1])
    assert ex.value.code in ("chain.broken", "chain.diverged")  # the host checkpoint notices the rewrite too
    s.append({"type": "log.added", "actor": env.agent, "text": "x"}, log=uids[0])


def test_a_ticket_is_replayed_with_the_tickets_it_refers_to(trio):
    env, uids = trio
    s = env.open()
    a, b = s.state.tickets[uids[0]].key, s.state.tickets[uids[1]].key
    env.update(uids[1], {"ticket.parent": a, "ticket.blocked_by": [s.state.tickets[uids[2]].key]})
    s.close()
    s = env.open(load="lazy")
    v = s.ticket(b)
    assert {uids[0], uids[1], uids[2]} <= s._loaded  # the parent and the blocker came with it
    assert v.fields["parent"] == a and not v.frozen and s.chain_errors() == [] and not s.state.workspace.invalid
    s.close()
    s = env.open(load="lazy")  # a new edit of a ticket whose references are not loaded yet
    s.append(
        {
            "type": "ticket.updated",
            "actor": env.agent,
            "base_rev": {"ticket.parent": canon.value_hash(a)},
            "set": {"ticket.parent": None},
        },
        log=uids[1],
    )
    assert s.state.tickets[uids[1]].fields["parent"] is None


def test_a_repeated_key_is_refused_even_when_its_ticket_is_not_loaded(trio):
    env, uids = trio
    s = env.open(load="lazy")
    key = s.uid_of("1") and "DEMO-0001"
    with pytest.raises(StoreError) as ex:
        s.append(
            {
                "type": "ticket.created",
                "actor": env.agent,
                "key": key,
                "ticket_type": "chore",
                "title": "dup",
                "owner": env.owner.ref,
            },
            log="01J9ZP0000000000000000DKK1",
        )
    assert ex.value.code == "ticket.exists"


def test_at_follows_what_another_process_wrote_to_a_ticket_this_store_never_loaded(trio):
    env, uids = trio
    lazy = env.open(load="lazy", clock=lambda: env.clock[0])
    later = env.other(clock=lambda: env.clock[0] + 50)
    later.append({"type": "log.added", "actor": env.agent, "text": "from the future"}, log=uids[2])
    r = lazy.append({"type": "log.added", "actor": env.agent, "text": "now"}, log=uids[0])
    assert r.event["at"] >= ts(env.clock[0] + 50)  # never before an event already in the workspace
    s = env.other()
    assert s.chain_errors() == []


def test_an_unattended_event_loads_every_ticket_for_its_workspace_wide_quota(trio):
    env, uids = trio
    s = env.open(load="lazy")
    unattended = {"kind": "agent", "id": "claude-code", "session": "s_01J9ZP0000000000000000000S", "unattended": True}
    s.append({"type": "log.added", "actor": unattended, "text": "hi"}, log=uids[0])
    assert s._loaded == set(uids)


def test_hooks_see_the_head_of_a_ticket_without_a_full_load(trio):
    env, uids = trio
    s = env.open(load="lazy")
    assert s.head_seq("2") == 2 and s.head_seq("DEMO-0009") == 0 and s.head_seq("workspace") == 2
    assert s._loaded == {uids[1]}


def test_lock_timeout_is_store_busy(env, tmp_path):
    env.bootstrap().close()
    marker = tmp_path / "held"
    p = mp.get_context("spawn").Process(target=workers.hold_lock, args=(str(env.root), str(marker), 3.0))
    p.start()
    try:
        deadline = time.time() + 20
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.02)
        t = time.time()
        with pytest.raises(StoreError) as ex:
            env.open(lock_timeout=0.3)
        assert ex.value.code == "store.busy" and time.time() - t < 2.5
    finally:
        p.join(30)
    assert env.open(lock_timeout=0.3).state.workspace.members  # free again
    assert WS


def test_a_clock_far_in_the_future_in_a_last_line_is_not_believed(trio):
    env, uids = trio
    p = env.path(uids[0], "events.jsonl")
    lines = p.read_bytes().splitlines(keepends=True)
    e = canon.parse_event_line(lines[-1])
    e["at"] = "2999-01-01T00:00:00Z"
    p.write_bytes(b"".join(lines[:-1]) + canon.event_line(e))
    s = env.open(load="lazy")
    assert any(r.code == "store.torn_write" and "future" in r.detail for r in s.reports)
    r = s.append({"type": "log.added", "actor": env.agent, "text": "x"}, log=uids[1])
    assert r.event["at"] < "2100"


def test_a_lazy_store_equals_an_eager_one_after_load_all(env):
    s = env.bootstrap()
    uids = [env.new_ticket(f"t{i}") for i in range(5)]
    for i, u in enumerate(uids):
        env.update(u, {"ticket.priority": "high"}, {"context": f"c{i}"})
    s.close()
    lazy = env.open(load="lazy")
    lazy.ticket(uids[3])
    lazy.load_all()
    eager = env.other()
    assert {u: v.fields for u, v in lazy.state.tickets.items()} == {u: v.fields for u, v in eager.state.tickets.items()}
    assert lazy.index.dump() == eager.index.dump()

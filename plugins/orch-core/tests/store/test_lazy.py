"""Lazy replay: the workspace log is always replayed, a ticket log when touched (with the tickets it refers to)."""

from __future__ import annotations

import multiprocessing as mp
import time

import pytest

from orch import canon
from orch.store import StoreError
from tests.store import workers
from tests.store.helpers import WS, Env, ts


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


def test_at_comes_from_verified_data_only(trio):
    env, uids = trio
    lazy = env.open(load="lazy", clock=lambda: env.clock[0])
    later = env.other(clock=lambda: env.clock[0] + 50)
    later.append({"type": "log.added", "actor": env.agent, "text": "from the future"}, log=uids[2])
    r = lazy.append({"type": "log.added", "actor": env.agent, "text": "now"}, log=uids[0])
    assert r.event["at"] <= ts(env.clock[0] + 5)  # an unloaded ticket's last line is not consulted for `at`
    assert env.other().chain_errors() == []


def forge_last_line(env, uid, **over):
    import json

    p = env.path(uid, "events.jsonl")
    last = json.loads(p.read_bytes().splitlines()[-1])
    last.update(over)
    last["seq"] += 1
    with open(p, "ab") as f:
        f.write(json.dumps(last).encode() + b"\n")


def test_forged_last_lines_cannot_move_at_or_hang_the_stamp(trio):
    env, uids = trio
    head = len(env.read_events("workspace"))
    forge_last_line(env, uids[2], at=ts(env.clock[0] + 6 * 3600), ws_seq=head)  # +6 h
    forge_last_line(env, uids[1], ws_seq=10**9)  # would loop for ever in a naive stamp
    s = env.open(load="lazy")
    t = time.time()
    r = s.append({"type": "claim.taken", "actor": env.agent}, log=uids[0])
    assert time.time() - t < 2 and r.event["at"] <= ts(env.clock[0] + 5) and r.event["ws_seq"] == head
    w = s.append(env.person_event(env.owner, "workspace", "grant.revoked", grant=env.grant_id), log="workspace")
    assert w.event["at"] <= ts(env.clock[0] + 5)
    env.tick(4 * 3600)
    s.refresh()
    assert not s.ticket(uids[0], heal=False).claim.live  # the forged +6 h did not extend the claim


def test_the_global_floor_uses_only_signed_last_lines_below_the_workspace_head_and_the_clock(trio):
    env, uids = trio
    head = len(env.read_events("workspace"))
    forge_last_line(env, uids[1], at=ts(env.clock[0] + 6 * 3600), ws_seq=head)  # unsigned: ignored
    s = env.open(load="lazy")
    assert s._global_floor(head)[1] <= env.clock[0] + 120  # the signed lines only
    later = env.other(clock=lambda: env.clock[0] + 3600)  # a *signed* last line, dated after our clock: reported
    later.append({"type": "log.added", "actor": env.agent, "text": "ahead"}, log=uids[2])
    floor = s._global_floor(head)
    assert floor is None or floor[1] <= env.clock[0] + 120
    assert any("dated after the host clock" in r.detail for r in s.reports)


def test_no_new_ticket_while_a_log_lacks_a_verified_creation_line(trio):
    env, uids = trio
    p = env.path(uids[2], "events.jsonl")
    lines = p.read_bytes().splitlines(keepends=True)
    lines[0] = b"x" + lines[0]
    p.write_bytes(b"".join(lines))
    k = env.root / "keys.jsonl"
    k.write_bytes(b"".join(x for x in k.read_bytes().splitlines(keepends=True) if b"0003" not in x))
    s = env.open(load="lazy")
    with pytest.raises(StoreError) as e:
        s.create_ticket(actor=env.agent, ticket_type="chore", title="C", owner=env.owner.ref)
    assert e.value.code == "chain.broken" and len(env.root.joinpath("tickets").iterdir().__class__.__name__) > 0


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


def test_resolution_of_references_goes_through_verified_creations(trio):
    """A first line that claims the key of another ticket (unsigned edit) makes the hint wrong; the verified map is
    what loads the parent, and a ticket without a verified creation makes everything load."""
    env, uids = trio
    s = env.open()
    a = s.state.tickets[uids[0]].key
    env.update(uids[1], {"ticket.parent": a})
    s.close()
    p = env.path(uids[2], "events.jsonl")
    lines = p.read_bytes().splitlines(keepends=True)
    forged = canon.parse_event_line(lines[0])
    forged["key"] = a  # the hint now says uids[2] owns the parent's key; its host_sig no longer fits
    p.write_bytes(canon.event_line(forged) + b"".join(lines[1:]))
    lazy = env.open(load="lazy")
    v = lazy.ticket(uids[1])
    assert v.fields["parent"] == a and not v.frozen and not lazy.state.workspace.invalid
    assert uids[0] in lazy._loaded and lazy._unsure()  # the broken log is seen: everything is loaded
    assert lazy._loaded == set(uids)


hypothesis = pytest.importorskip("hypothesis")


def _forge(env, uid, how, other_key):
    p = env.path(uid, "events.jsonl")
    lines = p.read_bytes().splitlines(keepends=True)
    if how == "key":
        e = canon.parse_event_line(lines[0])
        e["key"] = other_key
        lines[0] = canon.event_line(e)
    elif how == "garbage":
        lines[0] = b"garbage\n"
    elif how == "tail":
        forge_last_line(env, uid, at="2999-01-01T00:00:00Z", ws_seq=10**9)
        return
    elif how == "hide":
        lines[0] = b"x" + lines[0]
    p.write_bytes(b"".join(lines))


def test_lazy_replay_equals_full_replay_under_forged_hints(tmp_path_factory):
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        hows=st.lists(st.sampled_from(["none", "key", "garbage", "tail", "hide"]), min_size=4, max_size=4),
        touch=st.permutations([0, 1, 2, 3]),
        n_touch=st.integers(1, 4),
    )
    def check(hows, touch, n_touch):
        env = Env(tmp_path_factory.mktemp("lz"))
        s = env.bootstrap()
        uids = [env.new_ticket(f"t{i}") for i in range(4)]
        keys = [s.state.tickets[u].key for u in uids]
        env.update(uids[1], {"ticket.parent": keys[0]})
        env.update(uids[3], {"ticket.blocked_by": [keys[2]]})
        s.close()
        for i, how in enumerate(hows):
            if how != "none":
                _forge(env, uids[i], how, keys[(i + 1) % 4])
        lazy = env.open(load="lazy")
        for i in touch[:n_touch]:
            lazy.ticket(uids[i], heal=False)
        before = {u: (v.fields, v.status, v.frozen, v.sections) for u, v in lazy.state.tickets.items()}
        errors_before = {c[0] for c in lazy.chain_errors()}
        full = env.other(load="all")
        for u, snap in before.items():
            v = full.state.tickets[u]
            assert snap == (v.fields, v.status, v.frozen, v.sections), (hows, touch[:n_touch], u)
        assert errors_before <= {c[0] for c in full.chain_errors()} | set(full.diverged)

    check()


def test_a_host_write_from_a_lazy_store_first_loads_everything_when_hints_are_doubtful(trio):
    env, uids = trio
    p = env.path(uids[2], "events.jsonl")
    lines = p.read_bytes().splitlines(keepends=True)
    p.write_bytes(b"x" + lines[0] + b"".join(lines[1:]))  # no verified creation line for uids[2]
    env.path(uids[0], "ticket.json").write_text("{}")
    s = env.open(load="lazy")  # (the workspace heal of keys.jsonl may already have loaded everything)
    s.ticket(uids[0])  # heals ticket.json: a host write
    assert s._loaded == set(uids)  # ... from the full replay, not from a lazily replayed subset
    assert env.read_events(uids[0])[-1]["type"] == "projection.repaired"


def test_a_host_write_from_a_clean_lazy_store_does_not_need_to_load_everything(trio):
    env, uids = trio
    env.path(uids[0], "ticket.json").write_text("{}")
    s = env.open(load="lazy")
    s.ticket(uids[0])
    assert env.read_events(uids[0])[-1]["type"] == "projection.repaired"


def test_a_ticket_view_is_judged_at_the_current_clock(trio):
    env, uids = trio
    s = env.open(load="lazy")
    s.append({"type": "claim.taken", "actor": env.agent}, log=uids[0])
    assert s.ticket(uids[0], heal=False).claim.live
    env.tick(3 * 3600)  # claim_ttl is 120 minutes; no new load happens
    assert not s.ticket(uids[0], heal=False).claim.live

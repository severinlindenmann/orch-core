"""One workspace lock: two processes appending at once lose and interleave nothing."""

from __future__ import annotations

import multiprocessing as mp

from orch import canon
from orch.identity import CryptoVerifier
from orch.model import replay
from tests.store import workers
from tests.store.helpers import WS, Env


def args(env: Env):
    return str(env.root), str(env.tmp / "wsk"), str(env.host_state)


def run(target, *a):
    ctx = mp.get_context("spawn")
    with ctx.Pool(2) as pool:
        rs = [pool.apply_async(target, x) for x in a]
        return [r.get(timeout=120) for r in rs]


def test_two_processes_appending_to_one_ticket_lose_and_interleave_nothing(env):
    env.bootstrap()
    uid = env.new_ticket()
    env.store.close()
    r = run(
        workers.append_notes,
        (*args(env), uid, env.agent, 12, "a"),
        (*args(env), uid, env.agent, 12, "b"),
    )
    assert r == [12, 12]
    s = env.open()
    evs = env.read_events(uid)
    assert [e["seq"] for e in evs] == list(range(1, 26))  # 1 created + 24 notes, no gap, no duplicate seq
    texts = [e["text"] for e in evs[1:]]
    assert sorted(texts) == sorted([f"a-{i}" for i in range(12)] + [f"b-{i}" for i in range(12)])
    assert [t for t in texts if t.startswith("a-")] == [f"a-{i}" for i in range(12)]  # each process keeps its order
    prev = None
    for e in evs:
        assert e["prev"] == prev
        prev = canon.event_head(e)
    assert s.chain_errors() == [] and s.reports == []
    fresh = replay(
        env.read_events("workspace"), {uid: evs}, verifier=CryptoVerifier(), now=s.state.now,
        expected_workspace_id=WS, expected_genesis=s.genesis,
    )  # fmt: skip
    assert not fresh.chain_errors and not fresh.workspace.invalid


def test_two_processes_creating_tickets_never_share_a_key(env):
    env.bootstrap()
    env.store.close()
    a, b = run(
        workers.create_tickets,
        (*args(env), env.agent, env.owner.ref, 6),
        (*args(env), env.agent, env.owner.ref, 6),
    )
    assert len(set(a) | set(b)) == 12 and not set(a) & set(b)
    s = env.open()
    assert sorted(t.key for t in s.state.tickets.values()) == [f"DEMO-{i:04d}" for i in range(1, 13)]
    assert s.chain_errors() == []
    lines = (env.root / "keys.jsonl").read_text().splitlines()
    assert len(lines) == 12 and not s.reports
    assert s.scan() == []  # keys.jsonl and every ticket.json agree with the logs


def test_the_lock_is_reentrant_in_a_process_and_exclusive_across_processes(env):
    from orch.store import FileLock, LockTimeout

    path = env.tmp / "x.lock"
    with FileLock(path), FileLock(path):
        pass
    ctx = mp.get_context("spawn")
    with FileLock(path):
        with ctx.Pool(1) as pool:
            assert pool.apply(workers_try_lock, (str(path),)) == "busy"
    with ctx.Pool(1) as pool:
        assert pool.apply(workers_try_lock, (str(path),)) == "got it"
    assert LockTimeout.code == "store.busy"


def workers_try_lock(path: str) -> str:
    from orch.store import FileLock, LockTimeout

    try:
        with FileLock(path, timeout=0.2):
            return "got it"
    except LockTimeout:
        return "busy"

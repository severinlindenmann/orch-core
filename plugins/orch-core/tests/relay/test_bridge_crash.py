"""R2 exactly-once under crashes (11A.10 "R2 exactly-once"): a kill at every boundary of the rid state machine
(before T1, after T1, after the outcome ``begin``, after the append, before and after T2, after ``attempted``, after
the 202), then a restart (recovery) and the device's own recovery (identical resend; on ``already_done {unknown}`` a
new rid with the same idem). Invariant: the event lands exactly once, every chunk index only ever carries one byte
string, and the device ends with a definite answer. One test kills a real child process (``os._exit``)."""

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from orch.relay.rids import Crash
from orch.wsproto import wire

from .bridgekit import World, open_chunk

POINTS = [
    "before_T1", "after_T1", "after_begin", "after_append", "before_T2", "after_T2", "after_mark_attempted",
    "after_mark_posted",
]  # fmt: skip


def _fault(point: str):
    state = {"hit": False}

    def fault(p: str) -> None:
        if p == point and not state["hit"]:
            state["hit"] = True
            raise Crash(point)

    return fault, state


def _frozen(w: World) -> None:
    """Every (rid, idx) the relay ever got carries exactly one byte string."""
    seen: dict[tuple[str, int], set[bytes]] = defaultdict(set)
    for _, rid, env in w.post.posts:
        seen[(rid, wire.parse_header(env[: wire.HEADER_LEN]).seq)].add(env)
    assert all(len(v) == 1 for v in seen.values()), "a chunk index was posted with two byte strings"


def _settle(w: World, m: dict, env: bytes) -> dict:
    """The device: resend the identical bytes; on silence or already_done, a new rid with the same idem."""
    rid = env[40:56].hex()
    w.send(env)
    for _, r, chunk in reversed(w.post.posts):
        if r == rid:
            _, meta, _ = open_chunk(chunk, w.wk, w.key.public_key)
            if meta == {"status": "ok"}:
                return w.answer(w.post.for_rid(rid)[-1:])[1]
            break
    meta, data = w.call(m, ts_ms=w.clock.ms)  # not run / unknown: the same idem under a new rid, stamped now
    assert meta == {"status": "ok"}, meta
    return data


@pytest.mark.parametrize("point", POINTS)
def test_a_write_lands_exactly_once_whatever_the_crash_point(tmp_path, point):
    fault, state = _fault(point)
    w = World(tmp_path, fault=fault)
    try:
        m = w.new_ticket()
        env = w.phone.request(w.wk, m)
        try:
            w.send(env)
        except Crash:
            pass
        assert state["hit"], f"{point} was never reached"
        w.clock.ms += 1_000
        w.reopen()
        data = _settle(w, m, env)
        assert data["data"]["log"] == m["args"]["log"]
        assert len(w.host.log) == 1, "the event landed more or less than once"
        _frozen(w)
        assert w.rids.dump()["submission"] == []  # deleted with T2
        assert not [r for r in w.rids.dump()["rid"] if r[2] == "admitted"]
    finally:
        w.close()


@pytest.mark.parametrize("point", ["after_T1", "before_T2"])
def test_a_read_admitted_before_a_crash_is_answered_already_done_unknown(tmp_path, point):
    fault, _ = _fault(point)
    w = World(tmp_path, fault=fault)
    try:
        env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
        with pytest.raises(Crash):
            w.send(env)
        w.reopen()
        (chunk,) = w.post.for_rid(env[40:56])
        assert open_chunk(chunk, w.wk, w.key.public_key)[1] == {"refusal": "already_done", "status": "unknown"}
        assert w.send(env) == [chunk]  # the resend replays the same bytes
    finally:
        w.close()


def test_recovery_replays_persisted_chunks_before_anything_else_and_never_rebuilds_them(tmp_path):
    fault, _ = _fault("after_mark_attempted")
    w = World(tmp_path, fault=fault)
    try:
        env = w.phone.request(w.wk, {"op": "whoami", "args": {}})
        with pytest.raises(Crash):
            w.send(env)
        stored = w.rids.get(w.phone.device, env[40:56].hex()).chunks[0].data
        w.reopen()
        assert w.post.for_rid(env[40:56]) == [stored]
        assert w.host.reads == ["whoami"]
    finally:
        w.close()


def test_rid_only_loss_vs_whole_state_rollback_the_start_fence(tmp_path):
    """The rid store is lost (or the whole state rolled back) after a write ran: a relay replay of the old request is
    fenced by its ts_ms (already_done, not run); the device's same-idem resend gets the stored outcome."""
    w = World(tmp_path)
    try:
        m = w.new_ticket()
        env = w.phone.request(w.wk, m)
        w.send(env)
        w.close()
        for name in ("rids.sqlite", "rids.sqlite-wal", "rids.sqlite-shm"):
            p = tmp_path / "relay" / name
            if p.exists():
                p.unlink()
        w.clock.ms += 5_000
        w.reopen()
        n = len(w.post.posts)
        w.send(env)  # the hostile relay replays the old request
        (chunk,) = [e for _, _, e in w.post.posts[n:]]
        assert open_chunk(chunk, w.wk, w.key.public_key)[1] == {"refusal": "already_done", "status": "unknown"}
        assert len(w.host.runs) == 1
        _, data = w.call(m, ts_ms=w.clock.ms)
        assert data["duplicate"] is True and len(w.host.log) == 1
    finally:
        w.close()


CHILD = """
import os, sys
sys.path.insert(0, {root!r})
from pathlib import Path
from tests.relay.bridgekit import World

def fault(p):
    if p == {point!r}:
        os._exit(9)  # a hard kill: no finally, no close, no flush

w = World(Path({tmp!r}), fault=fault, seed=b"r2-kill")
w.send(bytes.fromhex({env!r}))
os._exit(0)
"""


@pytest.mark.parametrize("point", ["after_T1", "after_begin", "after_append", "after_T2", "after_mark_attempted"])
def test_a_real_process_kill_between_durable_steps(tmp_path, point):
    """A child process with the same world (same seed) runs the request and is killed with ``os._exit`` at
    ``point``: only fsynced state survives. The parent then restarts, recovers and settles the device."""
    root = str(Path(__file__).resolve().parents[2])
    w = World(tmp_path, seed=b"r2-kill")
    m = w.new_ticket()
    env = w.phone.request(w.wk, m)
    w.close()
    code = CHILD.format(root=root, point=point, tmp=str(tmp_path), env=env.hex())
    r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, timeout=60)
    assert r.returncode == 9, r.stderr.decode()[-2000:]  # the kill point was reached
    w.clock.ms += 1_000
    w.reopen()
    try:
        data = _settle(w, m, env)
        assert data["data"]["log"] == m["args"]["log"] and len(w.host.log) == 1
        assert w.rids.dump()["submission"] == []
    finally:
        w.close()

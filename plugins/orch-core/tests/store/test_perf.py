"""Latency at 1000 tickets: append (fsync excluded, target < 20 ms), cold open and a single-ticket command (target
< 300 ms), reload and full load. Slow: builds the workspace."""

from __future__ import annotations

import os
import statistics
import time

import pytest

from orch import crypto
from tests.identity.helpers import Person
from tests.store.helpers import Env

pytestmark = pytest.mark.slow
SLACK = 4 if os.environ.get("CI") else 1  # shared runners under -n auto: the numbers are printed, the bounds are loose


def build(env: Env, n: int):
    s = env.bootstrap()
    uids = [env.new_ticket(f"t{i}") for i in range(n)]
    return s, uids


def measure(fn, n: int, clock=time.process_time) -> list[float]:
    out = []
    for _ in range(n):
        t = clock()
        fn()
        out.append((clock() - t) * 1000)
    return out


def best_median(samples: list[float], batches: int = 5) -> float:
    """The best of ``batches`` medians: a regression moves every batch, a busy machine only some."""
    k = len(samples) // batches
    return min(statistics.median(samples[i * k : (i + 1) * k]) for i in range(batches))


def test_append_latency_at_1000_tickets(env, monkeypatch, capsys):
    s, uids = build(env, 1000)
    mara = Person()
    from orch.identity import certs

    s.append(
        env.person_event(
            env.owner, "workspace", "member.added", person=mara.ref, name="Mara", role="member",
            pk_pub=crypto.b64u(mara.pk_pub), device_cert=mara.cert(created=1_790_000_000_000),
        ),
        log="workspace",
    )  # fmt: skip
    assert certs
    monkeypatch.setattr(os, "fsync", lambda fd: None)  # the target excludes fsync
    from orch.store import fsio

    monkeypatch.setattr(fsio, "fsync_dir", lambda p: None)
    monkeypatch.setattr(fsio, "_full_fsync", lambda fd: None)  # F_FULLFSYNC counts as a flush too
    import orch.store.store as sm

    monkeypatch.setattr(sm, "fsync_dir", lambda p: None)
    i = iter(range(10**6))
    ticket = measure(lambda: env.log(uids[next(i) % 1000]), 60)
    roles = []
    for r in ("maintainer", "member", "maintainer"):
        t = time.process_time()
        s.append(env.person_event(env.owner, "workspace", "role.changed", person=mara.ref, role=r), log="workspace")
        roles.append((time.process_time() - t) * 1000)
    msg = (
        f"ticket event: median {statistics.median(ticket):.1f} ms, "
        f"p95 {sorted(ticket)[int(len(ticket) * 0.95)]:.1f} ms, "
        f"max {max(ticket):.1f} ms; role.changed: {', '.join(f'{x:.0f} ms' for x in roles)}"
    )
    with capsys.disabled():
        print("\nPERF", msg)
    assert best_median(ticket) < 20 * SLACK
    assert max(roles) < 5000 * SLACK
    monkeypatch.undo()
    s.close()

    def ms(fn):
        t = time.perf_counter()
        out = fn()
        return (time.perf_counter() - t) * 1000, out

    cold, lazy = ms(lambda: env.open(load="lazy"))
    t_ticket, _ = ms(lambda: lazy.ticket("DEMO-0500"))
    t_append, _ = ms(lambda: lazy.append({"type": "log.added", "actor": env.agent, "text": "x"}, log=uids[499]))
    lazy.close()
    # a fresh process's whole life: open, read, append; wall time (it flushes to disk), best of 5
    cmd = min(ms(lambda k=k: _one_command(env, uids[321 + k]))[0] for k in range(5))
    other = env.other(load="lazy")
    other.append({"type": "log.added", "actor": env.agent, "text": "elsewhere"}, log=uids[7])
    reload_ms, _ = ms(lambda: lazy.refresh())  # `lazy` is closed but usable: it re-reads what changed
    full, _ = ms(lambda: lazy.load_all())
    msg2 = (
        f"cold open (lazy) {cold:.0f} ms; first ticket read {t_ticket:.0f} ms; first append {t_append:.0f} ms; "
        f"open + read + append one ticket {cmd:.0f} ms; reload after another process appended {reload_ms:.0f} ms; "
        f"load_all (every event verified) {full:.0f} ms"
    )
    with capsys.disabled():
        print("PERF", msg2)
    assert cmd < 300 * SLACK


def _one_command(env, uid):
    s = env.open(load="lazy")
    s.ticket(uid)
    s.append({"type": "log.added", "actor": env.agent, "text": "one command"}, log=uid)
    s.close()
    return s

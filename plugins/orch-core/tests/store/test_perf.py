"""Append latency at 1000 tickets, fsync excluded (target for a ticket event: < 20 ms). Slow: builds the workspace."""

from __future__ import annotations

import os
import statistics
import time

import pytest

from orch import crypto
from tests.identity.helpers import Person
from tests.store.helpers import Env

pytestmark = pytest.mark.slow


def build(env: Env, n: int):
    s = env.bootstrap()
    uids = [env.new_ticket(f"t{i}") for i in range(n)]
    return s, uids


def measure(fn, n: int) -> list[float]:
    out = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000)
    return out


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
    import orch.store.store as sm

    monkeypatch.setattr(sm, "fsync_dir", lambda p: None)
    i = iter(range(10**6))
    ticket = measure(lambda: env.log(uids[next(i) % 1000]), 60)
    roles = []
    for r in ("maintainer", "member", "maintainer"):
        t = time.perf_counter()
        s.append(env.person_event(env.owner, "workspace", "role.changed", person=mara.ref, role=r), log="workspace")
        roles.append((time.perf_counter() - t) * 1000)
    msg = (
        f"ticket event: median {statistics.median(ticket):.1f} ms, "
        f"p95 {sorted(ticket)[int(len(ticket) * 0.95)]:.1f} ms, "
        f"max {max(ticket):.1f} ms; role.changed: {', '.join(f'{x:.0f} ms' for x in roles)}"
    )
    with capsys.disabled():
        print("\nPERF", msg)
    assert statistics.median(ticket) < 20
    assert max(roles) < 5000

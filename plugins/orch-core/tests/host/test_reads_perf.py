"""H6b read cost at 1000 tickets: ``ticket.query`` (first page, a walk of the later pages), ``needs`` and
``activity.list`` warm and cold (base cache dropped, as after a journal rebuild). Slow: builds the workspace. The
numbers are printed; the bounds are loose (shared runners).

Note (O(rows) per later page): a later ``ticket.query`` page checks the visibility of every uid of the snapshot (to
count ``total`` and find the next row) and rebuilds only the ``limit`` rows it serves, so walking N rows in pages of
``limit`` costs O(N * N / limit) checks in all; the walk below measures it."""

from __future__ import annotations

import os
import time

import pytest

from orch.host import reads_feed
from tests.host.lifecycle_helpers import Lab
from tests.host.live_helpers import World, ok

pytestmark = pytest.mark.slow
SLACK = 4 if os.environ.get("CI") else 1
N = 1000


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, (time.perf_counter() - t) * 1000


def test_read_costs_at_1000_tickets(tmp_path, capsys, monkeypatch):
    from orch.host.control_client import ControlClient
    from tests.host import lifecycle_helpers

    # registering 1000 tickets builds the journal: on a slow runner that takes longer than the 5 s control default
    monkeypatch.setattr(
        lifecycle_helpers.HostThread, "client", lambda self: ControlClient(self.host.control_path, timeout=180)
    )
    lab = Lab(tmp_path)
    store = lab.ws.other()  # the tickets are made before the host starts (making them through it takes minutes)
    lab.ws.store = store
    try:
        for i in range(N):
            lab.ws.tick()
            lab.ws.new_ticket(f"ticket {i}")
    finally:
        store.close()
        lab.ws.store = None
    w = World(lab)
    try:
        client = w.client()
        ids = iter(range(10**6))

        class C:  # a slow CI runner: wait up to 2 min for each answer (the host's own timeout_ms raised to match)
            @staticmethod
            def req(op, args):
                client.ws.send(f"p{next(ids)}", op, args, opts={"timeout_ms": 120_000})
                return client.ws.read(timeout=120)

        c = C()
        first, first_ms = timed(lambda: ok(c.req("ticket.query", {"limit": 50, "sort": "key", "order": "asc"}))["data"])
        assert first["total"] == N and first["next_page"]

        def walk():
            args, page, n = {"limit": 50, "sort": "key", "order": "asc"}, first["next_page"], 0
            while page:
                data = ok(c.req("ticket.query", {**args, "page": page}))["data"]
                n, page = n + len(data["rows"]), data["next_page"]
            return n

        later, walk_ms = timed(walk)
        assert later == N - 50
        _, needs_ms = timed(lambda: ok(c.req("needs", {}))["data"])
        _, warm_first = timed(lambda: ok(c.req("activity.list", {"limit": 50}))["data"])
        _, warm_ms = timed(lambda: ok(c.req("activity.list", {"limit": 50}))["data"])
        w.service.activity_base = None  # cold: as after a journal rebuild
        cold, cold_ms = timed(lambda: ok(c.req("activity.list", {"limit": 50}))["data"])
        assert len(cold["items"]) == 50
        old = reads_feed.MAX_ENTRIES
        reads_feed.MAX_ENTRIES = 100  # a cache that no longer holds the oldest events: a deep page is a cold range read
        try:
            w.service.activity_base = None
            _, deep_ms = timed(lambda: ok(c.req("activity.list", {"limit": 50, "order": "asc"}))["data"])
        finally:
            reads_feed.MAX_ENTRIES = old
        with capsys.disabled():
            print(
                f"\nPERF ticket.query first {first_ms:.0f} ms; {later // 50} later pages {walk_ms:.0f} ms "
                f"({walk_ms / (later // 50):.1f} ms/page); needs {needs_ms:.0f} ms; activity.list warm {warm_ms:.0f} ms"
                f" (first call {warm_first:.0f} ms), cold {cold_ms:.0f} ms, capped-cache asc page {deep_ms:.0f} ms"
            )
        assert first_ms < 2_000 * SLACK
        assert walk_ms / (later // 50) < 3_000 * SLACK
        assert needs_ms < 5_000 * SLACK
        assert warm_ms < 500 * SLACK
        assert cold_ms < 30_000 * SLACK
        assert deep_ms < 30_000 * SLACK
    finally:
        w.close()
        w.lab.close()

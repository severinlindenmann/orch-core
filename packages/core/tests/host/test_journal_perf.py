"""H6a journal cost at 1000 tickets (review of #396): the build (start and every F11 rebuild), the per-append hooks
(the cheap foreign-append check and the journal step) against the 20 ms append target of ``tests/store/test_perf.py``,
the full 60 s check, and a workspace event that recomputes every ticket's needs. Slow: builds the workspace. The numbers
are printed; the bounds are loose (shared runners). Incremental rebuild is the follow-up if the build time matters."""

from __future__ import annotations

import os
import statistics
import time

import pytest

from orch import crypto
from orch.host.journal import Cursors, Journal
from tests.identity.helpers import Person
from tests.store.helpers import WS, Env

pytestmark = pytest.mark.slow
SLACK = 4 if os.environ.get("CI") else 1
N = 1000


def test_journal_build_and_append_hooks_at_1000_tickets(tmp_path, capsys):
    env = Env(tmp_path)
    s = env.bootstrap()
    uids = [env.new_ticket(f"t{i}") for i in range(N)]
    mara = Person()
    s.append(
        env.person_event(
            env.owner, "workspace", "member.added", person=mara.ref, name="Mara", role="member",
            pk_pub=crypto.b64u(mara.pk_pub), device_cert=mara.cert(created=1_790_000_000_000),
        ),
        log="workspace",
    )  # fmt: skip
    s.close()
    store = env.other(load="lazy")  # as the host opens it
    env.store = store
    journal = Journal(WS, env.store_dir, Cursors(os.urandom(32)))
    t = time.perf_counter()
    journal.build(store)
    build_ms = (time.perf_counter() - t) * 1000
    assert journal.head > N and not journal.stale

    hooks: list[float] = []
    check, committed = journal.check, journal.committed

    def timed_check() -> None:
        t0 = time.perf_counter()
        check()
        hooks.append((time.perf_counter() - t0) * 1000)

    def timed_commit(log: str, event: dict) -> None:
        t0 = time.perf_counter()
        committed(log, event)
        hooks[-1] += (time.perf_counter() - t0) * 1000

    store.before_append, store.on_commit = timed_check, timed_commit
    for i in range(60):
        env.tick()
        env.log(uids[i % N])
    per_append = statistics.median(hooks)
    t = time.perf_counter()
    with store.locked():
        journal.check(full=True)
    full_ms = (time.perf_counter() - t) * 1000
    env.tick()
    hooks.clear()
    s2 = store.append(env.person_event(env.owner, "workspace", "role.changed", person=mara.ref, role="maintainer"),
                      log="workspace")  # fmt: skip
    role_ms = hooks[-1]
    assert s2.event["type"] == "role.changed" and not journal.stale
    store.close()
    env.store = None
    msg = (
        f"journal build {build_ms:.0f} ms ({journal.head} events); per ticket append {per_append:.2f} ms (hooks); "
        f"full check {full_ms:.1f} ms; role.changed step {role_ms:.0f} ms"
    )
    with capsys.disabled():
        print("\nPERF", msg)
    assert per_append < 5 * SLACK  # about 0.4 ms today; the append target is 20 ms
    assert build_ms < 30_000 * SLACK
    assert full_ms < 500 * SLACK
    assert role_ms < 3_000 * SLACK

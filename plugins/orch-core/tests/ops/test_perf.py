"""1000 tickets: the commands an agent runs all the time stay fast (review of #351, finding 2)."""

from __future__ import annotations

import os
import time

import pytest

from tests.ops.helpers import Cli, Ws

pytestmark = pytest.mark.slow
SLACK = 3 if os.environ.get("CI") else 1  # shared runners are slower; the budgets are for a laptop


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    w = Ws(tmp_path_factory.mktemp("big"))
    w.bootstrap()
    s = w.store
    for i in range(1000):
        r = s.create_ticket(
            actor=w.agent, ticket_type="feature", title=f"Ticket number {i} about seeds", owner=w.owner.ref
        )
        w.tick() if i % 50 == 0 else None
        assert r.event["seq"] == 1
    s.close()
    w.store = None
    return w


RUNS = 5


def timed(cli, *argv, budget, cpu=True):
    """Best of RUNS: a real regression moves the minimum, load noise does not. The commands run in this process,
    so CPU time (``process_time``) is what is measured; ``cpu=False`` measures wall time (for a command that waits)."""
    clock = time.process_time if cpu else time.perf_counter
    best, r = float("inf"), None
    for _ in range(RUNS if cpu else 1):
        t = clock()
        r = cli(*argv)
        best = min(best, clock() - t)
        assert r.code == 0, (argv, r.err)
    assert best < budget * SLACK, f"{' '.join(argv)} took {best:.2f} s best of {RUNS} (budget {budget} s)"
    return r


def test_the_commands_of_a_task_loop_meet_their_budgets(big):
    cli = Cli(big)
    assert cli("claim", "DEMO-0500").code == 0
    for argv in (
        ["show"],
        ["status"],
        ["task", "list"],
        ["log", "a note"],
        ["ac", "add", "a criterion"],
        ["task", "next"],
    ):
        timed(cli, *argv, budget=0.4)
    timed(cli, "wait", "--timeout", "1", budget=2.0, cpu=False)  # the timeout plus one second
    timed(cli, "list", budget=0.8)
    timed(cli, "list", "--status", "open", "--limit", "50", budget=0.8)
    timed(cli, "inbox", budget=0.8)
    timed(cli, "next", budget=0.8)
    r = timed(cli, "search", "number 777", budget=1.5)
    assert "DEMO-0778" in r.out


def test_a_hidden_ticket_is_not_counted_at_scale(big):
    anon = Cli(big, grant=False)
    r = timed(anon, "list", "--limit", "5", budget=0.8)
    assert "50 or more not shown" in r.out
